"""`keepfold app`: a stdlib HTTP server on 127.0.0.1 and one page (docs/design/keepfold-app.md).

Security, because the app changes other apps' config files:
- a per-launch random token on every request: the page, its CSS and JS take it as `?t=` (the
  page then cleans the URL); every API call sends it as the `X-Keepfold-Token` header;
- the Host must be the app's own loopback address (DNS rebinding), and any Origin its own;
- GET never changes anything; changes are POST with `Content-Type: application/json`;
- CSP `default-src 'self'`, no inline script; the page builds its DOM without innerHTML;
- in the browser it stops after 30 minutes idle, or on Quit; the desktop app (desktop.py: its
  own window and a tray icon) runs until Quit.
Long actions run as background jobs, one at a time, with a progress line (`Jobs`).
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import unicodedata
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote, unquote, urlsplit

from keepfold import atomic, update
from keepfold.app import actions, exports, news, status, targets
from keepfold.app.routes._common import (  # noqa: F401  (re-exported: `server.Busy` and the rest)
    CAPTURE_MAX,
    CLOUD_FIELDS,
    DRY_IDLE_S,
    IMPORTS,
    PAGE_ROWS,
    URL_ONLY,
    Busy,
    NotFound,
    _bool,
    _checkout,
    _connect_skipped,
    _disk,
    _domains,
    skill_url,
)
from keepfold.app.routes.chat import ChatRoutes
from keepfold.app.routes.home import HomeRoutes
from keepfold.app.routes.imports import ImportsRoutes
from keepfold.app.routes.review import ReviewRoutes
from keepfold.app.routes.setup import SetupRoutes
from keepfold.app.routes.system import SystemRoutes
from keepfold.app.routes.xray import XrayRoutes
from keepfold.secret_rules import log_safe

HOST = "127.0.0.1"
IDLE_S = 30 * 60
PRIVATE_S = 10 * 60  # the Private switch relocks by itself after 10 minutes
UPDATE_FIRST_S, UPDATE_EVERY_S = 60, 6 * 3600  # the quiet update check: a minute in, then 6 h
WATCH_S = 5  # seconds between idle checks
WARM_STATUS = True  # `start` asks Status's slow probes at once (tests turn it off: conftest)
PIGGYBACK = True  # C8: a fact-harvest pass after each Chat turn (tests turn it off: conftest)
MAX_BODY = 64 * 1024
# D7 §2.6: the banner on a vault this Keepfold must not change (fixed text, no note text)
READ_ONLY = {"too_new": "This vault was made by a newer Keepfold. It is read-only here until "
                        "you update.",
             "unreadable": "Keepfold can't read this vault's format file, so it won't change "
                           "your notes. Run keepfold doctor."}
PICTURE ="/api/brain/picture"  # Save picture: a PNG in base64, so it has its own body cap
PICTURE_MAX = 8 * 1024 * 1024  # decoded bytes
MAX_UPLOAD = 4 * 2**30
UPLOAD = "/api/import/upload"  # the one raw (not JSON) POST: an export file, streamed to disk
UPLOAD_EXT = (".zip", ".json", ".html", ".md", ".txt", ".ics", ".mbox",
              ".pdf", ".docx",  # these two: a document, or a file attached in Chat (C1)
              ".apkg", ".colpkg")  # Anki exports (R22)
ASSETS = Path(__file__).resolve().parents[1] / "assets" / "app"
_JS, _CSS = "text/javascript; charset=utf-8", "text/css; charset=utf-8"
# path -> (its file, type); "/" is the live home, filled by dashboard.render_live
STATIC = {"/": (None, "text/html; charset=utf-8"),
          "/manage": (ASSETS / "index.html", "text/html; charset=utf-8"),
          "/app.css": (ASSETS / "app.css", _CSS), "/app.js": (ASSETS / "app.js", _JS),
          # quick capture's box (the desktop app's hotkey): its own small page
          "/capture": (ASSETS / "capture.html", "text/html; charset=utf-8"),
          "/capture.js": (ASSETS / "capture.js", _JS),
          "/dashboard.css": (ASSETS.parent / "dashboard.css", _CSS),
          "/favicon.ico": (ASSETS.parent / "keepfold.ico", "image/x-icon"),  # browsers ask unasked
          **{"/" + n: (ASSETS.parent / n, _JS) for n in ("d3.v7.min.js", "brain3d.js",
                                                        "dashboard.js")}}
PORT = 47615  # tried first: one origin across launches keeps the page's theme and brain style
QUERY_GETS = {"/api/chat/get", "/api/chat/passage", "/api/home",  # GETs that read the query
              "/api/home/delta", "/api/home/site", "/api/status", "/api/browse/private-chats",
              "/api/chat/pages"}
CSP = ("default-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")
EXPIRED = (b"<!doctype html><meta charset=utf-8><title>Keepfold</title><p>This link has no valid "
           b"key. Open the link that <code>keepfold app</code> printed, or run it again.</p>")
EXPIRED_INSTALLED = (b"<!doctype html><meta charset=utf-8><title>Keepfold</title><p>This link "
                     b"has no valid key. Open Keepfold from the Start menu again.</p>")


def snapshot_name() -> str:
    return f"keepfold-snapshot-{time.strftime('%Y-%m-%d')}.html"


def reopen() -> str:
    """How this user reopens the app (A12): the Start menu for an installer install."""
    m = update.mode()
    if m == "app":
        return ("Open Keepfold from Applications or Launchpad to reopen it."
                if sys.platform == "darwin" else "Open Keepfold from the app menu to reopen it.")
    return ("Open Keepfold from the Start menu to reopen it." if m == "installer"
            else "Run keepfold app again to reopen it.")
_open = webbrowser.open  # tests replace this




def _opener() -> list[str] | None:
    """The OS's own file opener off Windows (Finder, the file manager), else None."""
    if sys.platform == "darwin":
        return ["open"]
    return ["xdg-open"] if sys.platform.startswith("linux") else None


def _open_path(p: Path) -> None:
    if sys.platform == "win32" and _open is webbrowser.open:
        os.startfile(p)  # noqa: S606  (a folder or file Keepfold chose, never the page's)
    elif _open is webbrowser.open and (cmd := _opener()):
        targets._run([*cmd, str(p)], timeout=10)
    else:
        _open(p.as_uri())


def _obsidian() -> bool:  # tests replace this
    """An obsidian:// handler is registered: the Windows registry, Obsidian.app on macOS, or
    xdg-mime's default handler elsewhere."""
    if sys.platform == "darwin":
        try:
            return any((d / "Applications" / "Obsidian.app").exists()
                       for d in (Path("/"), Path.home()))
        except (OSError, RuntimeError):  # no home, or a folder it may not read
            return False
    if sys.platform != "win32":
        ok, out = targets._run(["xdg-mime", "query", "default", "x-scheme-handler/obsidian"],
                               timeout=5)
        return ok and bool(out)
    import winreg
    for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Classes\obsidian"),
                      (winreg.HKEY_CLASSES_ROOT, "obsidian")):
        try:
            winreg.CloseKey(winreg.OpenKey(hive, key))
            return True
        except OSError:
            pass
    return False


def _open_note(p: Path) -> dict:
    """A vault page or folder in Obsidian; without Obsidian, with this computer's default app
    (Explorer for a folder)."""
    if _obsidian():
        _open("obsidian://open?path=" + quote(str(p), safe=""))
        return {"ok": True, "obsidian": True}
    _open_path(p)
    return {"ok": True, "obsidian": False}


# ---- jobs -------------------------------------------------------------------------------------


KEEPALIVE_S = 3  # seconds between {"t": "wait"} lines while a chat answer is quiet



class Job:
    def __init__(self, name: str, section: str, cancellable: bool):
        self.name, self.section, self.cancellable = name, section, cancellable
        self.lines: list[str] = []
        self.result = self.error = None
        # monotonic: durations only, so a clock change mid-import never shows negative seconds
        self.running, self.started, self.ended = True, time.monotonic(), None
        self.cancelled = threading.Event()
        self.proc = None
        self.steps: dict | None = None  # a stepped job's place (`progress`)

    def progress(self, step: int, steps: int, done: int, total: int, label: str) -> None:
        """Where a stepped job is: step `step` of `steps`, named `label`, `done` of `total` in
        it. The overall share never goes back."""
        frac = min(max(done / total, 0.0), 1.0) if total else 0.0
        overall = ((step - 1) + frac) / steps if steps else 0.0
        if self.steps is not None:
            overall = max(overall, self.steps["overall"])
        self.steps = {"step": step, "steps": steps, "label": label, "done": done,
                      "total": total, "pct": round(frac * 100), "overall": overall,
                      "at": time.monotonic()}

    def say(self, line) -> None:
        self.lines.append(str(line)[:400])
        del self.lines[:-300]

    def cancel(self) -> None:
        self.cancelled.set()
        if self.proc is not None:
            try:
                self.proc.terminate()
            except OSError:
                pass

    def view(self) -> dict:
        out = {"name": self.name, "section": self.section, "running": self.running,
               "cancellable": self.cancellable, "line": self.lines[-1] if self.lines else "",
               "seconds": round((self.ended or time.monotonic()) - self.started, 1),
               "result": self.result, "error": self.error}
        if (s := self.steps) is not None:
            took, overall = s["at"] - self.started, s["overall"]
            # time left from the pace so far, once there is a pace to go by
            eta = took * (1 - overall) / overall if overall >= 0.02 and took >= 2 else None
            out["progress"] = {**{k: s[k] for k in ("step", "steps", "label", "done", "total",
                                                    "pct")},
                               "overall": round(overall * 100),
                               "eta_s": None if eta is None else round(eta)}
        return out


def _log_exc() -> None:
    """The exception being handled, as one safe line on stderr (A13: no stack text, no secret,
    no home path)."""
    print(log_safe(sys.exc_info()[1]), file=sys.stderr)


class Jobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.cur: Job | None = None
        self.seq = 0
        self.on_end = None  # called with each job once it ends (the desktop app's notifier)

    def start(self, name: str, section: str, fn, *, cancellable: bool = False,
              quiet_errors: bool = False) -> dict:
        """Run `fn(job)` on a thread; Busy while another job runs. `quiet_errors` reports an
        unexpected error by type only (browse jobs: a message could quote a page)."""
        with self.lock:
            if self.cur is not None and self.cur.running:
                raise Busy(f"Wait: {self.cur.name} is still running.")
            job = self.cur = Job(name, section, cancellable)
            self.seq += 1

        def body():
            try:
                job.result = fn(job)
            except Exception as e:  # noqa: BLE001  (reported on the page, never a traceback)
                from keepfold.vault import VaultError  # its messages are written for users
                job.error = (type(e).__name__ if quiet_errors
                             and not isinstance(e, ValueError | VaultError)
                             else str(e)[:500] or type(e).__name__)
                if not quiet_errors and not isinstance(e, ValueError | VaultError):  # refusals
                    _log_exc()
            finally:
                job.running, job.ended = False, time.monotonic()
                status.forget()  # a job may have changed what Status shows
                if self.on_end is not None:
                    try:
                        self.on_end(job)
                    except Exception:  # noqa: BLE001  (a notification never fails the job)
                        _log_exc()

        threading.Thread(target=body, daemon=True, name=f"keepfold-app-{name}").start()
        return {"job": name, "seq": self.seq}

    def view(self) -> dict:
        return {"job": self.cur.view() if self.cur else None, "seq": self.seq}

    def running(self) -> bool:
        return self.cur is not None and self.cur.running


# ---- the app ---------------------------------------------------------------------------------






class App(HomeRoutes, ReviewRoutes, XrayRoutes, ChatRoutes, SetupRoutes, ImportsRoutes,
          SystemRoutes):
    def __init__(self, root: Path | None):
        self.root = Path(root).resolve() if root else None
        self.token = secrets.token_urlsafe(32)
        self.port = 0
        self.jobs = Jobs()
        self.scanned: dict = {}  # source key -> the last scan's counts
        self.uploads: dict[str, Path] = {}  # upload id -> its file under ~/.keepfold/tmp
        self.chatting = threading.Lock()  # one chat answer at a time, beside any job
        self.piggy = threading.Lock()  # C8: one fact-harvest piggyback after a Chat turn at once
        self.echo = print  # `start` sets its own: the background passes' log lines
        from keepfold import askfile
        self.files = askfile.Store()  # C1: files attached in Chat, in memory only
        self.last = time.monotonic()
        self.stop = threading.Event()
        self.desktop = None  # desktop.Desktop (window and tray) in the desktop app, else None
        self.private_until = 0.0  # the Private switch: ?private=1 applies until this monotonic
        self.update_ready: dict | None = None  # a newer release, prepared by the quiet check
        self.homes = (threading.Lock(), {})  # vault root -> its HomeCache
        # X-ray's dry runs and compares: one read-only Index, one request at a time (_dry_index)
        self.dry, self.dry_index, self.dry_used = threading.Lock(), None, 0.0
        self.warming: threading.Thread | None = None  # `warm_status`'s thread
        self.core = None  # (vault root, mcp_server.Keepfold) for the Review's clicks
        self.reviewing = threading.Lock()  # one Review click at a time
        self.get = {"/api/status": self.status, "/api/status/report": self.status_report,
                    "/api/connect": self.connect,
                    "/api/app/autostart": self.autostart, "/api/app/notify": self.notify,
                    "/api/app/hotkey": self.hotkey, "/api/app/welcome": self.welcome,
                    "/api/home": self.home, "/api/home/version": self.home_version,
                    "/api/home/delta": self.home_delta, "/api/home/site": self.home_site,
                    "/api/home/check": self.cold_check, "/api/import/pickup": self.pickup,
                    "/api/vault/snapshot": None,
                    "/api/llm": self.llm, "/api/browse": self.browse, "/api/vault": self.vault,
                    "/api/setup": self.setup, "/api/chat": self.chat_state,
                    "/api/chat/list": self.chat_list, "/api/chat/get": self.chat_get,
                    "/api/chat/passage": self.chat_passage, "/api/chat/pages": self.chat_pages,
                    "/api/chat/settings": self.chat_settings_get,
                    "/api/update": self.update_state, "/api/job": self.jobs.view,
                    "/api/review": self.review, "/api/xray": self.xray,
                    "/api/tonight": self.tonight,
                    "/api/browse/private-chats": self.private_chats}
        self.post = {
            "/api/connect/plan": self.connect_plan, "/api/connect/apply": self.connect_apply,
            "/api/connect/undo": self.connect_undo,
            "/api/app/move": self.app_move, "/api/app/uninstall": self.app_uninstall,
            "/api/connect/path": self.connect_path,
            "/api/connect/copied": self.pack_copied,
            "/api/profiles": self.profiles_set,
            "/api/llm/settings": self.llm_settings, "/api/llm/load": self.llm_load,
            "/api/llm/test": self.llm_test, "/api/llm/pull": self.llm_pull,
            "/api/llm/size": self.llm_size,
            "/api/browse/settings": self.browse_settings, "/api/browse/cloud": self.cloud,
            "/api/browse/auto": self.auto_settings,
            "/api/chat": None,  # do_POST streams it
            "/api/chat/delete": self.chat_delete, "/api/chat/settings": self.chat_settings,
            "/api/chat/eval": self.chat_eval,
            "/api/chat/attach": self.chat_attach, "/api/chat/file/remove": self.chat_file_remove,
            "/api/chat/file/save": self.chat_file_save,
            "/api/browse/login": self.login,
            "/api/browse/process": self.process, "/api/browse/selftest": self.selftest,
            "/api/browse/scan": self.scan, "/api/browse/import": self.history_import,
            "/api/vault/export": self.export, "/api/vault/backup": self.backup,
            PICTURE: self.brain_picture,
            "/api/home/unlock": self.unlock, "/api/home/lock": self.lock,
            "/api/home/check/answer": self.cold_answer, "/api/home/date": self.home_date,
            "/api/home/layout": self.home_layout, "/api/home/fact": self.home_fact,
            "/api/vault/doctor": self.doctor, "/api/vault/git": self.git_settings,
            "/api/config/restore": self.config_restore,
            "/api/open": self.open,
            "/api/setup/vault": self.setup_vault, "/api/setup/embed": self.setup_embed,
            "/api/setup/voice": self.setup_voice,
            "/api/setup/index": self.setup_index, "/api/setup/install": self.setup_install,
            "/api/setup/git": self.setup_git,
            "/api/setup/pull": self.setup_pull, "/api/setup/test": self.setup_test,
            "/api/setup/start": self.setup_start, "/api/setup/model": self.setup_model,
            UPLOAD: None,  # do_POST streams it
            "/api/import/preview": self.import_preview, "/api/import/run": self.import_run,
            "/api/import/pickup/add": self.pickup_add,
            "/api/import/pickup/dismiss": self.pickup_dismiss,
            "/api/import/pickup/later": self.pickup_later,
            "/api/import/watch/add": self.watch_add,
            "/api/import/watch/remove": self.watch_remove,
            "/api/job/cancel": self.cancel, "/api/quit": self.quit,
            "/api/setup/skip": self.setup_skip, "/api/update/check": self.update_check,
            "/api/update/apply": self.update_apply, "/api/whats_new/seen": self.news_seen,
            "/api/window/show": self.window_show, "/api/window/capture": self.window_capture,
            "/api/app/autostart": self.autostart_set,
            "/api/app/notify": self.notify_set, "/api/app/chat-import": self.chat_import_set,
            "/api/app/welcome": self.welcome_set, "/api/app/hotkey": self.hotkey_set,
            "/api/app/update-check": self.update_check_set,
            "/api/capture/hide": self.capture_hide,
            "/api/review/approve": self.review_approve, "/api/review/dismiss": self.review_dismiss,
            "/api/review/still": self.review_still, "/api/review/retire": self.review_retire,
            "/api/review/conflict": self.review_conflict,
            "/api/review/rule/approve": self.review_rule_approve,
            "/api/review/rule/approve-all": self.review_rule_approve_all,
            "/api/review/resume": self.review_resume,
            "/api/review/rule/dismiss": self.review_rule_dismiss,
            "/api/review/decision/approve": self.review_decision_approve,
            "/api/review/decision/dismiss": self.review_decision_dismiss,
            "/api/review/forget/dismiss": self.review_forget_dismiss,
            "/api/review/import/dismiss": self.review_import_dismiss,
            "/api/review/unsafe/keep": self.review_unsafe_keep,
            "/api/review/outside/keep": self.review_outside_keep,
            "/api/review/outside/remove": self.review_outside_remove,
            "/api/review/crowded/keep": self.review_crowded_keep,
            "/api/review/playbook/keep": self.review_playbook_keep,
            "/api/review/promote/pin": self.review_promote_pin,
            "/api/review/promote/later": self.review_promote_later,
            "/api/xray/pin": self.xray_pin, "/api/xray/mute": self.xray_mute,
            "/api/xray/dryrun": self.xray_dryrun, "/api/xray/trace": self.xray_trace,
            "/api/xray/compare": self.xray_compare, "/api/xray/ack": self.xray_ack,
            "/api/tonight/undo": self.tonight_undo, "/api/capture": self.capture}

    def touch(self) -> None:
        self.last = time.monotonic()

    def need_root(self) -> Path:
        if self.root is None or not (self.root / ".keepfold").is_dir():
            raise ValueError("Set up a vault first (Setup, step 1).")
        return self.root

    def read_only(self) -> str:
        """The banner text while the vault refuses writes (too new, unreadable stamp), else ""."""
        from keepfold import vault_format
        return READ_ONLY.get(vault_format.state(self.root).status, "") if self.root else ""

    def banner(self) -> bytes:
        text = self.read_only()
        return f'<p class="readonly" role="status">{text}</p>'.encode() if text else b""

    def entry(self) -> dict | None:
        if self.root is None or not (self.root / ".keepfold").is_dir():
            return None
        from keepfold import clients
        return clients.server_entry(clients.mcp_command(), self.root)

    def _vault(self):
        from keepfold.vault import Vault
        return Vault(self.need_root())

    def unlock(self, body: dict) -> dict:
        """The Private switch: Windows asks the user to verify (Windows Hello); private items
        then show for PRIVATE_S, until the switch is pressed again or the window minimised."""
        from keepfold.app import hello
        ok, why = hello.verify()
        if ok:
            self.private_until = time.monotonic() + PRIVATE_S
        return {"unlocked": ok, "message": why}

    @staticmethod
    def _today() -> str:
        from datetime import UTC, datetime

        from keepfold.events import local_day
        return local_day(datetime.now(UTC))

    def piggyback(self) -> threading.Thread | None:
        """C8: after a Chat turn ends, a daemon thread waits fact_harvest.PIGGY_WAIT_S and then
        runs fact_harvest.after_chat, unless the app is quitting or a new Chat turn started
        meanwhile. At most one at a time (`piggy`); it never holds `chatting`. The thread, or
        None when one already runs."""
        if not PIGGYBACK or not self.piggy.acquire(blocking=False):
            return None

        def go():
            from keepfold import fact_harvest
            try:
                if self.stop.wait(fact_harvest.PIGGY_WAIT_S) or self.chatting.locked():
                    return  # quitting, or the owner asked again: that turn's end tries again
                c = fact_harvest.after_chat(self)
                if c:  # counts only: never a page's text
                    self.echo("keepfold app: facts from chats after a Chat turn: " + ", ".join(
                        f"{k} {n}" for k, n in sorted(c.items()) if k != "session"))
            except Exception as e:  # noqa: BLE001  (the next turn or chats pass tries again)
                self.echo(f"keepfold app: saving facts after a Chat turn failed "
                          f"({type(e).__name__})")
            finally:
                self.piggy.release()

        t = threading.Thread(target=go, daemon=True, name="fact-piggyback")
        try:
            t.start()
        except RuntimeError:  # no thread to spare: never keep `piggy` held
            self.piggy.release()
            return None
        return t

    def snapshot(self) -> bytes:
        """The self-contained snapshot page (dashboard.snapshot: CSP default-src 'none', no
        private item)."""
        from datetime import UTC, datetime

        from keepfold import dashboard
        return dashboard.snapshot(self._vault(), now=datetime.now(UTC)).encode()

    def export(self, body: dict) -> dict:
        """Export snapshot. The desktop app asks where to save it (a save dialog) and writes it
        as a job; the browser downloads it from GET /api/vault/snapshot."""
        self.need_root()
        win = self.desktop.win if self.desktop is not None else None
        if win is None:
            return {"download": "/api/vault/snapshot"}
        import webview
        got = win.create_file_dialog(webview.FileDialog.SAVE, save_filename=snapshot_name(),
                                     file_types=("Web page (*.html)",))
        if not got:
            return {"cancelled": True}
        dest = Path(got if isinstance(got, str) else got[0])

        def run(job):
            job.say("building the snapshot")
            dest.write_bytes(self.snapshot())  # the owner's chosen file, outside the vault
            return {"ok": True, "path": str(dest)}
        return self.jobs.start("Exporting a snapshot", "vault", run)

    def brain_picture(self, body: dict) -> dict:
        """Save picture: the brain page's PNG (`png`, base64; `day`, the page's day, names it).
        The desktop app asks where to save it (Downloads first) and writes it there; the browser
        app answers `download` and the page saves it itself. Never written anywhere else."""
        import base64
        import binascii
        bad = ValueError("That picture could not be saved.")
        b64 = body.get("png")
        if not isinstance(b64, str) or len(b64) > (PICTURE_MAX + 2) // 3 * 4:
            raise bad
        try:
            png = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise bad from None
        if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) > PICTURE_MAX:
            raise bad
        win = self.desktop.win if self.desktop is not None else None
        if win is None:
            return {"download": True}
        day = body.get("day")
        if not (isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)):
            day = time.strftime("%Y-%m-%d")
        import webview
        got = win.create_file_dialog(webview.FileDialog.SAVE, directory=str(exports.downloads()),
                                     save_filename=f"keepfold-brain-{day}.png",
                                     file_types=("PNG picture (*.png)",))
        if not got:
            return {"cancelled": True}
        dest = Path(got if isinstance(got, str) else got[0])
        try:
            dest.write_bytes(png)  # the owner's chosen file, outside the vault
        except OSError:  # a read-only folder, a full disk
            raise bad from None
        return {"saved": dest.name}

    def open(self, body: dict) -> dict:
        what = body.get("what")
        if what == "page":  # a source of an answer: a Markdown page inside the vault
            root, rel = self.need_root(), body.get("path")
            p = (root / rel).resolve() if isinstance(rel, str) and rel else None
            if p is None or Path(rel).is_absolute() or not p.is_relative_to(root) \
                    or p.suffix != ".md" or not p.is_file() \
                    or p.relative_to(root).parts[0].startswith("."):
                raise ValueError("not a page in this vault")
            return _open_note(p)
        if what == "skill_zip":
            p = _checkout() / "dist"
            if not p.is_dir():
                raise ValueError(f"{p} is not there; build it first.")
            _open_path(p)
            return {"ok": True}
        root = self.need_root()
        if what in ("vault", "private"):
            from keepfold.vault import PRIVATE_BROWSE_DIR
            p = root / PRIVATE_BROWSE_DIR / "browse-private-index.md" if what == "private" \
                else root
            if what == "private" and not p.is_file():
                p = p.parent
                if not p.is_dir():
                    raise ValueError("There are no private browse pages yet.")
            return _open_note(p)
        elif what == "folder":  # the plain folder, in Explorer or Finder
            _open_path(root)
        else:
            raise ValueError("unknown thing to open")
        return {"ok": True}


# ---- HTTP -----------------------------------------------------------------------------------


def _handler(app: App):
    class H(BaseHTTPRequestHandler):
        server_version = "keepfold-app"
        sys_version = ""
        timeout = 30

        def log_message(self, *a):  # nothing from requests is logged
            pass

        def _send(self, code: int, body, ctype: str = "application/json", extra=None):
            if self.command not in ("GET", "HEAD") and not getattr(self, "_drained", False):
                self._drained = True  # Windows resets a socket closed with unread data
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    if 0 < n <= MAX_BODY:
                        self.rfile.read(n)
                except (ValueError, OSError):
                    pass
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            try:
                self._head(code, ctype, len(data), extra)
                self.wfile.write(data)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                self.close_connection = True  # the browser left mid-answer: nothing to tell it

        def _head(self, code: int, ctype: str, length: int | None, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.end_headers()

        def _redirect(self, where: str):
            self.send_response(302)
            self.send_header("Location", where)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()

        def _gate(self, query_token: str | None = None, need_token: bool = True) -> bool:
            hosts = {f"{HOST}:{app.port}", f"localhost:{app.port}"}
            if self.headers.get("Host") not in hosts:  # DNS rebinding
                self._send(403, {"error": "host"})
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {f"http://{h}" for h in hosts}:
                self._send(403, {"error": "origin"})
                return False
            # a browser says where a request comes from; same-origin, none (the address bar) and
            # no header at all (the desktop window, older browsers) stay allowed
            site = (self.headers.get("Sec-Fetch-Site") or "").lower()
            if self.command == "POST" and site == "cross-site":
                self._send(403, {"error": "origin"})
                return False
            if not need_token:  # the static page shell holds no data; the API needs the key
                return True
            given = self.headers.get("X-Keepfold-Token") or query_token or ""
            if not hmac.compare_digest(given.encode(), app.token.encode()):
                if self.command == "GET" and urlsplit(self.path).path == "/":
                    self._send(401, EXPIRED_INSTALLED if update.mode() == "installer"
                               else EXPIRED, "text/html; charset=utf-8")
                else:
                    self._send(401, {"error": "token"})
                return False
            app.touch()
            return True

        def do_GET(self):
            url = urlsplit(self.path)
            static = STATIC.get(url.path)
            qt = (parse_qs(url.query).get("t") or [None])[0] if static else None
            if not self._gate(qt, need_token=not static):
                return
            if static:
                path, ctype = static
                # the key goes only into a page opened with it
                ok = bool(qt) and hmac.compare_digest(qt.encode(), app.token.encode())
                key = app.token if ok else ""
                if path is None:
                    try:
                        app.need_root()
                    except ValueError:  # first run: no vault yet, so Setup
                        self._redirect("/manage" + (f"?t={key}" if key else "") + "#setup")
                        return
                    from keepfold import dashboard
                    data = dashboard.render_live(key).encode().replace(
                        b"<body>", b"<body>" + app.banner(), 1)
                else:
                    data = path.read_bytes()
                if url.path in ("/manage", "/capture"):
                    data = data.replace(b"{{T}}", key.encode()) \
                        .replace(b"{{REOPEN}}", reopen().encode())
                if url.path == "/manage":
                    data = data.replace(b"{{READONLY}}", app.banner(), 1)
                self._send(200, data, ctype)
            elif url.path == "/api/vault/snapshot":  # Export snapshot in the browser: a download
                self._run(app.snapshot, ctype="text/html; charset=utf-8", extra={
                    "Content-Disposition": f'attachment; filename="{snapshot_name()}"'})
            elif url.path in app.get:
                self._run(app.get[url.path],
                          *([parse_qs(url.query)] if url.path in QUERY_GETS else []))
            elif url.path in app.post:
                self._send(405, {"error": "use POST"})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            path = urlsplit(self.path).path
            if not self._gate():
                return
            if path not in app.post:
                self._send(405 if path in app.get or path in STATIC else 404,
                           {"error": "not found"})
                return
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if path == UPLOAD:
                self._upload(ctype)
                return
            if ctype != "application/json":
                self._send(415, {"error": "Content-Type must be application/json"})
                return
            try:
                n = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._send(411, {"error": "length"})
                return
            if not 0 <= n <= (PICTURE_MAX * 3 // 2 if path == PICTURE else MAX_BODY):
                self.close_connection = True
                self._send(413, {"error": "too large"})
                return
            self._drained = True
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, UnicodeDecodeError, RecursionError):
                body = None
            if not isinstance(body, dict):
                self._send(400, {"error": "the body must be a JSON object"})
                return
            if path == "/api/chat":
                self._chat(body)
                return
            status.forget()  # an action: Status asks its slow probes again
            self._run(app.post[path], body)

        def _chat(self, body: dict):
            """Stream one chat turn as NDJSON on this thread (not a job: chat works while an
            import runs). The client going away sets cancel; the partial answer is kept."""
            from keepfold import chat, localllm
            try:
                vault, cid, text = app.chat_begin(body)
            except Busy as e:
                self._send(409, {"error": str(e)})
                return
            except ValueError as e:
                self._send(400, {"error": str(e)[:500]})
                return
            cancel, ended, wlock = threading.Event(), threading.Event(), threading.Lock()

            def emit(ev: dict):
                if cancel.is_set():
                    return
                app.touch()
                with wlock:  # the keepalive thread writes too
                    if ended.is_set():
                        return
                    try:
                        self.wfile.write(json.dumps(ev).encode() + b"\n")
                        self.wfile.flush()
                    except OSError:  # BrokenPipe, ConnectionReset/Aborted: the page stopped
                        cancel.set()
                    if ev.get("t") in ("done", "err"):
                        ended.set()

            def keepalive():
                # a closed page shows only on a write: while the model reads or reasons quietly,
                # write now and then, so Stop ends the turn (and frees the slot) early
                while not ended.wait(KEEPALIVE_S):
                    emit({"t": "wait"})
            try:
                self.close_connection = True  # no length: the end of the answer ends the body
                self._head(200, "application/x-ndjson", None)
                threading.Thread(target=keepalive, daemon=True, name="chat-keepalive").start()
                # local only, never cloudllm; looked up only when the turn needs the model. An
                # attached file (C1) is asked like any question: the person asked, so the
                # configured local model may start, as for every Chat turn
                files = app.files.get(body.get("files"))
                chat.reply(vault, cid, text, localllm.find, emit, cancel, files=files,
                           pages=body.get("pages"))  # UX-B: ids picked with "#"
            except Exception as e:  # noqa: BLE001  (the type only: a message could quote a page)
                emit({"t": "err", "code": "internal",
                      "message": f"internal error ({type(e).__name__})"})
            finally:
                ended.set()
                app.chatting.release()
                app.piggyback()  # C8: the model is warm; a short fact-harvest pass

        def _upload(self, ctype: str):
            """Stream an export to ~/.keepfold/tmp/upload-<id>-<stem><ext>, owner-only (0600).
            Of the client's name (X-Keepfold-Name) only the extension and up to 40 letters,
            digits, combining marks, _ and - of its stem are kept: `importers.detect` reads
            names such as MyActivity.json. The size is capped at MAX_UPLOAD."""
            self.close_connection = True  # a refused body is left unread
            if ctype != "application/octet-stream":
                self._send(415, {"error": "Content-Type must be application/octet-stream"})
                return
            raw = unquote(self.headers.get("X-Keepfold-Name", ""))
            name = PurePosixPath(raw.replace("\\", "/"))
            ext = name.suffix.lower()
            if ext not in UPLOAD_EXT:
                self._send(400, {"error": "Add the export as it came: a .zip, .json, .html, .md, "
                                          ".txt, .ics, .mbox, .pdf, .docx, .apkg or .colpkg "
                                          "file."})
                return
            # letters, digits and combining marks of any script (Devanagari vowel signs are
            # marks), so takeout.activity_file still knows MeineAktivitäten or मेरीगतिविधि
            stem = "".join(c for c in unicodedata.normalize("NFC", name.stem)
                           if c.isalnum() or c in "_-"
                           or unicodedata.category(c)[0] == "M")[:40]
            try:
                n = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._send(411, {"error": "length"})
                return
            if not 0 < n <= MAX_UPLOAD:
                self._send(413, {"error": "The file is larger than 4 GB; unzip it and paste "
                                          "the path of the file inside instead."})
                return
            self._drained = True
            d = actions.tmp_dir()
            d.mkdir(mode=0o700, parents=True, exist_ok=True)  # Windows: the profile's own ACL
            uid = secrets.token_hex(8)
            p, left = d / f"upload-{uid}{'-' * bool(stem)}{stem}{ext}", n
            try:
                fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                             0o600)
                with os.fdopen(fd, "wb") as f:
                    while left:
                        chunk = self.rfile.read(min(left, 1 << 20))
                        if not chunk:
                            raise OSError("short body")
                        f.write(chunk)
                        left -= len(chunk)
            except OSError:
                p.unlink(missing_ok=True)
                self._send(400, {"error": "The upload stopped before the end; try again."})
                return
            app.uploads[uid] = p
            self._send(200, {"upload": uid, "size": n})

        def _run(self, fn, *args, ctype: str = "application/json", extra=None):
            try:
                self._send(200, fn(*args), ctype, extra)
            except Busy as e:
                self._send(409, {"error": str(e)})
            except NotFound:
                self._send(404, {"error": "not found"})
            except ValueError as e:
                self._send(400, {"error": str(e)[:500]})
            except Exception as e:  # noqa: BLE001  (the type only: a message could quote a page)
                self._send(500, {"error": f"internal error ({type(e).__name__})"})

        def _refuse(self):
            self._send(405, {"error": "method"})

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse  # noqa: N815

    return H


MAX_CONNECTIONS = 32  # open connections at once: one thread each


class _Server(ThreadingHTTPServer):
    """No port sharing: Windows binds a used port without SO_EXCLUSIVEADDRUSE, and the token
    would go to its owner. At most MAX_CONNECTIONS connections at once: a local program opening
    thousands of idle ones would otherwise start a thread for each; one over the cap is closed
    at once. Each handler's `timeout` bounds how long an idle one keeps its slot."""
    # POSIX SO_REUSEADDR only skips TIME_WAIT (a quick reopen keeps port 47615); on Windows
    # it would let a second listener take the live port
    allow_reuse_address = os.name != "nt"

    def __init__(self, *a, **kw):
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(*a, **kw)

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:  # no thread started, so none will release the slot
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_server(root: Path | None, port: int = 0) -> tuple[ThreadingHTTPServer, App]:
    """The app's server on 127.0.0.1 only (port 0 picks a free one)."""
    app = App(root)
    actions.sweep_uploads()  # left by an app that was killed before its `finally` ran
    actions.sweep_updates(update.prepared())  # installers an update already ran; a newer one
    # downloaded and still matching its checksum stays for Restart to update
    srv = _Server((HOST, port), _handler(app))
    srv.daemon_threads = True
    app.port = srv.server_address[1]
    return srv, app


def state_file() -> Path:
    return Path.home() / ".keepfold" / "app.json"


def _save_state(app: App) -> None:
    """~/.keepfold/app.json {port, token, pid}, owner-only: a second launch opens this app."""
    p = state_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    # owner-only: atomic.replace's temp file is created 0600 (POSIX)
    atomic.replace(p, json.dumps({"port": app.port, "token": app.token, "pid": os.getpid()}),
                   newline=None)


def _drop_state(app: App) -> None:
    try:
        if json.loads(state_file().read_text(encoding="utf-8")).get("token") == app.token:
            state_file().unlink()
    except (OSError, ValueError, AttributeError):
        pass


def running_url() -> str | None:
    """The link of a Keepfold app that runs now (app.json names it and it answers with that
    key), else None: a stale or unreadable file is ignored."""
    import http.client
    try:
        s = json.loads(state_file().read_text(encoding="utf-8"))
        port, token = int(s["port"]), str(s["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    c = http.client.HTTPConnection(HOST, port, timeout=2)
    try:
        c.request("GET", "/api/job", headers={"Host": f"{HOST}:{port}", "X-Keepfold-Token": token})
        ok = c.getresponse().status == 200
    except OSError:
        return None
    finally:
        c.close()
    return f"http://{HOST}:{port}/?t={token}" if ok else None


def show_window(url: str) -> bool:
    """Ask the app at `url` (from running_url) to bring its window up; False when it has no
    window (the browser app) or does not answer."""
    return _ask_shown(url, "/api/window/show")


def show_capture(url: str) -> bool:
    """Ask the app at `url` to show its quick capture box; False as for show_window."""
    return _ask_shown(url, "/api/window/capture")


def _ask_shown(url: str, path: str) -> bool:
    import http.client
    u = urlsplit(url)
    token = (parse_qs(u.query).get("t") or [""])[0]
    c = http.client.HTTPConnection(HOST, u.port, timeout=10)
    try:
        c.request("POST", path, body=b"{}", headers={
            "Host": u.netloc, "X-Keepfold-Token": token, "Content-Type": "application/json"})
        r = c.getresponse()
        return r.status == 200 and json.loads(r.read()).get("shown") is True
    except (OSError, ValueError):
        return False
    finally:
        c.close()


def start_url(app: App, now=None) -> str:
    """The page the app opens on when it starts: Home, or Tonight from 20:00 until the Keepfold
    day rolls over at 04:00 local time (the owner's choice), once a vault exists. `now`: a
    naive local datetime (tests); later navigation is never moved."""
    from datetime import datetime

    from keepfold.events import DAY_ROLLOVER
    h = (now or datetime.now()).hour
    night = (h >= 20 or h < DAY_ROLLOVER.seconds // 3600) and app.root is not None \
        and (app.root / ".keepfold").is_dir()
    return f"http://{HOST}:{app.port}/" + (f"manage?t={app.token}#tonight" if night
                                           else f"?t={app.token}")


def start(root: Path | None, *, port: int = 0, echo=print, idle: bool = True,
          ) -> tuple[ThreadingHTTPServer, App, str]:
    """The app's server, app.json and its watch and auto-import threads, not yet serving: the
    caller runs `srv.serve_forever` and then `finish`. `idle`: stop after 30 minutes with no
    request and no job (the browser app; the desktop app runs until Quit). No `port`: PORT if
    it is free, else any free one."""
    try:
        srv, app = make_server(root, port or PORT)
    except OSError as e:
        if port:
            raise SystemExit(f"Port {port} is in use or not allowed ({e.strerror or e}). Pick "
                             "another, or leave --port out for a free one.") from None
        srv, app = make_server(root, 0)  # PORT is taken (a second Keepfold, another program)
    app.echo = echo
    url = start_url(app)
    try:
        _save_state(app)
    except OSError:  # no single-instance file: a second launch starts its own app
        _log_exc()
    try:
        news.start()  # a fresh install remembers this version, so it shows no What's new
    except Exception:  # noqa: BLE001  (an unwritable config only means no card; the app opens)
        _log_exc()
    try:
        actions.migrate_login(app.root if app.entry() else None)
    except Exception:  # noqa: BLE001  (Start at login stays as it was; the app still opens)
        _log_exc()
    if app.entry() is not None:
        try:  # D7 §1.3: a vault with no format stamp gets one (Vault() moved a .cortex/ first)
            from keepfold import vault_format
            if vault_format.AUTO_STAMP and vault_format.writable(app.root):
                vault_format.write_stamp(app._vault())
        except Exception:  # noqa: BLE001  (a too_new or busy vault: stamped at the next start)
            _log_exc()
        try:
            from keepfold import dashboard
            dashboard.retire_old(app._vault())  # the old .keepfold/dashboard.html, once
        except Exception:  # noqa: BLE001  (it is moved at the next start; the app still opens)
            _log_exc()
        try:
            from keepfold.browse import legacy
            legacy.migrate(app._vault())  # the browse files from before the store, once
        except Exception:  # noqa: BLE001  (tried again at the next start or import)
            _log_exc()

    def watch():
        while not app.stop.wait(WATCH_S):
            if idle and time.monotonic() - app.last > IDLE_S and not app.jobs.running():
                echo("keepfold app: 30 minutes idle; stopping.")
                break
            app.drop_dry()
        app.stop.set()
        srv.shutdown()

    def forget_due():  # R10: a //forget's sweep, queued by a hook or keepfold_prime
        if app.read_only():  # D7 §2.6: no background pass writes a vault that refuses writes
            return
        try:
            app.forget_due()
        except Exception as e:  # noqa: BLE001  (the next pass tries again)
            echo(f"keepfold app: forgetting a //forget line failed ({type(e).__name__})")

    def auto():  # the live import: first after a minute, then every 10
        from keepfold import browse_serve
        wait = browse_serve.AUTO_FIRST_S
        forget_due()  # once at start, then with each pass
        while not app.stop.wait(wait):
            wait = browse_serve.AUTO_EVERY_S
            if app.read_only():
                continue
            try:
                app.auto_import()
            except Exception as e:  # noqa: BLE001  (the next check tries again)
                echo(f"keepfold app: live import failed ({type(e).__name__}); retried in "
                     f"{browse_serve.AUTO_EVERY_S // 60} minutes")
            try:  # R18: the watched folders' new and changed files; stats only when none
                app.watch_auto()
            except Exception as e:  # noqa: BLE001  (the next pass tries again)
                echo(f"keepfold app: watched folders failed ({type(e).__name__})")
            forget_due()
            try:  # the rollup pages after the import; a stamp check when nothing changed
                if app.root is not None and (app.root / ".keepfold").is_dir() \
                        and not app.jobs.running():
                    from keepfold import rollups
                    rollups.run(app._vault())
            except Exception as e:  # noqa: BLE001  (the next pass tries again)
                echo(f"keepfold app: rollup pages failed ({type(e).__name__})")

    def tidy():  # D1-D3: the about-me sweep, at start and with each chat-import pass
        if app.read_only():
            return
        try:
            app.tidy_about_me()
        except Exception as e:  # noqa: BLE001  (the next pass tries again)
            echo(f"keepfold app: tidying the about-me lines failed ({type(e).__name__})")

    def chats():  # app.chat_import: the first pass a minute after start, then every 5 minutes
        from keepfold import chat_import
        wait = chat_import.FIRST_S
        tidy()
        while not app.stop.wait(wait):
            wait = chat_import.EVERY_S
            if app.read_only():  # imports, harvest, rules refresh: all write
                continue
            tidy()
            # the safety net reads the pages the last import wrote; first, since the import
            # below starts a job and the net waits while any job runs
            try:
                app.safety_net_auto()
            except Exception as e:  # noqa: BLE001  (the next pass tries again)
                echo(f"keepfold app: the chat safety net failed ({type(e).__name__}); retried in "
                     f"{chat_import.EVERY_S // 60} minutes")
            try:  # facts the owner stated in those chats, saved as about-me guesses
                from keepfold import fact_harvest
                fact_harvest.auto(app)
            except Exception as e:  # noqa: BLE001  (the next pass tries again)
                echo(f"keepfold app: saving facts from chats failed ({type(e).__name__})")
            try:
                app.chat_import_auto()
            except Exception as e:  # noqa: BLE001  (the next pass tries again)
                echo(f"keepfold app: automatic chat import check failed ({type(e).__name__})")
            try:  # plan v9 item 8: the Keepfold block in agents' rules files follows the core
                targets.refresh_rules(app.entry())
            except Exception as e:  # noqa: BLE001  (the next pass tries again)
                echo(f"keepfold app: rules block refresh failed ({type(e).__name__})")

    def updates():  # the quiet update check: a minute after start, then every 6 hours
        wait = UPDATE_FIRST_S
        while not app.stop.wait(wait):
            wait = UPDATE_EVERY_S
            try:
                app.update_quietly()
            except Exception as e:  # noqa: BLE001  (offline, GitHub busy: the next check tries)
                echo(f"keepfold app: the update check failed ({type(e).__name__})")

    threading.Thread(target=watch, daemon=True).start()
    threading.Thread(target=auto, daemon=True, name="keepfold-app-auto-import").start()
    threading.Thread(target=chats, daemon=True, name="keepfold-app-chat-import").start()
    threading.Thread(target=updates, daemon=True, name="keepfold-app-updates").start()
    if WARM_STATUS:
        app.warm_status()
        if app.entry() is not None:  # the first page: its brain built before the window asks
            threading.Thread(target=app.warm_home, daemon=True,
                             name="keepfold-app-warm-home").start()
    return srv, app, url


def finish(srv: ThreadingHTTPServer, app: App) -> None:
    """After serving: app.json and unused uploads go; the port closes."""
    app.stop.set()
    _drop_state(app)
    for uid in list(app.uploads):  # exports added but never imported
        app._drop(uid)
    srv.server_close()


def run(root: Path | None, *, open_browser: bool = True, port: int = 0, echo=print) -> None:
    """Serve until Quit, Ctrl+C or 30 minutes with no request and no job."""
    srv, app, url = start(root, port=port, echo=echo)
    # the key (?t=) only when the link is the output (--no-open): logs get shared
    echo(f"Keepfold app: {url if not open_browser else url.split('?')[0]}")
    echo("It runs on this computer only. Close it with Quit on the page or Ctrl+C here.")
    if open_browser:
        _open(url)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        finish(srv, app)
