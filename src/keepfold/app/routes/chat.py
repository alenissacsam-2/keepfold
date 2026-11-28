"""Chat: its state, past chats, pages and passages, settings, attached files and the Chat test.

Pure moves out of `App` (server.py): the methods keep their bodies; `App` inherits this
mixin and its get/post tables still bind `self.<name>`."""

from __future__ import annotations

from keepfold.app import actions, status
from keepfold.app.routes._common import PAGE_ROWS, Busy, NotFound


class ChatRoutes:
    def private_chats(self, query: dict) -> dict:
        """Imported chats made private for a sensitive category (chat_import.made_private),
        newest first, PAGE_ROWS from `offset`: titles, apps, dates and categories only, never
        chat text or browsing. The app's own page only: no MCP tool reads it. While the Private
        switch is locked: counts by category only (`locked`), no row."""
        from keepfold import chat_import
        from keepfold.events import EventLog
        try:
            at = max(0, int((query.get("offset") or ["0"])[0]))
        except ValueError:
            at = 0
        v = self._vault()
        rows = chat_import.made_private(v, EventLog(v).iter_events())
        cats: dict[str, int] = {}
        for r in rows:
            cats[r["category"]] = cats.get(r["category"], 0) + 1
        if not self.private_unlocked:  # titles only while the Private switch is open
            return {"rows": [], "total": len(rows), "offset": 0, "locked": True,
                    "by_category": cats}
        return {"rows": rows[at:at + PAGE_ROWS], "total": len(rows), "offset": at,
                "locked": False, "by_category": cats}

    def chat_state(self) -> dict:
        """What the Chat page needs to open: the missing pieces (search model, local model),
        the model's name, the settings and the past chats."""
        from keepfold import chat, localllm
        if self.entry() is None:
            return {"vault": False}
        st = localllm.status()  # read-only: a GET never starts LM Studio or loads a model
        runner, why = st["runner"], st["error"]
        missing = [k for k, gone in (("embed", not status.embedding()["downloaded"]),
                                     ("llm", runner is None)) if gone]
        return {"vault": True, "missing": missing, "llm_error": why,
                "model": runner.model if runner else None, "settings": chat.get_settings(),
                "chats": chat.list(self._vault()), "files": self.files.list()}

    def chat_list(self) -> dict:
        from keepfold import chat
        return {"chats": chat.list(self._vault())}

    def chat_get(self, query: dict) -> dict:
        from keepfold import chat
        cid = (query.get("id") or [None])[0]
        if cid is None:
            return {"id": None, "messages": [], "remembers": 0}
        msgs = chat.load(self._vault(), cid)  # load checks the id
        return {"id": cid, "messages": msgs,  # where the model's memory starts, for its line
                "remembers": chat.remembers(msgs, chat.get_settings()["sources"])}

    def chat_passage(self, query: dict) -> dict:
        """The passage an answer cited (?path=&at=&len=), under Chat's source switches of now
        (`chat.passage`): a page under a switch that is off is refused, never shown."""
        from keepfold import chat
        q = {k: (query.get(k) or [""])[0] for k in ("path", "at", "len")}
        if not q["path"]:  # no page named: not found, as /api/home/site
            raise NotFound
        try:
            return chat.passage(self._vault(), q["path"], q["at"] or 0, q["len"] or 0,
                                chat.get_settings()["sources"])
        except PermissionError as e:
            raise ValueError(str(e)) from None

    def chat_pages(self, query: dict) -> dict:
        """The "#" list (?q=): pages Chat may read now whose title holds the words
        (`chat.pick_list`); {pages: [{id, title, kind}]}."""
        from keepfold import chat
        q = (query.get("q") or [""])[0][:200]
        return {"pages": chat.pick_list(self._vault(), q, chat.get_settings()["sources"])}

    def chat_settings_get(self) -> dict:
        from keepfold import chat
        return chat.get_settings()

    def chat_begin(self, body: dict) -> tuple:
        """Check a chat turn and take the one chat slot: (vault, id or None, text). Busy while
        another answer streams; ValueError on a bad body."""
        from keepfold import chat
        cid, text = body.get("id"), body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > chat.MAX_TEXT:
            raise ValueError(f"Type a message (up to {chat.MAX_TEXT:,} characters).")
        vault = self._vault()
        if cid is not None:
            chat._path(vault, cid)  # a ULID or ValueError: never a path
        if not self.chatting.acquire(blocking=False):
            raise Busy("An answer is still being written; wait for it or press Stop.")
        return vault, cid, text

    def chat_delete(self, body: dict) -> dict:
        from keepfold import chat
        if self.chatting.locked():  # the answer's final save would bring the chat back
            raise Busy("An answer is still being written; press Stop first, then delete.")
        if body.get("all") is True:
            self.files.clear()  # the chats are cleared: their attached files go too
            return {"deleted": chat.delete_all(self._vault())}
        return {"deleted": int(chat.delete(self._vault(), body.get("id")))}

    def chat_settings(self, body: dict) -> dict:
        from keepfold import chat
        return chat.save_settings(body)

    # ---- C1: a file attached in Chat, kept in memory only (keepfold.askfile) ----
    def chat_attach(self, body: dict) -> dict:
        """Read the file an upload brought (`body.upload`, named `body.name` by the page) into
        the attached files: parsed in the child, chunked and embedded in memory. The uploaded
        copy is deleted at once, read or not."""
        from keepfold import askfile
        uid = body.get("upload")
        p = self.uploads.pop(uid, None) if isinstance(uid, str) else None
        if p is None:
            raise ValueError("That file is gone; attach it again.")
        try:
            name = askfile.safe_name(body.get("name"))
            if not name.lower().endswith(p.suffix.lower()):  # the upload's checked extension
                name += p.suffix
            f = askfile.read(p, name)
        finally:
            p.unlink(missing_ok=True)
        return self.files.add(f).info()

    def chat_file_remove(self, body: dict) -> dict:
        if body.get("all") is True:
            self.files.clear()
        else:
            self.files.remove(body.get("id"))
        return {"files": self.files.list()}

    def chat_file_save(self, body: dict) -> dict:
        """Save to your brain: the attached file through the documents import (askfile.save),
        committed as other imports are; {saved: {title, path}, files}."""
        from keepfold import askfile
        from keepfold.vault import Vault
        got = self.files.get([body.get("id")])
        if not got:
            raise ValueError("That file is no longer attached; attach it again.")
        v = Vault(self.need_root())
        saved = askfile.save(v, got[0])
        actions._commit(v, "docs")
        return {"saved": saved, "files": self.files.list()}

    def chat_eval(self, body: dict) -> dict:
        """Run Chat's offline test (keepfold.chat_eval) as a job: a made-up vault in a temp
        folder, never the owner's; the result is the score, blockers and failures."""
        from keepfold import chat_eval

        def run(job):
            r = chat_eval.run(cancel=job.cancelled, progress=lambda d, n, qid: job.progress(
                1, 1, d, n, f"Question {qid}"))
            return {"kind": "chat_eval", "passed": r["passed"], "ran": r["ran"],
                    "result": r["result"], "manifest": r["manifest"][:12],
                    "queries": {k: v["rate"] for k, v in r["queries"].items()},
                    "misses": {k: len(v) for k, v in r["misses"].items()},
                    "blocked": r["blocked"], "total": r["total"], "bar": r["pass_bar"],
                    "model": r["model"], "model_note": r["model_note"], "stopped": r["stopped"],
                    "blockers": [{k: b[k] for k in ("name", "state")} for b in r["blockers"]],
                    "failures": [{"id": q["id"], "question": q["question"],
                                  "why": "; ".join(q["why"]), "answer": q["answer"][:300]}
                                 for q in r["questions"] if q["status"] == "fail"],
                    "recall": r["retrieval"]["recall"], "median_s": r["latency"]["median"]}
        return self.jobs.start("Running the Chat test", "chat", run, cancellable=True)
