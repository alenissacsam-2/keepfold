"""System: Status, updates, backup, doctor, git, the app's switches (autostart, notifications,
hotkey), job cancel and Quit.

Pure moves out of `App` (server.py): the methods keep their bodies; `App` inherits this
mixin and its get/post tables still bind `self.<name>`."""

from __future__ import annotations

import json
import os
import sys
import threading

from keepfold import update
from keepfold.app import actions, news, status, targets
from keepfold.app.routes._common import _bool, _connect_skipped


class SystemRoutes:
    def status(self, q: dict | None = None) -> dict:
        """The Status board; its slow probes are `status.kept` (`?fresh=1`: asked again)."""
        from keepfold import chat_import, egress, taint
        if self.warming not in (None, threading.current_thread()):
            self.warming.join()  # its probes are already under way: wait for them, not ask twice
        if (q or {}).get("fresh") == ["1"]:
            status.forget()
        v = status.vault(self.root)
        entry = self.entry() if v["ok"] else None
        ready = v["ok"] and (_connect_skipped() or any(
            t["connected"] for t in status.kept(("targets", json.dumps(entry, sort_keys=True)),
                                                lambda: targets.describe(entry))))
        bad = taint.config_error()  # privacy choices fail closed until it is fixed
        return {"rows": ([{"id": "config", "label": "Settings file", "state": "warn",
                           "value": bad, "detail": taint.CONFIG_FIX,
                           **({"fix": {"post": "/api/config/restore",
                                       "label": "Restore the last good copy"}}
                              if taint.restorable() else {})}] if bad else [])
                + status.rows(self.root) + (self._paused_rows() if v["ok"] else []),
                "first_run": not v["ok"], "vault": v["path"], "update": self.update_state(),
                "whats_new": news.card(), "chat_import": chat_import.enabled(),
                "issue_url": f"https://github.com/{update.REPO}/issues/new?template=bug_report.yml",
                "connects": egress.summary(),  # keepfold.egress: where Keepfold connects
                "land": "status" if ready else "setup"}  # Setup until its key steps are done

    def status_report(self) -> dict:
        """Copy details: the same block as `keepfold doctor --report` (no paths, notes, values)."""
        return {"text": status.report(self.root)}

    def _paused_rows(self) -> list[dict]:
        """RR-10: one Status row per paused Review producer, with its Resume; "imports" also
        when the owner undid most of the lines fact_harvest saved (fact_harvest.ledger)."""
        from keepfold import fact_harvest, review
        from keepfold.events import EventLog
        try:
            v = self._vault()
            events = list(EventLog(v).iter_events())
            led = review.ledger(events)
            undone = fact_harvest.ledger(v, events)
        except Exception:  # noqa: BLE001  (a Status row must never cost the board)
            return []
        rows = [{"id": f"review-{p}", "label": "Review", "state": "warn",
                 "value": f"{review.PRODUCERS[p]} paused",
                 "detail": f"You dismissed {x['reject']} of the last {x['accept'] + x['reject']} "
                           "of these cards, so Review stopped offering them.", "resume": p}
                for p, x in led.items() if x["paused"]]
        if undone["paused"] and not led[fact_harvest.PRODUCER]["paused"]:
            n = undone["accept"] + undone["reject"]
            rows.append({"id": f"review-{fact_harvest.PRODUCER}", "label": "Review",
                         "state": "warn",
                         "value": f"{review.PRODUCERS[fact_harvest.PRODUCER]} paused",
                         "detail": f"You undid {undone['reject']} of the last {n} lines Keepfold "
                                   "saved from your chats, so it stopped saving them.",
                         "resume": fact_harvest.PRODUCER})
        return rows

    def warm_status(self) -> None:
        """Status and Setup read once in the background at start: a first read waits ~3-4 s
        on the LLM probes (LM Studio's `lms`, refused loopback connects) and first imports;
        Status, Setup and Local model then answer from `status.kept` (refreshed after KEEP_S)."""
        def warm():
            self.status()
            self.setup()  # Settings opens on Setup until it is done: its probes too
        self.warming = threading.Thread(target=warm, daemon=True, name="keepfold-app-warm-status")
        self.warming.start()

    def vault(self) -> dict:
        from keepfold.session import git_choice
        g = status.config().get("git")
        # the saved private choice, shown (greyed out) even while git is off
        private = isinstance(g, dict) and g.get("private_browsing") is True
        return {**status.vault(self.root), "embed": status.embedding(),
                "git": {"enabled": git_choice()[0], "private_browsing": private}}

    # ---- updates: checked and prepared quietly, applied only on a click ----
    def update_state(self) -> dict:
        from keepfold.app import desktop
        return {"current": update.current(), "mode": update.mode(), "ready": self.update_ready,
                "auto": desktop.update_check()}

    def update_quietly(self) -> None:
        """The background check (UPDATE_FIRST_S after start, then every UPDATE_EVERY_S): a
        newer release is downloaded and checked (installer), or its command noted (dev), or
        only notified (app: Mac/Linux never download unasked, D22), then
        shown as Restart to update and notified once per version. Never applied here. Skipped
        while `app.update_check` is off (read each pass); the buttons work either way."""
        from keepfold.app import desktop
        if not desktop.update_check():
            return
        info, cur = update.latest(), update.current()
        if info.get("waiting") or not update.newer(info["version"], update.parse_version(cur)):
            return
        version = ".".join(map(str, info["version"]))
        if self.update_ready and self.update_ready["version"] == version:
            return
        ready = {"version": version, "mode": update.mode(), "notes_url": info["notes_url"]}
        if ready["mode"] == "app":  # D22: Mac/Linux only tell; the download waits for a click
            self._notify_update(version, f"Keepfold {version} is available")
            return
        if ready["mode"] == "installer":
            dest = update.dest_dir(info)
            actions.sweep_updates(dest)
            if update.download(info, dest, None, self.stop) is None:  # the app is stopping
                return
        else:
            ready["command"] = update.command(info["tag"])
        self.update_ready = ready
        self._notify_update(version, f"Keepfold {version} is ready: restart to update")

    def _notify_update(self, version: str, text: str) -> None:
        """Once per version, while the updates notification is on."""
        from keepfold.app import desktop
        if self.desktop is not None and desktop.notify_settings()["updates"] \
                and desktop.load_state().get("update_notified") != version:
            self.desktop.notify(text)
            desktop.save_state("update_notified", version)

    def _check(self, job) -> tuple[dict, dict]:
        job.say("asking GitHub for the latest release")
        info, cur = update.latest(), update.current()
        out = {"current": cur, "latest": ".".join(map(str, info["version"])),
               "newer": update.newer(info["version"], update.parse_version(cur)),
               "notes_url": info["notes_url"], "mode": update.mode(),
               "size": info["exe_size"]}
        if info.get("waiting"):  # this computer's file is not attached yet: not offered
            out.update(newer=False, waiting=True, note=f"Keepfold {out['latest']} is out; the file "
                       "for this computer is still being prepared. Try again in half an hour.")
        if out["mode"] == "dev":
            out["command"] = update.command(info["tag"])
        actions.sweep_updates(update.dest_dir(info) if out["newer"] else None)
        return info, out

    def news_seen(self, body: dict) -> dict:
        """Got it on the What's new card: this version's news is not shown again."""
        news.record()
        return {"ok": True}

    def update_check(self, body: dict) -> dict:
        return self.jobs.start("Checking for updates", "update", lambda job: self._check(job)[1])

    def update_apply(self, body: dict) -> dict:
        """Download the new installer (unless the quiet check already did), check it, start it
        and quit; it reopens the app. Restart to update and Update now both come here. The Mac
        and Linux app swaps the new version in instead (`_swap`); `close` lists the Keepfold
        programs the user agreed to end (Programs in use)."""
        if (m := update.mode()) == "dev":
            info = update.latest()
            raise ValueError("This copy of Keepfold was not installed with KeepfoldSetup.exe. "
                             f"Update it with: {update.command(info['tag'])}")
        close = body.get("close") or []
        if not isinstance(close, list) or any(type(p) is not int for p in close):
            raise ValueError("close must be a list of process ids.")

        def run(job):
            info, out = self._check(job)
            if not out["newer"]:  # the same or a lower version is never installed
                return {**out, "launched": False}
            self._backup_first(job)

            def said(got, total):
                job.say(f"downloaded {got >> 20} of {total >> 20} MB")
            if m == "app":
                return self._swap(job, info, out, said, close)
            ready = self.update_ready
            exe = update.dest_dir(info) / update.EXE  # checked when the quiet check fetched it
            if not (ready and ready["version"] == out["latest"] and exe.is_file()):
                exe = update.download(info, update.dest_dir(info), said, job.cancelled)
            if exe is None:
                return {"cancelled": True}
            job.say("checked; starting the installer")
            update.launch(exe, os.getpid(), update.install_dir())
            threading.Timer(2, self.stop.set).start()  # the page reads the result first
            return {**out, "launched": True}
        return self.jobs.start("Updating Keepfold", "update", run, cancellable=True)

    def _backup_first(self, job) -> None:
        """D7 §6: an `update` backup before a new version runs, unless the last backup is under a
        day old. A refusal (no free space) stops the update with its reason."""
        import time

        from keepfold import backup
        if self.root is None or not (self.root / ".keepfold").is_dir():
            return
        last = backup.last(self.root)
        if last is not None and time.time() - last.stat().st_mtime < 86400:
            return
        job.say("backing up the vault first")
        backup.backup(self.root, reason="update")

    def _swap(self, job, info: dict, out: dict, said, close: list[int]) -> dict:
        """Mac/Linux Update now (D22: only on this click): download this computer's file, swap
        it in and quit; the new copy starts once this app has exited."""
        if not update.can_replace():  # a standard Mac user cannot change /Applications
            return {**out, "launched": False, "download_url": out["notes_url"],
                    "note": "Keepfold cannot replace itself where it is installed (this account "
                            "may not change that folder). Download the new version from the "
                            "release page and install it as you installed this one."}
        archive = update.download_asset(info, update.dest_dir(info), said, job.cancelled)
        if archive is None:
            return {"cancelled": True}
        job.say("checked; installing")
        r = update.install(archive, info, close)
        if r.get("busy"):  # Programs in use: the page asks, then sends their pids as `close`
            return {**out, "launched": False, **r}
        threading.Timer(2, self.stop.set).start()  # the page reads the result first
        return {**out, "launched": True}

    def update_check_set(self, body: dict) -> dict:
        """Turn the quiet update check on or off (`app.update_check`)."""
        from keepfold.app import desktop
        if set(body) != {"enabled"}:
            raise ValueError("send {\"enabled\": true or false}")
        on = _bool(body, "enabled")
        desktop._merge_app(lambda a: {**a, "update_check": on})
        return {"enabled": desktop.update_check()}

    def welcome(self) -> dict:
        """The welcome guide's quick look as the page opens, without Setup's slower probes:
        finished or skipped before, and whether a vault is set up."""
        from keepfold.app import desktop
        return {"welcome_done": desktop._app_cfg().get("welcome_done") is True,
                "vault": self.root is not None and (self.root / ".keepfold").is_dir()}

    def welcome_set(self, body: dict) -> dict:
        """The welcome guide was finished or skipped (`app.welcome_done`); it never opens by
        itself again. Like first_run_hidden, only true is accepted."""
        from keepfold.app import desktop
        if body != {"welcome_done": True}:
            raise ValueError("welcome_done can only be true")
        desktop._merge_app(lambda a: {**a, "welcome_done": True})
        return {"welcome_done": True}

    def backup(self, body: dict) -> dict:
        root = self.need_root()
        return self.jobs.start("Backing up the vault", "vault",
                               lambda job: actions.backup(job, root))

    def git_settings(self, body: dict) -> dict:
        """Save `git:` (enabled, private_browsing), keeping its other keys, then apply the
        private-browsing choice to the vault's .gitignore (and index) now, not at the next
        browse import."""
        from keepfold import taint
        from keepfold.browse import forget
        new = {"enabled": _bool(body, "enabled"),
               "private_browsing": _bool(body, "private_browsing")}
        taint.write_config(lambda cfg: {"git": {
            **(cfg["git"] if isinstance(cfg.get("git"), dict) else {}), **new}})
        if self.entry() is not None:
            forget.sync_private(self._vault())
        return {"ok": True}

    def config_restore(self, body: dict) -> dict:
        """The Status row's click: config.yaml.bak back over an unreadable config.yaml."""
        from keepfold import taint
        from keepfold.vault import VaultError
        try:
            taint.restore_config()
        except VaultError as e:  # its text names files, never a line of them
            raise ValueError(str(e)) from None
        return {"ok": True, "say": "Restored the last good copy of the settings file."}

    def doctor(self, body: dict) -> dict:
        root = self.root
        return self.jobs.start("Running the doctor", "vault",
                               lambda job: actions.doctor(job, root))

    def cancel(self, body: dict) -> dict:
        if self.jobs.cur is not None and self.jobs.cur.running and self.jobs.cur.cancellable:
            self.jobs.cur.cancel()
        return {"ok": True}

    def quit(self, body: dict) -> dict:
        if self.jobs.running():  # "Running jobs are cancelled": a download's process too
            self.jobs.cur.cancel()
        self.stop.set()
        return {"ok": True}

    # ---- the desktop app: window, Start with Windows, notifications ----
    def window_show(self, body: dict) -> dict:
        """A second `keepfold-app` launch brings this app's window up (False: the browser app)."""
        return {"shown": self.desktop is not None and self.desktop.show()}

    def window_capture(self, body: dict) -> dict:
        """`keepfold capture` (GNOME's shortcut) shows the quick capture box."""
        return {"shown": self.desktop is not None and self.desktop.capture()}

    def autostart(self) -> dict:
        from keepfold.app import desktop
        return desktop.autostart_state()

    def autostart_set(self, body: dict) -> dict:
        from keepfold.app import desktop
        out = desktop.set_autostart(_bool(body, "enabled"))
        if self.desktop is not None:
            self.desktop.refresh_tray()  # its Start with Windows tick
        return out

    def notify(self) -> dict:
        from keepfold.app import desktop
        # system_off: a Mac denied notifications in System Settings, so none are shown
        return {**desktop.notify_settings(),
                "system_off": desktop.load_state().get("notify_denied") is True}

    def notify_set(self, body: dict) -> dict:
        from keepfold.app import desktop
        return desktop.save_notify(body)

    def hotkey(self) -> dict:
        """Quick capture's key: the choice, the choices, and whether another app holds it
        (`taken`). `desktop`: false in the browser app, which has no hotkey, and on a Linux
        desktop other than GNOME, which says so in `note`."""
        from keepfold.app import desktop
        h = self.desktop.hotkey if self.desktop is not None else None
        linux = self.desktop is not None and h is None and sys.platform.startswith("linux")
        return {"desktop": h is not None, "key": desktop.hotkey_setting(),
                "choices": [{"key": k, "label": desktop.hotkey_label(k)}
                            for k in desktop.HOTKEYS],
                "taken": bool(h and h.taken), "note": desktop.HOTKEY_NOTE if linux else None}

    def hotkey_set(self, body: dict) -> dict:
        from keepfold.app import desktop
        desktop.save_hotkey(body)
        if self.desktop is not None:
            self.desktop.change_hotkey()  # registered again now, no restart
        return self.hotkey()

    def capture_hide(self, body: dict) -> dict:
        """The capture box's Esc, a saved line, or the box losing focus."""
        return {"hidden": self.desktop is not None and self.desktop.hide_box()}
