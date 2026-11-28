"""B2-b: a chat export the owner downloaded, noticed in Downloads and offered on Home.

Claude Desktop and claude.ai chats have no transcript on disk; the account's data export is
the complete copy. The app looks at the .zip files directly in Downloads (a stat, then the
zip's member NAMES only: no member is opened before the owner clicks Add chats), at most once
a minute, and only while `app.chat_import` is on. What was offered and answered lives in
~/.keepfold/desktop.json (`desktop.save_state`), keyed by path, mtime and size, so a zip
downloaded again under the same name counts as new.

T11: the card shows the site the zip came from (its Mark of the Web, `where_from`), and a zip
whose mark names a site other than the service its member names claim is never offered.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

KEY = "export_pickup"  # desktop.json: {"seen": {path: [mtime, size, "added"|"dismissed"]},
#                        "later": ISO time the reminder stays hidden until, "claude": last Add}
SCAN_S = 60  # Home polls every 3 s: the folder is looked at once a minute at most
DAYS = 30
MAX_ZIPS = 20
KEEP_SEEN = 50
NAMES = {"claude": "claude.ai", "chatgpt": "ChatGPT", "gemini": "Google",
         "google-activity": "Google"}
# T11: the sites (or their subdomains) each service's export downloads from. Not
# googleusercontent.com or googleapis.com: anyone can host a file there.
HOSTS = {"claude": ("claude.ai", "anthropic.com"),
         "chatgpt": ("chatgpt.com", "openai.com", "oaiusercontent.com"),
         "gemini": ("google.com",), "google-activity": ("google.com",)}

_lock = threading.Lock()
_cache: dict = {"at": None, "offer": None}
_kinds: dict = {}  # (path, mtime, size) -> source or None: a zip's names are read once


def downloads() -> Path:
    """The Downloads folder: KEEPFOLD_DOWNLOADS_DIR (tests), else the Windows known folder (it
    may have been moved), else on Linux XDG_DOWNLOAD_DIR (a localized name such as ~/Загрузки),
    else ~/Downloads."""
    if env := os.environ.get("KEEPFOLD_DOWNLOADS_DIR"):
        return Path(env)
    if sys.platform == "win32":
        try:
            return Path(_known_downloads())
        except (OSError, AttributeError, ValueError):
            pass
    elif sys.platform != "darwin" and (xdg := _xdg_downloads()) is not None:
        return xdg
    return Path.home() / "Downloads"


def _xdg_downloads() -> Path | None:
    """XDG_DOWNLOAD_DIR from user-dirs.dirs (`XDG_DOWNLOAD_DIR="$HOME/Загрузки"`), or None."""
    cfg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "user-dirs.dirs"
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r'^\s*XDG_DOWNLOAD_DIR\s*=\s*"([^"]*)"', text, re.M)
    if not m:
        return None
    v = m[1]
    if v == "$HOME" or v.startswith("$HOME/"):
        return Path.home() / v[6:]
    return Path(v) if v.startswith("/") else None  # the spec allows only these two forms


def _known_downloads() -> str:
    """SHGetKnownFolderPath(FOLDERID_Downloads)."""
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD),
                    ("d", ctypes.c_ubyte * 8)]
    u = uuid.UUID("374DE290-123F-4565-9164-39C4925E467B")
    g = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
    out = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)):
        raise OSError("no Downloads known folder")
    try:
        return out.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


def kind_of(names: list[str]) -> str | None:
    """The guided-import source of an export, from its member names alone. A name-only twin
    of `importers.detect`, which reads file heads (never done before the owner's click)."""
    from keepfold.browse import takeout
    low = [n.replace("\\", "/").lower() for n in names]
    base = {n.rsplit("/", 1)[-1] for n in low}
    if "conversations.json" in base:
        if base & {"chat.html", "message_feedback.json"}:
            return "chatgpt"
        if base & {"users.json", "projects.json"}:
            return "claude"
    if any(n.startswith("takeout/") for n in low):
        acts = [n for n in low if takeout.activity_file(n)]
        if any("gemini" in n for n in acts):
            return "gemini"
        if any(takeout.wanted(n) for n in acts):  # Search, YouTube or Chrome activity
            return "google-activity"
    return None


def where_from(p: Path) -> list[str]:
    """The hosts in the zip's Mark of the Web, HostUrl's first, then ReferrerUrl's (a download
    a service's page sends on to a file host); [] when there is none. Windows: the
    Zone.Identifier stream the browser wrote. macOS: none (its where-froms and quarantine
    xattrs need os.getxattr, which the stdlib has only on Linux). Linux: none."""
    if sys.platform != "win32":
        return []
    try:
        with open(f"{p}:Zone.Identifier", encoding="utf-8", errors="replace") as f:
            text = f.read(4096)
    except OSError:
        return []
    out: list[str] = []
    for k in ("HostUrl", "ReferrerUrl"):
        m = re.search(rf"^[ \t]*{k}[ \t]*=[ \t]*(\S+)", text, re.M | re.I)
        try:
            host = m and urlsplit(m[1]).hostname  # about:internet has none
        except ValueError:
            host = None
        if host and host not in out:
            out.append(host)
    return out


def trusted(source: str, hosts: list[str]) -> bool:
    """No mark, or one of its hosts is the service `source` claims (HOSTS)."""
    return not hosts or any(h == s or h.endswith("." + s) for h in hosts for s in HOSTS[source])


def _kind(p: Path, mtime: float, size: int) -> str | None:
    k = (str(p), mtime, size)
    if k not in _kinds:
        try:
            with zipfile.ZipFile(p) as z:
                _kinds[k] = kind_of(z.namelist())
        except (OSError, zipfile.BadZipFile, ValueError):
            _kinds[k] = None
    return _kinds[k]


def _state() -> dict:
    from keepfold.app import desktop
    s = desktop.load_state().get(KEY)
    return s if isinstance(s, dict) else {}


def _save(fn) -> None:
    from keepfold.app import desktop
    with _lock:
        s = fn(_state())
        seen = s.get("seen") or {}
        s["seen"] = dict(list(seen.items())[-KEEP_SEEN:])
        desktop.save_state(KEY, s)
    forget()


def forget() -> None:
    _cache["at"] = None


def _imported_after(source: str, mtime: float) -> bool:
    """A chat source's folder changed after the zip landed: it was imported (the Import page
    too, which records no path)."""
    from keepfold import chats
    if source not in chats.EXPORT_DIRS:
        return False
    d = chats.export_root() / chats.EXPORT_DIRS[source][0]
    try:
        return any(d.glob("*.jsonl")) and d.stat().st_mtime >= mtime
    except OSError:
        return False


def candidates(now: float | None = None) -> list[dict]:
    """The new exports in Downloads, newest first: {path, source, mtime, size}."""
    now = time.time() if now is None else now
    try:
        zips = [(p, p.stat()) for p in downloads().iterdir()
                if p.suffix.lower() == ".zip" and p.is_file()]
    except OSError:
        return []
    zips = sorted(((p, st) for p, st in zips if now - st.st_mtime <= DAYS * 86400),
                  key=lambda x: x[1].st_mtime, reverse=True)[:MAX_ZIPS]
    seen = _state().get("seen") or {}
    out = []
    for p, st in zips:
        was = seen.get(str(p))
        if isinstance(was, list) and was[:2] == [st.st_mtime, st.st_size]:
            continue
        source = _kind(p, st.st_mtime, st.st_size)
        if source and not _imported_after(source, st.st_mtime) and trusted(
                source, hosts := where_from(p)):
            out.append({"path": str(p), "source": source, "mtime": st.st_mtime,
                        "size": st.st_size, "host": hosts[0] if hosts else None})
    return out


def _claude_recent(now: float) -> bool:
    """A claude.ai export was imported in the last DAYS days (its folder, or an Add here)."""
    from keepfold import chats
    s = _state()
    try:
        if now - datetime.fromisoformat(s.get("claude", "")).timestamp() <= DAYS * 86400:
            return True
    except (TypeError, ValueError):
        pass
    d = chats.export_root() / chats.EXPORT_DIRS["claude"][0]
    try:
        return any(d.glob("*.jsonl")) and now - d.stat().st_mtime <= DAYS * 86400
    except OSError:
        return False


def _remind(now: float) -> bool:
    """The claude.ai reminder: Claude Desktop is connected, no claude.ai export came in the
    last DAYS days, and Later was not clicked in that time."""
    from keepfold.app import targets
    try:
        if datetime.fromisoformat(_state().get("later", "")).timestamp() > now:
            return False
    except (TypeError, ValueError):
        pass
    try:
        on = targets.ClaudeDesktop().connected()
    except (OSError, ValueError):
        on = False
    return on and not _claude_recent(now)


def offer() -> dict:
    """Home's card: {"found": the newest new export or None, "remind": bool}; the found card
    wins. Nothing while the chat import setting is off. Cached for SCAN_S."""
    from keepfold import chat_import
    if not chat_import.enabled():
        return {"found": None, "remind": False}
    t = time.monotonic()
    if _cache["at"] is not None and t - _cache["at"] < SCAN_S:
        return _cache["offer"]
    now = time.time()
    c = candidates(now)
    found = c[0] if c else None
    out = {"found": found and {
        "path": found["path"], "file": Path(found["path"]).name, "source": found["source"],
        "name": NAMES[found["source"]], "host": found["host"],
        "day": datetime.fromtimestamp(found["mtime"]).date().isoformat()},
        "remind": not found and _remind(now)}
    _cache.update(at=t, offer=out)
    return out


def pick(path) -> dict:
    """The offered export at `path` (only a current candidate: never any path a page names)."""
    from keepfold import chat_import
    forget()
    for c in candidates() if chat_import.enabled() else []:
        if c["path"] == path:
            return c
    raise ValueError("That file is no longer offered. Import it from Settings, Add your "
                     "history.")


def mark(c: dict, was: str) -> None:
    """Remember the Add or Not this one answer for this path, mtime and size."""
    stamp = datetime.now(UTC).isoformat()

    def fn(s):
        seen = {k: v for k, v in (s.get("seen") or {}).items() if k != c["path"]}
        seen[c["path"]] = [c["mtime"], c["size"], was]
        return {**s, "seen": seen,
                **({"claude": stamp} if was == "added" and c["source"] == "claude" else {})}
    _save(fn)


def later() -> None:
    """Hide the claude.ai reminder for DAYS days."""
    until = (datetime.now(UTC) + timedelta(days=DAYS)).isoformat()
    _save(lambda s: {**s, "later": until})
