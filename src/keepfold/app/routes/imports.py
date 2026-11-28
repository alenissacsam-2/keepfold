"""Imports: guided imports, Downloads pickup, watched folders and the periodic import passes.

Pure moves out of `App` (server.py): the methods keep their bodies; `App` inherits this
mixin and its get/post tables still bind `self.<name>`."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from keepfold.app import actions, exports, status
from keepfold.app.routes._common import IMPORTS, Busy, _bool


class ImportsRoutes:
    def auto_import(self) -> dict | None:
        """The live import, every AUTO_EVERY_S (browse.auto_import_hours 0 turns it off): only
        the new visits of the browsers and profiles imported before, quietly (no job: it takes
        seconds), when no job runs and the browse-wide lock is free; never before the user's
        first import. The site pages first move into the store (`store.migrate`), as a job
        with its steps when there are pages to move. {"live": counts}, {"job": ...}, or None
        when it did not run."""
        from keepfold import browse_serve
        from keepfold.browse import load_settings, store
        from keepfold.vault import Vault
        if not self.entry() or self.jobs.running():
            return None
        if not status.embedding()["downloaded"]:  # every page would land under Other: wait
            return None                            # for the search model
        root = self.root
        v = Vault(root)
        if not browse_serve.auto_import_due(v, live=True):
            return None
        srcs = browse_serve.auto_sources(v)  # never an unticked one
        if not srcs:
            return None
        if not store.migrated(v):
            if store._site_page_files(v):
                try:
                    return self.jobs.start("Moving site pages into Keepfold", "browse",
                                           lambda job: actions.browse_migrate(job, root),
                                           quiet_errors=True)
                except Busy:
                    return None
            store.migrate(v, settings=load_settings())  # no page to move: only marks it done
        return {"live": status.safe(browse_serve.run_import(v, srcs=srcs,
                                                            say=lambda s: None))}

    def chat_import_auto(self) -> dict | None:
        """Start a chat import job (chat_import.run) when the setting is on, a vault is set up,
        no job runs and a transcript is new or changed (stats only); None when it did not."""
        from keepfold import chat_import, taint
        from keepfold.vault import Vault
        if self.entry() and (err := taint.config_error()):  # fail closed: it may turn it off
            self.echo(f"keepfold app: chat import skipped: {err}")
            return None
        if not chat_import.enabled() or not self.entry() or self.jobs.running():
            return None
        root = self.root
        if not chat_import.pending(Vault(root)):
            return None
        try:
            return self.jobs.start("Chat import", "status",
                                   lambda job: actions.chat_import(job, root),
                                   cancellable=True, quiet_errors=True)
        except Busy:  # a job started meanwhile: the next pass tries again
            return None

    def safety_net_auto(self) -> dict | None:
        """One safety_net pass over the chat pages earlier imports wrote, in this thread, when
        the chat import is on, a vault is set up and no job runs; committed like an import.
        Its counts, or None when it did not run."""
        from keepfold import chat_import, safety_net
        from keepfold.vault import Vault
        if not chat_import.enabled() or not self.entry() or self.jobs.running():
            return None
        v = Vault(self.root)
        if not safety_net.pending(v):  # stats only: no model probe when nothing changed
            return None
        runner, embed = safety_net.background_tools()
        try:
            return safety_net.run(v, runner, embed)
        finally:
            actions._commit(v, "safety net")

    def chat_import_set(self, body: dict) -> dict:
        """Turn the automatic chat import on or off (`app.chat_import`)."""
        from keepfold import chat_import
        from keepfold.app import desktop
        on = _bool(body, "enabled")
        desktop._merge_app(lambda a: {**a, "chat_import": on})
        return {"enabled": chat_import.enabled()}

    # ---- guided imports: an upload (or a pasted path), a dry-run preview, then the import ----
    def _drop(self, uid: str | None) -> None:
        if uid and (p := self.uploads.pop(uid, None)) is not None:
            p.unlink(missing_ok=True)

    def _import(self, body: dict, title: str, fn, keep: bool) -> dict:
        """Start `fn(job, source, path, root)` as a job on a checked source and file. The
        upload is deleted when the job fails, and after it unless `keep` (a preview)."""
        source, uid, raw = body.get("source"), body.get("upload"), body.get("path")
        if source not in IMPORTS:
            raise ValueError(f"source must be one of {', '.join(IMPORTS)}")
        if uid is not None:
            p = self.uploads.get(uid) if isinstance(uid, str) else None
            if p is None or not p.is_file():
                raise ValueError("That file is gone; add it again.")
        else:
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("Add the file, or paste its full path.")
            p = Path(raw.strip().strip("\"'")).expanduser()
            if not p.is_absolute():
                raise ValueError("Paste the full path, such as "
                                 "C:\\Users\\you\\Downloads\\export.zip.")
            p = p.resolve()
            if not p.exists():  # a folder is fine: Perplexity .md files, an unzipped Takeout
                raise ValueError(f"Nothing is at {p}.")
        # notes, calendars and Google activity go into the vault; chats go beside it
        root = self.need_root() if source in actions.TIERED or (
            source == "google-activity" and not keep) else self.root

        def run(job):
            out = None
            try:
                out = fn(job, source, p, root)
                return out
            finally:  # kept after a preview, or to retry once the search model is in
                if out is None or not (keep or out.get("need")):
                    self._drop(uid)
        return self.jobs.start(title, "setup", run,
                               cancellable=source in ("google-activity", "voice-memos"),
                               quiet_errors=source == "google-activity")

    def import_preview(self, body: dict) -> dict:
        return self._import(body, "Checking the file", actions.import_preview, keep=True)

    def import_run(self, body: dict) -> dict:
        # C2: the tier the page's choice sent; only the notes and calendar cards send one
        if body.get("tier") not in (None, "skip", "local", "cloud"):  # before the job: the
            raise ValueError("tier must be one of skip, local, cloud")  # upload is kept
        fn = actions.import_run if body.get("tier") is None else \
            partial(actions.import_run, tier=body["tier"])
        return self._import(body, "Importing", fn, keep=False)

    # B2-b: an export in Downloads, offered on Home (keepfold.app.exports)
    def pickup(self) -> dict:
        return exports.offer()

    def pickup_add(self, body: dict) -> dict:
        """Add chats: the Import page's import of the offered zip, without its preview."""
        c = exports.pick(body.get("path"))
        out = self._import({"source": c["source"], "path": c["path"]}, "Importing",
                           actions.import_run, keep=False)
        exports.mark(c, "added")
        return out

    def pickup_dismiss(self, body: dict) -> dict:
        exports.mark(exports.pick(body.get("path")), "dismissed")
        return {"ok": True}

    def pickup_later(self, body: dict) -> dict:
        exports.later()
        return {"ok": True}

    # R18: watched folders (keepfold.watch), re-imported by the periodic pass (`watch_auto`)
    def _watched(self) -> dict | None:
        from keepfold import watch
        if self.root is None or not (self.root / ".keepfold").is_dir():
            return None
        return watch.listing(self._vault())

    def watch_add(self, body: dict) -> dict:
        from keepfold import watch
        from keepfold.vault import VaultError
        path, source, tier = body.get("path"), body.get("source"), body.get("tier")
        if source not in watch.SOURCES:
            raise ValueError(f"source must be one of {', '.join(watch.SOURCES)}")
        if tier not in (None, "skip", "local", "cloud"):
            raise ValueError("tier must be one of skip, local, cloud")
        if not isinstance(path, str | None) or (not (path or "").strip() and source != "zotero"):
            raise ValueError("Paste the folder's full path.")
        try:  # an empty path: Zotero finds its own library
            watch.add(self._vault(), (path or "").strip() or None, source, tier)
        except VaultError as e:
            raise ValueError(str(e)) from None
        return {"ok": True, "watched": self._watched()}

    def watch_remove(self, body: dict) -> dict:
        from keepfold import watch
        if not isinstance(body.get("path"), str) or not watch.remove(self._vault(), body["path"]):
            raise ValueError("That folder is not watched.")
        return {"ok": True, "watched": self._watched()}

    def watch_auto(self) -> list | None:
        """One pass over the watched folders (keepfold.watch.run), in the periodic pass's thread,
        when a vault is set up and no job runs; committed like an import. Its results, or None
        when it did not run."""
        from keepfold import watch
        from keepfold.vault import Vault
        if not self.entry() or self.jobs.running():
            return None
        v = Vault(self.root)
        try:
            return watch.run(v)
        finally:
            actions._commit(v, "watched folders")

    def forget_due(self) -> list | None:
        """R10: the //forget lines a prompt hook or keepfold_prime retired and queued, swept from
        every store (keepfold.forget.run_due), in the periodic pass's thread when a vault is set
        up and no job runs; committed. The receipts, or None when it did not run."""
        from keepfold import forget, session
        from keepfold.vault import Vault
        if not self.entry() or self.jobs.running():
            return None
        v = Vault(self.root)
        try:
            return forget.run_due(v)
        finally:
            if v.writes:
                session._commit(v, "keepfold: forget a line", v.wrote_since(0))
