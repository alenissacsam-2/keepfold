"""Review (what waits for the owner's click) and Tonight.

Pure moves out of `App` (server.py): the methods keep their bodies; `App` inherits this
mixin and its get/post tables still bind `self.<name>`."""

from __future__ import annotations

import sys

from keepfold.app import actions, targets
from keepfold.app.routes._common import NotFound


class ReviewRoutes:
    # ---- Review (plan 5a): what waits for the user's own click ----
    def tidy_about_me(self) -> dict:
        """D1-D3: Keepfold's own sweep of the about-me lines (mcp_server.Keepfold.tidy), at start
        and with each chat-import pass: waiting about-me lines move to their pages, near-copies
        and settled pairs are retired, in one session `keepfold undo` can take back. Not an app
        click, so nothing is stamped `clicked`."""
        from keepfold.mcp_server import Keepfold
        from keepfold.vault import Vault
        root = self.root
        if root is None or not (root / ".keepfold").is_dir():
            return {"moved": 0, "retired": 0}
        from keepfold import review
        with self.reviewing:  # never beside a Review click
            review.record_baseline(Vault(root))  # R5a, first run: the guard's baseline
            return Keepfold(Vault(root), build=False).tidy()  # tidy reads me/ pages only

    def review(self) -> dict:
        """The Review view and its badge count (keepfold.review); shown text only. UX-B: each
        card about a page carries `related` (`_related`)."""
        from keepfold import review
        from keepfold.events import EventLog
        v = self._vault()
        evs = list(EventLog(v).iter_events())
        return self._related(v, review.view(v, evs, self._today()), evs)

    # ---- UX-B: "#" pages in Chat, related pages beside Review cards ----
    RELATED = 3
    CARDS = ("pending", "expired", "stale", "rules", "imports", "unsafe", "outside", "crowded",
             "promote")  # the Review boards whose cards name their `page`

    def _related(self, v, view: dict, evs: list) -> dict:
        """Each card with a `page` gets `related`: [{title, path}] of up to RELATED pages linked
        with it (its [[links]] either way: the edges arm of `semantic.linked`, the cheap one;
        the name arms cost ~180 ms a page at 5k), most links first, then by id; never the
        card's own, a rollup, a hub or a quieted page. Chat's kept index holds no private
        page, so none is listed (a card on a private page has none)."""
        from keepfold import chat, chat_study, harvest, semantic
        from keepfold.topics import is_hub
        cards = [x for k in self.CARDS for x in view.get(k) or []
                 if isinstance(x.get("page"), str)]
        # never build Chat's index (~3 s at 5k) just for Review: before Chat's first use, no line
        if not cards or chat._INDEX.get(v.root) is None:
            return view
        quiet, got = harvest.quiet(evs), {}
        with chat._INDEX_LOCK:
            idx = chat._index(v, chat.FRESH)
            paths = {i: p for p, i in idx._ids.items()}
            for page in {x["page"] for x in cards}:
                n: dict = {}
                for i, _ in semantic._neighbours(idx, page):
                    note = idx._notes.get(i)
                    if i != page and i in paths and note and note["kind"] != "rollup" \
                            and not is_hub(note) and quiet.get(i) != 0:
                        n[i] = n.get(i, 0) + 1
                got[page] = [{"title": chat_study._title(idx._notes[i]["title"]) or i,
                              "path": idx._rel(paths[i])}
                             for i in sorted(n, key=lambda i: (-n[i], i))[:self.RELATED]]
        for x in cards:
            x["related"] = got[x["page"]]
        return view

    def _click(self, tool: str, method: str, kwargs: dict, card: str | None = None,
               producer: str | None = None) -> dict:
        """One Review click, written by an mcp_server.Keepfold with `app_clicks` set through the
        tool's own code path (keepfold_patch, keepfold_log), in a session closed at once so it is
        committed and `keepfold undo` can take it back. Returns the fresh Review view. `card`
        (R3-4) and `producer` (RR-10, review.ledger): stamped on each change the click
        writes."""
        return self._clicks([(tool, method, kwargs, card, producer)])

    def _with_core(self, fn, summary: str):
        """fn(core) with the app's shared mcp_server.Keepfold (`app_clicks` set: every write is
        the owner's own) under the Review lock, in a session closed at once so it is committed
        and `keepfold undo` can take it back; fn's own result."""
        from keepfold.mcp_server import Keepfold
        from keepfold.vault import Vault
        root = self.need_root()
        with self.reviewing:
            if self.core is None or self.core[0] != root:
                core = Keepfold(Vault(root))
                core.app_clicks = True  # every write here is the user's own click
                self.core = (root, core)
            core = self.core[1]
            try:
                return fn(core)
            finally:
                if core.sessions.current is not None:
                    core.sessions.close(summary=summary)

    def _clicks(self, calls: list[tuple]) -> dict:
        """One answer's writes, each a `_click` (tool, method, kwargs, card, producer), all in
        one session, so one `keepfold undo` takes the whole answer back (D5: an answer that
        covers near-copies). Stops at the first error; what was written before it is kept."""
        def run(core) -> dict:
            out: dict = {}
            try:
                for tool, method, kwargs, card, producer in calls:
                    core.sessions.stamp = {k: v for k, v in (("card", card),
                                                             ("producer", producer)) if v}
                    out = core._call(tool, getattr(core, method), kwargs)
                    if "error" in out:
                        break
            finally:
                core.sessions.stamp = {}
            return out
        out = self._with_core(run, "Review in the Keepfold app")
        if "error" in out:
            raise ValueError(out.get("message") or out["error"])
        # R3-7: the rules-file block; R5a: a Keep or Remove on a line changed outside Keepfold
        if any(c[2].get("id") == "me/core" or c[2].get("page") in ("outside", "core")
               for c in calls):
            try:  # follows the core at once
                targets.refresh_rules(self.entry())
            except Exception as e:  # noqa: BLE001  (the periodic pass tries again)
                print(f"keepfold app: rules block refresh failed ({type(e).__name__})",
                      file=sys.stderr)
        return self.review()

    def _waiting(self, body: dict) -> dict:
        from keepfold import review
        key = body.get("key")
        item = next((p for p in review.pending(self._vault(), self._today())
                     if isinstance(key, str) and p["key"] == key), None)
        if item is None:
            raise NotFound
        return item

    def _live(self, body: dict) -> dict:
        from keepfold import claims
        cid = body.get("id")
        row = next((r for r in claims.rows(self._vault()) if isinstance(cid, str)
                    and r["id"] == cid and r["status"] == "live"), None)
        if row is None:
            raise NotFound
        return row

    def _waiting_group(self, p: dict) -> list[dict]:
        """D5: the waiting lines of `p`'s page that say nearly what `p` says, `p` first."""
        from keepfold import review
        same = {x["key"]: x for x in review.pending(self._vault(), self._today())
                if x["page"] == p["page"]}
        return [same[k] for k in review.group(list(same.values()), p["key"]) if k in same]

    def review_approve(self, body: dict) -> dict:
        """Approve: the waiting line written as keepfold_patch writes a confirmed one, stamped
        `confirm: clicked`; a teaching-contract rule applies only this way (E2). D5: its
        near-copies go with it: on a claim page each is approved too (keepfold_patch keeps one of
        near-duplicates, D1), elsewhere they are dismissed. Only the first click is the card."""
        from keepfold import claims
        p = self._waiting(body)
        calls = []
        for n, x in enumerate(self._waiting_group(p)):
            card = "pending" if n == 0 else None
            if n and x["page"] not in claims.PAGES:
                calls.append(("keepfold_patch", "_dismiss", {
                    "page": x["page"], "section": x["section"], "text": x["text"]}, card,
                    self._producer(x)))
                continue
            rule = x["page"] == "teaching-contract"  # stored bare; a rule is a list line
            calls.append(("keepfold_patch", "keepfold_patch", {
                "id": f"me/{x['page']}", "op": "append_to_section", "target": x["section"],
                "text": "- " * rule + x["text"], "user_confirmed": True}, card,
                self._producer(x)))
        return self._clicks(calls)

    def review_dismiss(self, body: dict) -> dict:
        """Dismiss: a claim page's line goes to its `## Not me` (keepfold_patch reject); another
        page's (the contract, the portrait) leaves a `dismiss` change. Never proposed again.
        D5: it covers the line's near-copies too."""
        from keepfold import claims
        calls = []
        for n, p in enumerate(self._waiting_group(self._waiting(body))):
            card = "pending" if n == 0 else None
            if p["page"] in claims.PAGES:
                calls.append(("keepfold_patch", "keepfold_patch", {
                    "id": f"me/{p['page']}", "op": "reject",
                    "target": f"{p['section']}: {p['text']}"}, card, self._producer(p)))
            else:
                calls.append(("keepfold_patch", "_dismiss", {
                    "page": p["page"], "section": p["section"], "text": p["text"]}, card,
                    self._producer(p)))
        return self._clicks(calls)

    def _line_group(self, cid: str) -> list[str]:
        """D5: the live line `cid` and, when it is on the Still true? or the unsafe board, the
        near-copies that card stands for (review.stale's `ids`, review.collapse)."""
        from keepfold import review
        from keepfold.events import EventLog
        v = self._vault()
        events = list(EventLog(v).iter_events())
        for x in review.stale(review.live_rows(v, events, self._today()), events,
                              self._today(), review.known_texts(v)):
            if cid in x["ids"]:
                return [cid, *(i for i in x["ids"] if i != cid)]
        hidden = review._private(v)
        return review.group([x for x in review.unsafe_lines(v, events)
                             if x["page"] not in hidden], cid, "id")

    def review_still(self, body: dict) -> dict:
        """Still true: a reconfirm stamped `confirm: clicked` (claims.tiers: confirmed), for
        the line and the near-copies its card stands for (D5)."""
        return self._clicks([("keepfold_log", "keepfold_log", {
            "kind": "reconfirm", "payload": {"claim": cid, "verdict": "yes"}}, None, None)
            for cid in self._line_group(self._live(body)["id"])])

    def review_retire(self, body: dict) -> dict:
        """Retire: keepfold_patch's retire op moves the line to `## Past`. A //forget card's
        candidate is the forget producer's accept (RR-10); a crowded line's (G7) is stamped
        `crowded`, so it counts in that card's monthly cap. D5: on a Still true? or unsafe card
        the near-copies it stands for are retired too."""
        from keepfold import claims, review, tags
        from keepfold.events import EventLog
        r = self._live(body)
        v = self._vault()
        events = list(EventLog(v).iter_events())
        cards = [x["text"] for x in tags.forget_cards(v, events, self._today())
                 if any(c["id"] == r["id"] for c in x["candidates"])]
        forget = "forget" if cards else None
        card = forget or ("crowded" if any(x["id"] == r["id"] for x in review.crowded(
            v, review.live_rows(v, events, self._today()))[0]) else None)
        live = {x["id"]: x for x in claims.rows(v) if x["status"] == "live"}
        view = self._clicks([("keepfold_patch", "keepfold_patch", {
            "id": f"me/{live[cid]['page']}", "op": "retire", "target": cid,
            "user_confirmed": True}, card if n == 0 else None, forget)
            for n, cid in enumerate(self._line_group(r["id"]) if card is None else [r["id"]])
            if cid in live])
        if not forget or r["id"] not in live:
            return view
        # R10: a //forget card's line then goes from every store; the answer carries the receipt
        from keepfold import forget as forget_
        from keepfold import session
        mark = v.writes
        receipt = forget_.claim(v, live[r["id"]]["page"], r["id"], also=tuple(cards))
        session._commit(v, "keepfold: forget a line", v.wrote_since(mark))
        return {**self.review(), "receipt": receipt, "receipt_lines": forget_.lines(receipt)}

    def review_crowded_keep(self, body: dict) -> dict:
        """G7: Keep, on a line the X-ray shows cut for space in most primes: the X-ray's Pin
        (prime serves it first, within the Preferences cap) and a `dismiss` change on id
        `crowded` holding its id's hash, stamped `crowded` for the monthly cap."""
        from keepfold import review, xray
        from keepfold.events import EventLog
        v, cid = self._vault(), body.get("id")
        if not any(x["id"] == cid for x in review.crowded(
                v, review.live_rows(v, EventLog(v).iter_events(), self._today()))[0]):
            raise NotFound
        with self.reviewing:
            xray.set_flag(v, "pin", xray.claim(cid), True)
        return self._click("keepfold_patch", "_dismiss", {
            "page": "crowded", "section": "Crowded", "text": cid, "waiting": False}, "crowded")

    def review_playbook_keep(self, body: dict) -> dict:
        """G7: Keep, on a playbook no AI app was served lately: a `dismiss` change on id
        `playbooks` holding its id's hash, so it is not asked about for review.UNUSED_DAYS;
        stamped `unused` for the monthly cap. The page stays as it is."""
        from keepfold import review
        from keepfold.events import EventLog
        v, pid = self._vault(), body.get("id")
        if not any(x["id"] == pid for x in review.unused(
                v, list(EventLog(v).iter_events()), self._today())[0]):
            raise NotFound
        return self._click("keepfold_patch", "_dismiss", {
            "page": "playbooks", "section": "Playbooks", "text": pid, "waiting": False},
            "unused")

    def _promoted(self, body: dict) -> dict:
        """R28a: the offered "Pin it to Core?" line (review.promote) whose id `body` names."""
        from keepfold import review
        from keepfold.events import EventLog
        v, cid = self._vault(), body.get("id")
        events = list(EventLog(v).iter_events())
        x = next((x for x in review.promote(v, review.live_rows(v, events, self._today()),
                                            events, self._today()) if x["id"] == cid), None)
        if x is None:
            raise NotFound
        return x

    def review_promote_pin(self, body: dict) -> dict:
        """R28a: Pin to Core: the line written to me/core's `## Core` as Approve writes a
        repeated correction there (`confirm: clicked`), stamped `promote_core` for the card's
        monthly cap."""
        from keepfold import claims, review
        x = self._promoted(body)
        return self._click("keepfold_patch", "keepfold_patch", {
            "id": "me/core", "op": "append_to_section", "target": "Core",
            "text": " ".join(claims._MARK.sub("", x["text"]).split()),
            "user_confirmed": True}, review.PROMOTE)

    def review_promote_later(self, body: dict) -> dict:
        """R28a: Not now: a `dismiss` change on id `promote_core` holding the line id's hash,
        so it is not offered for review.PROMOTE_QUIET days; stamped for the monthly cap."""
        from keepfold import review
        x = self._promoted(body)
        return self._click("keepfold_patch", "_dismiss", {
            "page": review.PROMOTE, "section": "Core", "text": x["id"], "waiting": False},
            review.PROMOTE)

    def _producer(self, p: dict) -> str:
        """RR-10: the producer of a waiting line (review.producer_of)."""
        from keepfold import review
        from keepfold.events import EventLog
        v = self._vault()
        return review.producer_of(review._proposals(EventLog(v).iter_events()), p["page"],
                                  p["text"])

    def review_resume(self, body: dict) -> dict:
        """RR-10: Resume a paused Review producer (a `resume` change; Status names it)."""
        from keepfold import fact_harvest, review
        from keepfold.events import EventLog
        p, v = body.get("producer"), self._vault()
        if not review.ledger(EventLog(v).iter_events()).get(p, {}).get("paused") and not (
                p == fact_harvest.PRODUCER and fact_harvest.ledger(v)["paused"]):
            raise NotFound
        return self._click("keepfold_patch", "_resume", {"producer": p})

    def review_conflict(self, body: dict) -> dict:
        """A conflict pair (plan 5b, D2: two of the owner's lines or two inferred ones that
        clash): `keep` both (a `keep_both` change, which also covers later near-copies of the
        pair, review.kept_cover), or `left` or `right`, which retires the other line and its
        near-copies (review.copies) through keepfold_patch's retire op."""
        from keepfold import review
        from keepfold.events import EventLog
        v, key, keep = self._vault(), body.get("key"), body.get("keep")
        if keep not in ("both", "left", "right"):
            raise ValueError("keep must be both, left or right.")
        events = list(EventLog(v).iter_events())
        rows = review.live_rows(v, events, self._today())
        pair = next((c for c in review.conflicts(rows, events, review.known_texts(v))
                     if c["key"] == key), None)
        if pair is None:
            raise NotFound
        card = "conflict"
        if keep == "both":
            return self._click("keepfold_patch", "_keep_both", {"page": pair["left"]["page"],
                                                              "pair": pair["key"]}, card)
        # D5: the other line's near-copies go too, so the card does not come back
        return self._clicks([("keepfold_patch", "keepfold_patch", {
            "id": f"me/{r['page']}", "op": "retire", "target": r["id"],
            "user_confirmed": True}, card if n == 0 else None, None)
            for n, r in enumerate(review.copies(rows, pair["right" if keep == "left"
                                                     else "left"]))])

    def _rule(self, body: dict) -> dict:
        from keepfold import corrections, review
        from keepfold.events import EventLog
        v, key = self._vault(), body.get("key")
        events = list(EventLog(v).iter_events())
        rows = review.live_rows(v, events, self._today())
        item = next((x for x in corrections.proposals(v, events, rows) if x["key"] == key), None)
        if item is None:
            raise NotFound
        return item

    def review_rule_approve(self, body: dict) -> dict:
        """A repeated correction (plan 5b) approved: me/core's `## Core` (a standing rule) or
        me/preferences' `## Personal` gets it as the user's own line (`corrections.bound`),
        written as Approve writes a waiting one (`confirm: clicked`)."""
        x = self._rule(body)
        return self._click("keepfold_patch", "keepfold_patch", {
            "id": f"me/{x['page']}", "op": "append_to_section", "target": x["section"],
            "text": x["text"],
            "user_confirmed": True}, "rule", "corrections")

    def review_rule_approve_all(self, body: dict) -> dict:
        """R3-4: Approve all, only on repeated corrections (mined patterns, each seen in 2+
        chats): every offered one of the shown `keys`, each written as its own Approve."""
        keys = body.get("keys")
        if not isinstance(keys, list):
            raise ValueError("keys must be the shown corrections' keys.")
        items = [x for k in dict.fromkeys(keys) if isinstance(k, str)
                 for x in [self._rule({"key": k})]]  # an unknown key is 404 before any write
        out = self.review()
        for x in items:
            out = self._click("keepfold_patch", "keepfold_patch", {
                "id": f"me/{x['page']}", "op": "append_to_section",
                "target": x["section"], "text": x["text"],
                "user_confirmed": True}, "rule", "corrections")
        return out

    def review_rule_dismiss(self, body: dict) -> dict:
        """A repeated correction dismissed: a `dismiss` change on page `corrections` holds its
        text's hash, so it is not offered again."""
        x = self._rule(body)
        return self._click("keepfold_patch", "_dismiss", {
            "page": "corrections", "section": "Core", "text": x["text"], "waiting": False}, "rule",
            "corrections")

    def _decision(self, body: dict) -> dict:
        from keepfold import more_sources
        from keepfold.events import EventLog
        v, key = self._vault(), body.get("key")
        item = next((x for x in more_sources.decision_proposals(v, EventLog(v).iter_events())
                     if x["key"] == key), None)
        if item is None:
            raise NotFound
        return item

    def review_decision_approve(self, body: dict) -> dict:
        """RR-9: a decision-like commit subject approved becomes a decision page (status
        accepted, the commit's day, linked to the repo page; private when that page is)."""
        x = self._decision(body)
        from datetime import date, timedelta

        from keepfold import loops  # follow up REVIEW_DAYS from the approve day, not the commit's
        due = date.fromisoformat(self._today()) + timedelta(days=loops.REVIEW_DAYS)
        fields = {"date": x["date"], "status": "accepted", "related": [x["page"]],
                  "review_on": due.isoformat(),
                  "summary": f"From my commit {x['hash'][:7]} in {x['project']} "
                             f"(\"{x['word']}\")."}
        if x["private"]:
            fields["private"] = True
        return self._click("keepfold_add", "_decide", {"title": x["subject"], "fields": fields,
                                                     "hash": x["key"]}, "decision",
                           "decisions")

    def review_decision_dismiss(self, body: dict) -> dict:
        """RR-9: a dismissed commit decision leaves a `dismiss` change on id `decisions` with its
        key, so it is not offered again."""
        x = self._decision(body)
        return self._click("keepfold_patch", "_dismiss", {
            "page": "decisions", "section": x["project"], "text": f"{x['page']} {x['hash']}",
            "waiting": False}, "decision", "decisions")

    def review_forget_dismiss(self, body: dict) -> dict:
        """A //forget card (keepfold.tags) dismissed: a `dismiss` change on page `forget` holds
        the hash of its event id, so the card closes; no line changes."""
        from keepfold import review, tags
        from keepfold.events import EventLog
        v, key = self._vault(), body.get("key")
        cards = tags.forget_cards(v, EventLog(v).iter_events(), self._today())
        if not any(x["key"] == key for x in cards):
            raise NotFound
        return self._clicks([("keepfold_patch", "_dismiss", {  # D5: the card's near-copies too
            "page": "forget", "section": "Forget", "text": k, "waiting": False},
            "forget" if n == 0 else None, "forget")
            for n, k in enumerate(review.group(cards, key))])

    def review_import_dismiss(self, body: dict) -> dict:
        """RR-18: a card for a hand-edited imported page dismissed: a `dismiss` change on id
        `imports` holding its key; both pages stay as they are."""
        from keepfold import sources
        from keepfold.events import EventLog
        v, key = self._vault(), body.get("key")
        if not any(x["key"] == key for x in sources.edited_pages(v, EventLog(v).iter_events())):
            raise NotFound
        return self._click("keepfold_patch", "_dismiss", {
            "page": "imports", "section": "Imports", "text": key, "waiting": False})

    def review_unsafe_keep(self, body: dict) -> dict:
        """G6: Keep it, on a line that reads as an instruction to an AI or holds invisible
        characters: a `dismiss` change on id `unsafe` holding its key, so it is served again
        (review.unsafe_lines); the line stays as it is. Keep it out is Retire."""
        from keepfold import review
        from keepfold.events import EventLog
        v, key = self._vault(), body.get("key")
        lines = review.unsafe_lines(v, EventLog(v).iter_events())
        if not any(x["key"] == key for x in lines):
            raise NotFound
        hidden = review._private(v)
        return self._clicks([("keepfold_patch", "_dismiss", {
            "page": "unsafe", "section": "Unsafe", "text": k, "waiting": False}, None, None)
            for k in review.group([x for x in lines if x["page"] not in hidden], key)])  # D5

    def _outside(self, body: dict) -> dict:
        """R5a: the card's line changed outside Keepfold (review.outside_lines), by its key."""
        from keepfold import review
        from keepfold.events import EventLog
        v, key = self._vault(), body.get("key")
        hidden = review._private(v)
        x = next((x for x in review.outside_lines(v, EventLog(v).iter_events())
                  if isinstance(key, str) and x["key"] == key and x["page"] not in hidden), None)
        if x is None:
            raise NotFound
        return x

    def review_outside_keep(self, body: dict) -> dict:
        """R5a: Keep, on a line changed outside Keepfold: a `dismiss` change on id `outside`
        holding its key, so AI apps are served it from now on; the line stays as it is."""
        x = self._outside(body)
        return self._click("keepfold_patch", "_dismiss", {
            "page": "outside", "section": "Outside", "text": x["key"], "waiting": False})

    def review_outside_remove(self, body: dict) -> dict:
        """R5a: Remove, on a line changed outside Keepfold: a claim line is retired to `## Past`
        (keepfold_patch retire), any other line deleted (Keepfold._remove_outside); undoable."""
        x = self._outside(body)
        if x["id"] and x["page"] != "teaching-contract":
            return self._click("keepfold_patch", "keepfold_patch", {
                "id": f"me/{x['page']}", "op": "retire", "target": x["id"],
                "user_confirmed": True})
        return self._click("keepfold_patch", "_remove_outside", {"page": x["page"],
                                                               "key": x["key"]})

    # ---- Tonight (Gemini round 3 "Nightly Closeout"): close the day ----
    def tonight(self) -> dict:
        """The Tonight tab (keepfold.tonight) for the current Keepfold day, plus the Review view
        its Today's 5 board shows; read-only, shown text only."""
        from datetime import UTC, datetime

        from keepfold import tonight
        from keepfold.events import EventLog
        v = self._vault()
        return {**tonight.view(v, list(EventLog(v).iter_events()), datetime.now(UTC)),
                "review": self.review()}

    def tonight_undo(self, body: dict) -> dict:
        """Undo one AI app's session shown under "What AI apps learned today", as `keepfold
        undo <session>` does (the CLI's own code, as a job). A session not shown there
        (unknown, or already undone: its events went with it) is 404. A session still open
        has no commit yet, so it is refused rather than "undone" with nothing reverted."""
        from keepfold import cli
        from keepfold.events import EventLog
        sid, root = body.get("session"), self.need_root()
        if not isinstance(sid, str) or sid not in {
                g["session"] for g in self.tonight()["learned"]}:
            raise NotFound
        if not any(e.get("session") == sid and e.get("kind") == "close"
                   for e in EventLog(self._vault()).iter_events()):
            raise ValueError("This AI app's session is still open, so nothing is saved to undo "
                             "yet. Undo works once the session closes.")

        def run(job):
            try:
                return actions.capture(job, cli.undo, sid, vault=root)
            finally:
                with self.reviewing:  # never mid-click: the Review clicks' Keepfold reloads
                    self.core = None  # the reverted catalog

        return self.jobs.start("Undoing an AI app's changes", "tonight", run)
