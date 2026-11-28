"""Home: the live dashboard, the Private switch's reads, its layout, facts, date, the 90-second
check and quick capture.

Pure moves out of `App` (server.py): the methods keep their bodies; `App` inherits this
mixin and its get/post tables still bind `self.<name>`."""

from __future__ import annotations

import json
import sys
import time
from urllib.parse import urlsplit

from keepfold.app import status, targets
from keepfold.app.routes._common import CAPTURE_MAX, URL_ONLY, NotFound


class HomeRoutes:
    def _home(self):
        from keepfold.home import HomeCache
        from keepfold.vault import Vault
        root = self.need_root()
        lock, homes = self.homes
        with lock:
            if root not in homes:
                homes.clear()  # one vault at a time
                homes[root] = HomeCache(Vault(root))
            return homes[root]

    # ---- the Private switch: private items only after Windows Hello, for 10 minutes ----
    @property
    def private_unlocked(self) -> bool:
        return time.monotonic() < self.private_until

    def _private(self, query: dict) -> bool | None:
        """True: the page asks for private items and may have them. None: it asks while the
        switch is locked (it expired, or the window was minimised), so it relocks."""
        if (query.get("private") or [""])[0] != "1":
            return False
        return True if self.private_unlocked else None

    def warm_home(self) -> None:
        try:
            self._home().get(False)
        except Exception as e:  # noqa: BLE001  (the page's own read reports a real problem)
            print(f"keepfold app: the home warm-up failed ({type(e).__name__})", file=sys.stderr)

    def home(self, query: dict) -> dict:
        """The live home (keepfold.home): the dashboard's data plus feed, next_up and exams.
        Builds and keeps the brain layout (`.keepfold/brain-layout.json`, a view cache). A page
        that must relock gets the public home with `relock`."""
        private = self._private(query)
        home = self._home().get(private is True)
        home = {**home, **self._home_prefs(home)}
        return home if private is not None else {**home, "relock": True}

    # ---- UX-E: Home's pinned layout and its Get started board (config.yaml `app:`) ----
    def _home_prefs(self, home: dict) -> dict:
        """`home_layout` and `first_run_hidden` from config.yaml; while Get started shows,
        `connected`: an AI app is connected, as Setup's Connect step reads it."""
        from keepfold import dashboard
        from keepfold.app import desktop
        a = desktop._app_cfg()
        out = {"home_layout": dashboard.home_layout({"app": a}),
               "first_run_hidden": a.get("first_run_hidden") is True}
        if home.get("get_started") and not out["first_run_hidden"]:
            entry = self.entry()
            out["connected"] = any(t["connected"] for t in status.kept(
                ("targets", json.dumps(entry, sort_keys=True)), lambda: targets.describe(entry)))
        return out

    def home_layout(self, body: dict) -> dict:
        """Pin layout (`order`: Home's groups in order, null to unpin) and Get started's Hide
        (`first_run_hidden`: true), kept in config.yaml's `app:` as app.notify is."""
        from keepfold import dashboard
        from keepfold.app import desktop
        if not body or set(body) - {"order", "first_run_hidden"}:
            raise ValueError("send order or first_run_hidden")
        order = body.get("order")
        if "order" in body and order is not None and dashboard.home_layout(
                {"app": {"home_layout": order}}) is None:
            raise ValueError(f"order must list {', '.join(dashboard.HOME_GROUPS)}, each once")
        if "first_run_hidden" in body and body["first_run_hidden"] is not True:
            raise ValueError("first_run_hidden can only be true")

        def merge(a: dict) -> dict:
            a = {**a, **({"first_run_hidden": True} if "first_run_hidden" in body else {})}
            if "order" in body:
                a = {k: v for k, v in a.items() if k != "home_layout"}
                if order is not None:
                    a["home_layout"] = list(order)
            return a
        desktop._merge_app(merge)
        return self._home_prefs({})

    def home_fact(self, body: dict) -> dict:
        """Get started's one line about you, saved as a `//fact:` typed in a chat is (tags:
        me/profile under Background, the owner's own words)."""
        from keepfold import tags
        from keepfold.vault import Vault
        text = body.get("text")
        text = " ".join(text.split()) if isinstance(text, str) else ""
        if not text:
            raise ValueError("Write one line about you first.")
        if len(text) > tags.MAX_CHARS:
            raise ValueError(f"Keep it to {tags.MAX_CHARS} characters.")
        root, refused = self.need_root(), []
        with self.reviewing:
            saved = tags.apply_in(Vault(root), [("fact", text, self._today())], refused=refused)
        if refused:
            raise ValueError(refused[0])
        return {"saved": saved}

    def home_version(self) -> dict:
        """The home's version, the Private switch and a prepared update (the page's 3 s poll)."""
        r = self.update_ready
        return {"v": self._home().version(), "private": self.private_unlocked,
                "update": r and {"version": r["version"], "mode": r["mode"]}}

    def home_delta(self, query: dict) -> dict:
        """Changes since `since`; a page that must relock gets the public home whole, so no
        private item stays on it."""
        private = self._private(query)
        if private is None:
            return {**self._home().get(False), "full": True, "relock": True}
        return self._home().delta((query.get("since") or [None])[0], private)

    def home_site(self, query: dict) -> dict:
        """One website of the brain's stem, read from the browsing store (never the vault's
        Markdown). A private site while the switch is locked is a 404, as if it did not exist."""
        got = self._home().site((query.get("site") or [""])[0], self._private(query) is True)
        if got is None:
            raise NotFound
        return got

    # ---- the 90-second check on Home: self-graded, recorded through keepfold_grade ----
    COLD_ANSWERS = {"forgot": ("wrong", None, "Forgot"), "shaky": ("correct", 2, "Shaky"),
                    "knew": ("correct", 4, "Knew it")}

    def cold_check(self) -> dict:
        """Up to 3 cloud-visible concepts due today or tomorrow, most overdue first, with each
        page's one-line summary (home.cold_check). Never a private concept."""
        from datetime import UTC, datetime

        from keepfold import home
        return {"items": home.cold_check(self._vault(), datetime.now(UTC))}

    def cold_answer(self, body: dict) -> dict:
        """One answer: a closed-book recall item graded by the user (wrong; right at confidence
        2; right at confidence 4), written by keepfold_grade's own code path as the Review's clicks
        are (`_click`: an `app_clicks` Keepfold, a session closed at once so it is committed and
        `keepfold undo` takes it back), with the app as client and model "self". A concept the
        check would not offer now (unknown, private, not due) is a 404."""
        from datetime import UTC, datetime

        from keepfold import home, planner
        from keepfold.catalog import Catalog
        from keepfold.events import local_day
        from keepfold.mcp_server import Keepfold
        from keepfold.vault import Vault
        cid, answer = body.get("id"), body.get("answer")
        if answer not in self.COLD_ANSWERS:
            raise ValueError("answer must be forgot, shaky or knew.")
        v = self._vault()
        day = local_day(datetime.now(UTC))
        catalog = Catalog.load(v)
        if not isinstance(cid, str) or cid not in home.cold_due(
                planner.current_states(v, day, catalog)[0], catalog,
                planner.hidden(v, catalog), day):
            raise NotFound
        verdict, confidence, said = self.COLD_ANSWERS[answer]
        kwargs = {"concept": cid, "question": "90-second check: recall what you know, then "
                  "reveal.", "item_type": "recall", "learner_answer": said, "verdict": verdict,
                  **({"confidence": confidence} if confidence else {})}
        root = self.need_root()
        with self.reviewing:
            if self.core is None or self.core[0] != root:
                core = Keepfold(Vault(root))
                core.app_clicks = True  # every write here is the user's own click
                self.core = (root, core)
            core = self.core[1]
            client = core.sessions.client
            core.sessions.client = "keepfold-app"  # server-set, as grader.client always is
            try:
                core.session = core.sessions.start("ambient", None, "self")
                core._reset()
                out = core._call("keepfold_grade", core.keepfold_grade, kwargs)
            finally:
                core.sessions.client = client
                if core.sessions.current is not None:
                    core.sessions.close(summary="90-second check in the Keepfold app")
        if "error" in out:
            raise ValueError(out.get("message") or out["error"])
        return {"recorded": True}

    DATE_KINDS = ("exam", "deadline")

    def home_date(self, body: dict) -> dict:
        """Home's Add a date (R31): an exam or deadline as an assessment page {title, date,
        kind, outcome: pending}, written by keepfold_add's own code path as an AI app's add is (an
        `app_clicks` Keepfold, the app as client, a session closed at once so `keepfold undo` takes
        it back), so the planner, Next up, Chat and the snapshot see it. Refused: a name empty
        or over 120 characters, a date that is not real or before today, another kind, and the
        same name on the same day (an assessment page already has it)."""
        from datetime import date

        from keepfold.mcp_server import Keepfold
        from keepfold.vault import Vault
        what, when, kind = body.get("what"), body.get("when"), body.get("kind", "exam")
        what = what.strip() if isinstance(what, str) else ""
        if not what:
            raise ValueError("Give it a name, such as DSA exam.")
        if len(what) > 120:
            raise ValueError("Keep the name to 120 characters.")
        try:
            day = date.fromisoformat(when) if isinstance(when, str) else None
        except ValueError:
            day = None
        if day is None:
            raise ValueError("Pick a date.")
        if day.isoformat() < self._today():
            raise ValueError("That date has passed. Pick today or a later day.")
        if kind not in self.DATE_KINDS:
            raise ValueError("kind must be exam or deadline.")
        root = self.need_root()
        with self.reviewing:
            v = Vault(root)
            if any(str(n.fields.get("date") or "")[:10] == day.isoformat()
                   and str(n.fields.get("title") or "").casefold() == what.casefold()
                   for n in v.iter_notes("assessment")):
                raise ValueError("That is on your board already.")
            if self.core is None or self.core[0] != root:
                core = Keepfold(v)
                core.app_clicks = True  # every write here is the user's own click
                self.core = (root, core)
            core = self.core[1]
            client = core.sessions.client
            core.sessions.client = "keepfold-app"  # server-set, as grader.client always is
            try:
                core.session = core.sessions.start("ambient", None, "self")
                core._reset()
                # force: another day's page of the same name (last term's DSA exam) is no copy
                out = core._call("keepfold_add", core.keepfold_add, {
                    "kind": "assessment", "title": what, "force": True, "fields": {
                        "date": day.isoformat(), "kind": kind, "outcome": "pending"}})
            finally:
                core.sessions.client = client
                if core.sessions.current is not None:
                    core.sessions.close(summary="Added a date in the Keepfold app")
        if "error" in out:
            raise ValueError(out.get("message") or out["error"])
        return {"id": out["id"], "title": what, "date": day.isoformat(), "kind": kind}

    def capture(self, body: dict) -> dict:
        """Quick capture (the desktop app's hotkey box): one typed line, written through the
        shared core (`_with_core`) as the owner's own words. //pref, //fact and //forget lines
        take the chat's own tag path (tags.find, tags.apply); a line that is only an http(s)
        URL becomes a resource titled with its host and path (no fetch), unless a resource in
        the index has that URL already; anything else is a journal line. {kind, said}: `said`
        is the hint line's text. A refusal (a secret, an unsafe line) is a ValueError with the
        core's own message, and nothing is written."""
        from datetime import date

        from keepfold import chats, dates, tags
        from keepfold.events import local_day
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Type something first.")
        if len(text) > CAPTURE_MAX:
            raise ValueError(f"Keep it to {CAPTURE_MAX:,} characters.")
        clean = chats.drop_private(text).strip()
        if not clean.replace(chats.PRIVATE, "").strip():
            raise ValueError("Nothing to save: a <private> part is never saved.")
        if (found := tags.find(text)) == [("off", "")]:  # A24: a chat's switch, not a capture
            raise ValueError("Nothing saved: //off only works in a chat.")
        if found:
            def tag(core) -> tuple[list[str], list[str]]:
                refused: list[str] = []
                day = local_day(core._now())
                return tags.apply(core, [(t, x, day) for t, x in found],
                                  refused=refused), refused
            lines, refused = self._with_core(tag, "Quick capture in the Keepfold app")
            if refused:
                raise ValueError("; ".join(lines + refused))
            return {"kind": "tag", "said": "; ".join(lines) or "Already saved today."}
        parts = urlsplit(clean) if URL_ONLY.fullmatch(clean) else None
        if parts is not None and parts.hostname:
            want = clean.removesuffix("/")
            title = (parts.hostname + parts.path.removesuffix("/"))[:200]

            def link(core) -> dict:
                core.index.refresh()
                for n in core.index._notes.values():  # the index's parsed frontmatter
                    u = n["fields"].get("url")
                    if n["kind"] == "resource" and isinstance(u, str) \
                            and u.strip().removesuffix("/") == want:
                        return {"already": n["title"]}
                return core._call("keepfold_add", core.keepfold_add, {
                    "kind": "resource", "title": title, "force": True, "fields": {"url": clean}})
            out = self._with_core(link, "Quick capture in the Keepfold app")
            if "error" in out:
                raise ValueError(out.get("message") or out["error"])
            if "already" in out:
                return {"kind": "link", "said": f"Already saved: {out['already']}"}
            return {"kind": "link", "said": "Link saved"}

        def journal(core) -> dict:
            now = core._now()
            # typed just now, so its relative dates get today's day (L1); _log_journal pins
            # nothing itself under app_clicks (mcp_server._pin_day), so this is the one pin
            line = dates.pin(clean, date.fromisoformat(local_day(now)))
            out = core._call("keepfold_log", core.keepfold_log,
                             {"kind": "journal", "payload": {"text": line}})
            return {**out, "hhmm": f"{now.astimezone():%H:%M}"}
        out = self._with_core(journal, "Quick capture in the Keepfold app")
        if "error" in out:
            raise ValueError(out.get("message") or out["error"])
        return {"kind": "journal", "said": f"Added to today's journal {out['hhmm']}"}

    def lock(self, body: dict | None = None) -> dict:
        self.private_until = 0.0
        return {"ok": True}
