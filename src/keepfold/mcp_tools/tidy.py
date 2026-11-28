"""keepfold_reflect (pack, lint, split) and `tidy`, what Keepfold settles itself: wrong merges,
old guesses, the inbox. A mixin of `keepfold.mcp_server.Keepfold`."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from keepfold import claims, review, xray
from keepfold import lint as lint_mod
from keepfold.events import local_day
from keepfold.index import _wrap
from keepfold.mcp_server import (
    GUESS_DAYS,
    REFLECT_HARVEST_NEXT,
    REFLECT_NEXT,
    SPLITS_MAX,
    STEER_PAGES,
    TIDY_FILE,
)
from keepfold.reflect import reflect
from keepfold.vault import VaultError, invalid

if TYPE_CHECKING:  # build_server puts the SDK's names in this module's globals
    from mcp.server.mcpserver import Context


class TidyTools:
    """keepfold_reflect, `tidy` and `_old_guesses`."""

    def _lint(self) -> dict[str, Any]:
        findings, totals = lint_mod.lint_report(
            self.vault, self._now().astimezone().replace(tzinfo=None), self._withheld())
        more = lint_mod.not_shown(findings, totals)
        return {"findings": findings, "totals": totals,
                "next": "Fix findings with keepfold_patch or keepfold_add; most severe first"
                + (f"; not shown: {more}." if more else ".") if findings else "Nothing to fix."}

    def keepfold_reflect(self, action: Literal["pack", "lint", "split"] = "pack",
                       hub: str | None = None, hash: str | None = None,
                       children: list[dict[str, Any]] | None = None, key: str | None = None,
                       ctx: Context | None = None) -> dict[str, Any]:
        if action == "lint":
            return self._lint()
        if action == "split":
            if hub is None or hash is None or children is None:
                raise invalid("action=split needs hub, hash and children from a splits entry.")
            return self._split(hub, hash, children)
        out = reflect(self.vault, self.catalog, self._now())
        # inbox.md text was written by the model: fence it like keepfold_read does
        out["unconfirmed"] = [{**u, "text": _wrap("inbox", u["text"])}
                              for u in out["unconfirmed"]]
        out["old_fleeting"] = [{**f, "text": _wrap("inbox", f["text"])}
                               for f in out["old_fleeting"]]
        out["resurface"] = self._resurface_rows(3)
        pack = out["portrait"]["pack"]
        pack["personality"] = [_wrap("personality", t) for t in pack["personality"]]
        pack["journal_sample"] = [_wrap("journal", t) for t in pack["journal_sample"]]
        sv = out["saved"]
        sv["claims"] = [{**r, "section": _wrap("claim", r["section"]),
                         "text": _wrap("claim", r["text"])} for r in sv["claims"]]
        sv["changes"] = [{**c, "target": _wrap("change", c["target"])} for c in sv["changes"]]
        out["spot_check"] = [{**r, "question": _wrap("spot_check", r["question"]),
                              "answer": _wrap("spot_check", r["answer"])}
                             for r in out["spot_check"]]  # tool input text, untrusted
        out["splits"] = self._split_pack()
        # dogfood r4 #12: reflect.md's 5-minute default, not every part; in a harvest, step 2
        harvesting = getattr(self.session, "mode", None) == "history"
        return {**out, "next": REFLECT_HARVEST_NEXT if harvesting else REFLECT_NEXT + (
            " On a yes to more: for each spot_check row, show question, answer and verdict and "
            "ask keep or flip; record it with keepfold_log(kind='spot_check', payload={event_id, "
            "verdict}) (a flip regrades it)." if out["spot_check"] else "") + (
            " On a yes to clusters: per splits entry, name each cluster (a 2-6 word title, a "
            "12-30 word description from its terms and titles), show the user, then call "
            "keepfold_reflect(action='split', hub, hash, children)." if out["splits"] else "")}

    def _split_pack(self) -> list[dict]:
        """REVIEW-1005 §6.2: split proposals for at most SPLITS_MAX big hubs (none without the
        embedder). Titles and terms are note text, so they are fenced."""
        from keepfold import semantic, topics

        embed = semantic.load_embedder()  # never downloads
        if embed is None:
            return []
        rows: list[dict] = []
        try:
            idx = self.index
            idx.refresh()
            t = topics.tree(idx)
            stack = None
            for h, _ in topics.split_candidates(idx, t):
                if len(rows) >= SPLITS_MAX:
                    break
                if stack is None:
                    stack = topics.head_stack(idx, embed)
                p = topics.propose_split(idx, t, stack, h)
                if p is None:
                    continue
                topics.write_proposal(self.vault, p)
                rows.append({"hub": h, "title": _wrap("split", t.title[h]), "hash": p["hash"],
                             "clusters": [{"n": len(c["members"]), "terms": c["terms"],
                                           "titles": _wrap("split", "\n".join(c["sample_titles"]))}
                                          for c in p["clusters"]]})
        except Exception as e:  # a review pack must not fail over a broken model or hub
            logging.getLogger(__name__).warning("split proposals skipped: %s", e)
        return rows

    def _split(self, hub: str, hash: str, children: list[dict[str, Any]]) -> dict[str, Any]:
        from keepfold import semantic, topics

        embed = semantic.load_embedder()  # never downloads
        if embed is None:
            raise invalid(semantic.NOT_SET_UP)
        res = topics.apply_split(self.vault, self.index, embed, hub, hash, children)
        for c in res["children"]:
            self.sessions.note("added", change={"tool": "add", "id": c["id"], "kind": "note",
                                                "op": "create", "target": None,
                                                "note_type": "structure"})
        hint = self.sessions.note("patched", change={"tool": "split", "id": hub, "kind": "note",
                                                     "op": "split", "target": res["moved"]})
        return {"children": res["children"], "stayed": res["stayed"],
                "next": f"Show the user the new sub-topics of [[{hub}]] ({res['moved']} pages "
                        "moved). If they want it gone, `keepfold undo <session id>` reverts the "
                        "session commit after keepfold_end." + hint}

    def _tidy_state(self) -> dict:
        """TIDY_FILE as a dict ({} when missing or unreadable)."""
        try:
            got = json.loads((self.vault.root / TIDY_FILE).read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return got if isinstance(got, dict) else {}

    def _tidy_done(self) -> set[str]:
        """D1/D2: the pair keys and `page/id` lines Keepfold settled itself (TIDY_FILE), so a
        retire the user undid is never redone."""
        got = self._tidy_state().get("done")
        return {k for k in got if isinstance(k, str)} if isinstance(got, list) else set()

    def _wrong_merges(self, events: list[dict]) -> list[dict]:
        """0.4.1: the lines the 0.4.0 sweep retired as a duplicate, update or opposite of a
        different fact (`review.apart`), still in `## Past` of a non-private page:
        [{page, id, section}], `section` the one keepfold_patch appended the line to, if
        recorded. [] once TIDY_FILE says `repaired`."""
        if self._tidy_state().get("repaired"):
            return []
        voided, private = review._voided(events), review._private(self.vault)
        known = {(r["page"], r["id"]): r for r in claims.rows(self.vault)}
        where, out = {}, {}
        for e in events:
            c = review._payload(e).get("change") if e.get("kind") == "change" else None
            if not isinstance(c, dict) or e.get("id") in voided:
                continue
            ids = c.get("claim") if isinstance(c.get("claim"), list) else [c.get("claim")]
            if c.get("op") == "append_to_section":
                where.update({(c.get("id"), i): c.get("target") for i in ids})
            kept = c.get("kept") if isinstance(c.get("kept"), dict) else {}
            r, k = known.get((c.get("id"), c.get("claim"))), \
                known.get((kept.get("page"), kept.get("claim")))
            if (c.get("auto") is True and c.get("op") == "retire" and r and k
                    and c.get("why") in ("duplicate", "update", "opposite")
                    and r["status"] == "past" and r["page"] not in private
                    and review.apart(r["text"], k["text"])):  # norm drops the (until …) stamp
                out[(r["page"], r["id"])] = None
        return [{"page": p, "id": i, "section": where.get((p, i))} for p, i in out]

    def _repair_merges(self, wrong: list[dict]) -> int:
        """0.4.1, once: each `_wrong_merges` line goes back from `## Past` to its section,
        recorded as an `auto` change (why `repair`) in the open session, so `keepfold undo`
        takes it back; then TIDY_FILE says `repaired`. Their pair keys stay in `done`."""
        restored = 0
        for w in wrong:
            try:
                self.vault._mutate(self.vault.path_for("me", w["page"]),
                                   lambda t, w=w: claims.restore_from_past(
                                       t, w["page"], w["id"], w["section"]))
            except VaultError:
                continue
            self.sessions.note("patched", change={
                "tool": "patch", "id": w["page"], "kind": "me", "op": "restore",
                "target": w["id"], "claim": w["id"], "auto": True, "why": "repair"})
            restored += 1
        self.vault.write_text(self.vault.root / TIDY_FILE,
                              json.dumps({**self._tidy_state(), "repaired": 1}))
        return restored

    def _auto_retire(self, r: dict, keep: dict | None, why: str, since: str | None = None
                     ) -> bool:
        """D1/D2: retire live claim row `r` to `## Past` as keepfold_patch's retire does, for
        `keep` ({page, id}; None for G5b-2's unused guess, which no line replaces), recorded as
        a change (card `duplicate` or `conflict`, `auto`, `why`, `kept`) in the open session, so
        `keepfold undo` takes it back; R13a: `keep` gets `(since SINCE)` (`_stamp_since`). Its
        pair key and `page/id` go to TIDY_FILE so the sweep never redoes an undone retire.
        False when the line is no longer live."""
        day = local_day(self._now())
        try:
            self.vault._mutate(self.vault.path_for("me", r["page"]),
                               lambda t: claims.move_to_past(t, r["page"], r["id"], day)[0])
        except VaultError:
            return False
        change = {"tool": "patch", "id": r["page"], "kind": "me", "op": "retire",
                  "target": r["id"], "claim": r["id"], "auto": True, "why": why}
        done = self._tidy_done() | {f"{r['page']}/{r['id']}"}
        if keep is not None:
            change.update(card="duplicate" if why == "duplicate" else "conflict",
                          kept={"page": keep["page"], "claim": keep["id"]})
            change.update(self._stamp_since(keep, since))
            done.add(claims.pair_key((r["page"], r["id"]), (keep["page"], keep["id"])))
        self.sessions.note("patched", change=change)
        with self.vault.lock():  # the app's sweep writes it too, from another process
            done |= self._tidy_done()
            self.vault.write_text(self.vault.root / TIDY_FILE,
                                  json.dumps({**self._tidy_state(), "done": sorted(done)[-5000:]}))
        return True

    def _stamp_since(self, keep: dict, since: str | None) -> dict:
        """R13a: `(since SINCE)` on the live claim line `keep` ({page, id}) that replaced a
        retired one, unless it has a since or until day already: {since, kept: {page, claim[,
        line: its R5a hash on a review.GUARDED page]}} for the retire's change event; {} when
        nothing was stamped."""
        got: dict = {}

        def edit(t: str) -> str:
            new, got["line"] = claims.stamp_since(t, keep["page"], keep["id"], since)
            return new

        try:
            if not since or not self.vault._mutate(self.vault.path_for("me", keep["page"]), edit):
                return {}
        except VaultError:
            return {}
        line = {"line": review.line_hash(got["line"])} if keep["page"] in review.GUARDED else {}
        return {"since": since, "kept": {"page": keep["page"], "claim": keep["id"], **line}}

    def _old_guesses(self, events: list[dict], day: str) -> list[dict]:
        """G5b-2, at most once a day (TIDY_FILE's `guesses` day): the live guesses (tier agent
        or exposed: no yes, no Approve) of the non-private claim pages but me/core, written over
        GUESS_DAYS days ago, that no X-ray call served and no one confirmed or said again
        (review.use), not X-ray pinned, and not a retire the owner undid. Only a guess the
        X-ray saw all of: its oldest call is on or before the guess's day. A21: never a line
        the fact harvest quoted from the user's own words (review.quoted); the Review asks
        Still true? about it instead (review.stale).
        shortcut: a busy owner's ring (xray.CAP calls) may never reach 30 days back, and then
        nothing retires; a per-day fold of served ids if that matters."""
        ring = [str(r.get("ts")) for r in xray.rows(self.vault)  # R27b: find, recall and read
                if r.get("tool") not in xray.READS]  # rows reach further back (READ_CAP)
        if self._tidy_state().get("guesses") == day or not ring:
            return []
        try:
            first = local_day(datetime.fromisoformat(min(ring)))
        except ValueError:
            return []
        cut = (datetime.fromisoformat(day) - timedelta(days=GUESS_DAYS)).date().isoformat()
        used, done = review.use(self.vault, events), self._tidy_done()
        pins, said = xray.settings(self.vault)["pin"], review.quoted(events)
        return [r for r in review.live_rows(self.vault, events, day)
                if r["tier"] in review.INFERRED and r["page"] != "core" and r["id"] not in said
                and first <= str(r["day"] or "") < cut and r["id"] not in used
                and xray.claim(r["id"]) not in pins and f"{r['page']}/{r['id']}" not in done]

    def tidy(self) -> dict:
        """D1-D3, the app's sweep (at start and with each chat-import pass): the about-me lines
        waiting in inbox.md move to their claim pages as inferred lines (`_migrate_inbox`),
        then `review.settle` retires up to review.TIDY_MAX lines (near-copies, settled pairs)
        under the vault lock, in one session closed at once (one `keepfold undo`). Opens no
        session when there is nothing to do. First, once (0.4.1), `_repair_merges` puts back
        the lines the 0.4.0 sweep retired for a different fact. R13a: a kept line that replaced
        a retired one says `(since …)` (review.since_for). G5b-2: last, once a day, the old
        guesses no app used (`_old_guesses`) retire the same way, and the receipt says how
        many. {moved, retired[, restored][, guesses]}."""
        guesses = 0
        with self.vault.lock():
            day = local_day(self._now())
            waiting = [p for p in review.pending(self.vault, day)
                       if p["page"] in claims.PAGES and p["page"] not in STEER_PAGES]
            events = list(self.log.iter_events())
            plan = review.settle(review.live_rows(self.vault, events, day), events,
                                 review.known_texts(self.vault),
                                 self._tidy_done())["retire"][:review.TIDY_MAX]
            wrong = self._wrong_merges(events)
            old = self._old_guesses(events, day)
            if not waiting and not plan and not wrong and not old:
                return {"moved": 0, "retired": 0}
            self.session = self.sessions.start("tidy")
            self._reset()
            try:
                restored = self._repair_merges(wrong) if wrong else 0
                moved = self._migrate_inbox(waiting)
                events = list(self.log.iter_events())
                plan = review.settle(review.live_rows(self.vault, events, day), events,
                                     review.known_texts(self.vault),
                                     self._tidy_done())["retire"][:review.TIDY_MAX]
                retired = sum(self._auto_retire(x["retire"], x["keep"], x["why"],
                                                review.since_for(x["retire"], x["keep"], day,
                                                                 x["newer"]))
                              for x in plan)
                if old:  # the lines as the sweep left them
                    guesses = sum(self._auto_retire(r, None, "unused") for r in
                                  self._old_guesses(list(self.log.iter_events()), day))
                    self.vault.write_text(self.vault.root / TIDY_FILE,
                                          json.dumps({**self._tidy_state(), "guesses": day}))
            finally:
                if self.sessions.current is not None:
                    self.sessions.close(summary="Keepfold tidied the about-me lines" + (
                        f". Keepfold retired {guesses} old guess{'es' if guesses != 1 else ''} "
                        "no app used" if guesses else ""))
        return {"moved": moved, "retired": retired, **({"restored": restored} if restored else {}),
                **({"guesses": guesses} if guesses else {})}

    def _migrate_inbox(self, waiting: list[dict]) -> int:
        """D3, once per waiting line: an about-me line under `## About me (unconfirmed)` (claim
        pages but me/core and me/setup; teaching rules and portrait lines still wait) is
        written to its page as keepfold_patch writes an inferred line, then leaves inbox.md. A
        line claims.check refuses stays where it is; one under the page's `## Not me`, or a
        near-copy of or a clash with a line that stays, just leaves the inbox."""
        moved = 0
        for p in waiting:
            if claims.unsafe(f"{p['section']}\n{p['text']}", p["page"]):
                continue
            try:
                out = self.keepfold_patch(f"me/{p['page']}", "append_to_section", p["section"],
                                        p["text"])
            except VaultError:
                continue
            moved += bool(out.get("changed"))
            self._unpropose(f"{p['section']}: {p['text']}")
        return moved
