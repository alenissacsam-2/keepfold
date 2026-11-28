"""keepfold_patch and the claim operations it and the app's Review clicks run (settle, supersede,
dismiss, decide, keep both, resume). A mixin of `keepfold.mcp_server.Keepfold`."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Annotated, Any, Literal

from keepfold import access, chats, claims, dates, loops, render, review, voice
from keepfold.catalog import KINDS as CATALOG_KINDS
from keepfold.events import local_day
from keepfold.index import _wrap, link_targets
from keepfold.mcp_server import (
    CONFIRM_PAGES,
    HELD_NEXT,
    HUB_GENERATED,
    MORE_NEXT,
    PATCH_TARGET,
    PIN_KINDS,
    SAID,
    STEER_PAGES,
    SUPERSEDES_MAX,
    SUPERSEDES_NEXT,
    _ask_click,
    _pin_day,
    _update_of,
    _Yes,
)
from keepfold.reflect import UNCONFIRMED
from keepfold.vault import ID_RE, VaultError, invalid, scan_secrets

if TYPE_CHECKING:  # build_server puts the SDK's names in this module's globals
    from mcp.server.mcpserver import Context, ElicitationResult, Resolve


class PatchTools:
    """keepfold_patch and the claim operations behind it."""

    def _unsaid(self, line: str) -> str | None:
        """R3-2: why a relayed yes on `line` is not the user's: this chat read outside text, or
        its live log (the prime prompt, chat_turn) holds user turns and none says the line (at
        least SAID of its content words, review._bag). None: the yes stands (no log: as before)."""
        if self.session.exposed:
            return "this chat read outside text"
        if not self._chat:
            return None
        try:
            rows = (chats.export_root() / chats.LIVE / f"{self._chat[0]}.jsonl").read_text(
                "utf-8").splitlines()
        except OSError:
            return None
        turns = []
        for x in rows:
            try:
                r = json.loads(x)
                if r.get("type") == "user":
                    turns.append(review._bag(str(r["message"]["content"])))
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        want = review._bag(line)
        if not turns or not want or any(len(want & t) >= SAID * len(want) for t in turns):
            return None
        return "the user's messages in this chat do not say it"

    def _note_ref(self, ref: str) -> tuple[str, str]:
        """'me/profile', 'concepts/x' or a bare id or concept name -> (kind, id)."""
        if "/" in ref:
            folder, _, id = ref.removesuffix(".md").partition("/")
            kinds = {d: k for k, d in self.vault.KIND_DIRS.items()}
            if folder not in kinds:
                raise invalid(f"Unknown folder '{folder}'.")
            return kinds[folder], id
        found = [k for k in self.vault.KIND_DIRS
                 if ID_RE.fullmatch(ref) and self.vault.path_for(k, ref).is_file()]
        if len(found) > 1:
            raise invalid(f"'{ref}' exists as {', '.join(found)}; write it as <folder>/{ref}.")
        if found:
            return found[0], ref
        id = self.catalog.resolve(ref, CATALOG_KINDS)  # unknown -> did_you_mean
        return self.catalog.entries[id].kind, id

    def _propose(self, text: str, heading: str = UNCONFIRMED, page: str | None = None) -> None:
        """Append one line under `heading` (default `## About me (unconfirmed)`) in inbox.md
        (created if missing)."""
        scan_secrets(text)
        claims.check(text, page)  # G6: every waiting line is an about-me line or a teaching rule

        def add(page: str) -> str:  # B13: reruns on the reread page if the owner edited it
            lines = page.rstrip("\n").split("\n")
            if heading not in lines:
                lines += ["", heading]
            at = lines.index(heading) + 1
            while at < len(lines) and not lines[at].startswith("#"):
                at += 1
            while lines[at - 1].strip() == "" and lines[at - 1] != heading:
                at -= 1  # keep the blank line before the next heading
            lines.insert(at, text)
            return "\n".join(lines) + "\n"

        self.vault.update_text(self.vault.root / "inbox.md", add, "# Inbox\n")

    def keepfold_patch(self, id: str, op: Literal["append_to_section", "set_field", "check_item",
                                                "revise", "retire", "reject", "set_topics"],
                     target: str = "", text: str = "", key: str | None = None,
                     user_confirmed: bool = False, until: str | None = None,
                     supersedes: list[str] | None = None,
                     ctx: Context | None = None,
                     click: Annotated[ElicitationResult[_Yes], Resolve(_ask_click)] = None,
                     ) -> dict[str, Any]:
        kind, nid = self._note_ref(id)
        text = chats.drop_private(text)  # L1: a <private> span is never written
        # dogfood r4 #3: target defaults to "" so a missing one gets these messages, not pydantic's
        if op in PATCH_TARGET and not target.strip():
            raise invalid(f"{op} needs target: {PATCH_TARGET[op]}.")
        # idea 11: `click` is the user's own answer in Keepfold's dialog, filled by the SDK, never
        # tool input: accept carries data (None when no dialog was shown), decline and cancel not
        said = getattr(click, "data", getattr(click, "action", None))
        if isinstance(said, _Yes):
            said = "yes" if said.yes else "no"
        if said not in (None, "yes"):
            return {"changed": False, "declined": True,
                    "summary": f"the user said no in Keepfold's dialog ({said}); nothing written "
                               f"to {kind} {nid}",
                    "next": "The user declined this line themself. Do not save it another way "
                            "and do not ask about it again in this chat."}
        if (op == "append_to_section" and kind == "me" and text.strip() and "\n" not in text.strip()
                and nid in claims.PAGES and not text.lstrip().startswith(("- ", "#"))):
            text = "- " + text.strip()  # a bare line would be no claim at all
        if kind == "me" and op in ("append_to_section", "revise"):  # G6: before any write path
            claims.check(f"{target}\n{text}" if op == "append_to_section" else text, nid)
        sup = None if supersedes is None else self._supersedes(supersedes, kind, nid, op, text)
        # CONSENSUS item 3: every prime serves these as instructions, so outside text may not
        if kind == "me" and self.session.exposed and not self.app_clicks and op in (
                "append_to_section", "revise", "retire") and nid in (
                "teaching-contract", "profile"):  # B3: no me/ page takes set_field
            raise VaultError("refused_exposed", "This chat has read outside text (a source, a raw "
                             f"file or a past chat), so it cannot change me/{nid}.md.")
        # R3-2: a relayed yes counts only from a clean chat whose logged user turns say the line;
        # the owner's clicks (the app, Keepfold's own dialog) are never downgraded
        if (user_confirmed and said != "yes" and not self.app_clicks and kind == "me"
                and nid in claims.PAGES and op in ("append_to_section", "revise")
                and (why := self._unsaid(dates.unpin(text)))):  # a pinned day was not typed
            if op == "revise" and nid in CONFIRM_PAGES:  # its unconfirmed path: a new line waits
                cid = claims.resolve(self.vault, target, local_day(self._now()))
                section = next((r["section"] for r in claims.rows(self.vault)
                                if r["id"] == cid and r["page"] == nid), None)
                if section is None:
                    raise invalid(f"{target} is not a live claim on me/{nid}; read it first.")
                op, until, target = "append_to_section", None, section
                supersedes = supersedes or [cid]  # Approve then offers to replace the old line
            out = self.keepfold_patch(id, op, target, text, key, False, until, supersedes, ctx)
            if out.get("queued"):
                out["next"] = (f"Saved as unconfirmed ({why}): it waits for the user's Approve "
                               "in the Keepfold app; say so in one line and go on.")
            elif out.get("next", "").startswith(HELD_NEXT):  # G5b-6: no app sees it yet
                out["next"] = HELD_NEXT + "say so in one line and go on."
            elif out.get("changed"):  # D3: a claim line, written now, served (ask)
                out["next"] = (f"Saved now but unconfirmed ({why}): AI apps see it marked (ask) "
                               "until the user confirms it; say so in one line and go on.")
            return out
        if op in ("append_to_section", "revise") and kind in PIN_KINDS and (
                today := _pin_day(self)):  # L1, after R3-2's check of the typed words
            text = dates.pin(text, today)
        claim_page = kind == "me" and nid in claims.PAGES
        contract = kind == "me" and nid == "teaching-contract"
        if contract and not self.app_clicks:  # E2: only Approve in the Keepfold app applies a rule
            if op != "append_to_section":
                raise invalid("The teaching contract changes only by Approve in the Keepfold app: "
                              "propose a rule with op=append_to_section; it waits there.")
            user_confirmed = False  # a yes relayed by the model is not the user's click
        harvesting = self.sessions.harvesting
        if harvesting:
            self._harvest_patch(kind, nid, op, target, text, claim_page, user_confirmed)
        if until is not None and op not in ("revise", "retire"):
            raise invalid("until works only with revise and retire.")
        if op in ("revise", "retire", "reject"):
            if not claim_page:
                raise invalid(f"{op} works only on the claim pages: me/"
                           + ", me/".join(claims.PAGES) + ".")
            return self._held_command(
                self._claim_op(nid, op, target, text, user_confirmed, until, said == "yes"), text)
        changed = True
        day = local_day(self._now())
        if sup is None and self.app_clicks and claim_page and op == "append_to_section":
            sup = self._proposed_supersedes(nid, text)  # Approve: the proposal's own pair
        # plan 5b: a line that may update a live one; the write goes ahead and the agent asks.
        # RR-4: an explicit `supersedes` names the pair, so the guess is not needed
        update = (review.collision(self.vault, nid, text, day)
                  if op == "append_to_section" and kind == "me" and text.strip() and not sup
                  else None)
        # plan v9 item 8: a profiled client is shown no line of a page outside its scopes (a
        # sectioned page's line has no section here, so none); the pair is recorded all the same
        scopes = self._scopes()
        shown = update if scopes is None or (update and update["page"] not in access.SECTIONED
                                             and access.scope_page(scopes, update["page"])) \
            else None
        # D3: a claim page takes an inferred line at once (tier agent, served (ask)); the
        # portrait, a teaching rule and me/core and me/setup (copied into every agent's rules
        # file) wait for Approve, and while harvesting, old chats are stale evidence: every
        # claim page waits for a yes
        if (op == "append_to_section" and kind == "me" and not user_confirmed
                and ((nid in CONFIRM_PAGES and (not claim_page or nid in STEER_PAGES))
                     or (harvesting and claim_page))):
            text = " ".join(text.split()).removeprefix("- ")
            if (not target.strip() or "\n" in target or "\r" in target
                    or target.lstrip().startswith("#") or len(target) > 200):
                raise invalid("target must be a one-line section name.")  # as _ensure_section
            if claim_page and claims.is_not_me(nid, self._raw(f"me/{nid}.md"), text):
                return {"changed": False, "queued": False,
                        "summary": f"not proposed: me/{nid} lists it under ## Not me",
                        "next": "The user said this is not them; do not propose it again."}
            if not claim_page and review.dismissed(self.log.iter_events(), nid, text):
                return {"changed": False, "queued": False,
                        "summary": f"not proposed: the user dismissed it for me/{nid} in the "
                                   "Keepfold app",
                        "next": "The user dismissed this line; do not propose it again."}
            if harvesting and self._about_me_budget() <= 0:
                return {"changed": False, "queued": False,
                        "summary": "not proposed: the harvest's about-me budget is used up",
                        "next": "Ask the user about the waiting lines now (a yes/no card), or "
                                "leave them for /keepfold reflect; go on without about-me lines."}
            self._propose(f"- {target}: {text} (proposed {day}, me/{nid})", page=nid)  # Approve
            self.proposed.append(render.plain(f"{target}: {text}", 200))  # as unconfirmed()
            hint = self.sessions.note("patched", change={
                "tool": "propose", "id": nid, "kind": kind, "op": op, "target": target,
                "hash": review.text_hash(text), **({"supersedes": sup} if sup else {}),
                "producer": "imports" if harvesting else "chats"})  # RR-10
            if contract:
                return _update_of(shown, {
                    "changed": False, "queued": True,
                    "summary": "proposed for me teaching-contract; waiting for your Approve in "
                               "the Keepfold app",
                    "next": "Tell the user the rule is waiting for their Approve in the Keepfold "
                            "app (Review); it does not apply until then, and repeating the "
                            "patch does not apply it." + hint})
            return _update_of(shown, {
                "changed": False, "queued": True,
                **({"supersedes": [s["claim"] for s in sup]} if sup else {}),
                "summary": f"proposed for {kind} {nid}; awaits the user's OK",
                "next": "Saved as unconfirmed; it waits for the user's Approve in the Keepfold app "
                        "(Review). Say so in one line; don't stop to ask." + hint})
        replaced: list[dict] = []
        if op == "append_to_section" and claim_page and not user_confirmed:
            if (not target.strip() or "\n" in target or "\r" in target
                    or target.lstrip().startswith("#") or len(target) > 200):
                raise invalid("target must be a one-line section name.")  # as _ensure_section
            if claims.is_not_me(nid, self._raw(f"me/{nid}.md"), text):
                return {"changed": False, "queued": False,
                        "summary": f"not saved: me/{nid} lists it under ## Not me",
                        "next": "The user said this is not them; do not save it again."}
        if op == "append_to_section" and claim_page and "\n" not in text.strip():
            settled = self._settle_new(nid, target, text, day, user_confirmed, said == "yes",
                                       update, sup)
            if settled.get("lost"):
                return settled["lost"]
            replaced = settled["retire"]
            # R13a: the new line replaces these, so it says since when it holds
            since = min((s for x in replaced if (s := review.since_for(
                x["retire"], {"text": text}, day, True))), default=None)
            if since and not claims.since(text) and "(until" not in text:
                text = f"{text.rstrip()} (since {since})"
        if op == "append_to_section" and kind == "me" and nid in CONFIRM_PAGES:
            if user_confirmed or not claim_page:  # D3: an inferred claim line has no marker
                text = f"{text.rstrip()} (confirmed {day})"
            if nid == "portrait":
                self._portrait_section(target)
            elif nid == "voice":
                self._voice_section(target)
            elif claim_page or contract:  # a proposal may have named a section the page lacks
                self._ensure_section(nid, target)
        claim = None
        if op == "append_to_section" and claim_page:
            with self.vault.lock():  # ids must not clash with a line written meanwhile
                text, ids = claims.add_ids(nid, self._raw(f"me/{nid}.md"), text, day)
                self.vault.append_to_section(kind, nid, target, text)
                if user_confirmed:
                    self._unpropose(text)
            claim = (ids[0] if len(ids) == 1 else ids) or None
        elif op == "append_to_section":
            self.vault.append_to_section(kind, nid, target, text)
            if self.app_clicks and kind == "me" and nid in CONFIRM_PAGES:
                self._unpropose(text)  # Approve: the line no longer waits in the Review
        elif op == "set_topics":  # REVIEW-1005 §5.4: the one write path for a page's topics
            from keepfold import topics

            if kind not in topics.PAGE_KINDS:
                raise invalid("set_topics works only on resource and note pages.")
            hubs = [s.strip() for s in text.split(",") if s.strip()]
            changed = topics.place(self.vault, nid, hubs, kind)
            target = ", ".join(hubs) or "none"
        elif op == "set_field":
            value: Any = text
            if target == "current_day":
                if not text.strip().isdigit():
                    raise invalid("current_day must be a whole number.")
                value = int(text)
            self.vault.set_field(kind, nid, target, value)
        else:
            changed = self.vault.check_item(kind, nid, target, text)
        change = {"tool": "patch", "id": nid, "kind": kind, "op": op, "target": target}
        if kind == "me" and nid in review.GUARDED and op == "append_to_section":
            # R5a: what Keepfold wrote here, so review.outside_lines knows it from an outside edit
            change["lines"] = [review.line_hash(ln) for ln in (f"## {target.strip()}",
                                                               *text.splitlines()) if ln.strip()]
        if claim:
            change.update(claim=claim, **self._tier(user_confirmed, said == "yes"))
            if update and isinstance(claim, str):  # the pair the app's Review shows
                change["update_of"] = {"page": update["page"], "claim": update["id"]}
            if sup and isinstance(claim, str):  # RR-4: explicit pairs, shown alike
                change["supersedes"] = sup
        elif kind == "note":  # G4, R5b: a playbook is withheld until a confirmed write
            change.update(self._tier(user_confirmed, said == "yes"))
        if self.app_clicks:
            change["confirm"] = "clicked"
        hint = self.sessions.note("patched", change=change if changed else None)
        gone = [x for x in replaced if isinstance(claim, str)
                and self._auto_retire(x["retire"], {"page": nid, "id": claim}, x["why"])]
        out = {"changed": changed, **({"claim": claim} if claim else {}),
               **({"supersedes": [s["claim"] for s in sup]} if sup else {}),
               **({"replaced": [self._line_ref(x["retire"]) for x in gone]} if gone else {}),
               "summary": f"{op} {target!r} on {kind} {nid}",
               "next": (SUPERSEDES_NEXT if sup else "")
               + ("Keepfold retired the line(s) in `replaced` (they said nearly the same or this "
                  "one replaces them; `keepfold undo` brings them back); say so in one line. "
                  if gone else "") + self._next(MORE_NEXT) + hint}
        return _update_of(None if gone else shown, self._held_command(out, f"{target}\n{text}"))

    def _held_command(self, out: dict[str, Any], text: str) -> dict[str, Any]:
        """G5b-6: `out` with HELD_NEXT first in its `next` when the claim line it names is held
        for a command to run (review.unsafe_lines); text with no command costs nothing."""
        ids = out.get("claim")
        ids = set(ids) if isinstance(ids, list) else {ids} if ids else set()
        if ids and claims.command(text) and ids & {
                x["id"] for x in review.unsafe_lines(self.vault, self.log.iter_events())
                if x.get("command")}:
            out["next"] = HELD_NEXT + out["next"]
        return out

    def _line_ref(self, r: dict) -> dict:
        """A claim row as a result names it: {id, page, text} with the text wrapped, or no text
        for a page outside this client's scopes (plan v9 item 8)."""
        ref, scopes = f"me/{r['page']}", self._scopes()
        ok = scopes is None or (r["page"] not in access.SECTIONED
                                and access.scope_page(scopes, r["page"]))
        return {"id": r["id"], "page": ref, **({"text": _wrap(ref, r["text"])} if ok else {})}

    def _settle_new(self, page: str, target: str, text: str, day: str, confirmed: bool,
                    clicked: bool, update: dict | None, sup: list | None) -> dict:
        """D1/D2/D3 at write time: `review.settle` over the live lines plus the line about to be
        written to me/<page> (with its `update_of` guess or `supersedes` as a recorded pair).
        {lost: the result to return, nothing written} when the new line is the one to retire
        (a near-copy of a line that stays, or a guess that clashes with the user's own line);
        else {retire: [{retire, why}] the live lines it replaces}. A private page: nothing."""
        if page in review._private(self.vault):
            return {"retire": []}
        events = list(self.log.iter_events())
        rows = review.live_rows(self.vault, events, day)
        new = {"page": page, "id": "c-new00", "section": target, "line_no": 1 << 30,
               "text": " ".join(text.split()).removeprefix("- "), "day": day,
               "tier": self._tier(confirmed, clicked)["tier"]}
        pairs = {**({"update_of": {"page": update["page"], "claim": update["id"]}}
                    if update else {}), **({"supersedes": sup} if sup else {})}
        if pairs:
            events.append({"kind": "change", "id": "new", "payload": {"change": {
                "id": page, "claim": new["id"], **pairs}}})
        plan = review.settle([*rows, new], events, review.known_texts(self.vault),
                             self._tidy_done())["retire"]
        lost = next((x for x in plan if x["retire"] is new), None)
        if lost is None:
            return {"retire": [x for x in plan if x["keep"] is new]}
        keep = self._line_ref(lost["keep"])
        # a guessed `update` pair agrees (an opposite one is settled as `opposite`): a near-copy
        if lost["why"] in ("opposite", "supersedes"):
            return {"lost": {
                "changed": False, "clashes_with": keep,
                "summary": f"not saved: it clashes with the user's own line ^{keep['id']} on "
                           f"{keep['page']}",
                "next": "Not saved: it clashes with a line the user gave. Ask them in one "
                        "question whether that changed; on yes, keepfold_patch it again with "
                        f"user_confirmed=true and supersedes=['{keep['id']}']."}}
        if (s := self.sessions.current) is not None:  # R13b: the kept line came back
            review.reinforce(self.log, s.id, lost["keep"]["page"], lost["keep"]["id"],
                             self.sessions.client)
        return {"lost": {"changed": False, "duplicate_of": keep,
                         "summary": f"not saved: {keep['page']} already says this "
                                    f"(^{keep['id']})",
                         "next": "Already saved; go on." + self._next(MORE_NEXT)}}

    def _kept_by_click(self, page: str, target: str, day: str) -> tuple[dict, str | None] | None:
        """R13a: Keep first or second on a conflict card (the Review click's `card` stamp)
        retires the other line: (the card's line that stays, the since it gets:
        review.since_for, a guessed update's right line being the newer), or None when the
        retired line is on no shown card or on several."""
        events = list(self.log.iter_events())
        cid = target.strip().lstrip("^")
        hits = [(c, c["right"] if (c["left"]["page"], c["left"]["id"]) == (page, cid)
                 else c["left"])
                for c in review.conflicts(review.live_rows(self.vault, events, day), events,
                                          review.known_texts(self.vault))
                if (page, cid) in ((c["left"]["page"], c["left"]["id"]),
                                   (c["right"]["page"], c["right"]["id"]))]
        if len(hits) != 1:
            return None
        c, keep = hits[0]
        old = c["left"] if keep is c["right"] else c["right"]
        newer = (keep is c["right"]) if c["why"] == "maybe_update" else None
        return keep, review.since_for(old, keep, day, newer)

    def _supersedes(self, ids, kind: str, nid: str, op: str, text: str) -> list[dict]:
        """RR-4: keepfold_patch's `supersedes` -> [{page, claim}], each id one live line of a
        non-private claim page; refused unless one line is added to a claim page."""
        if (not isinstance(ids, list) or not 1 <= len(ids) <= SUPERSEDES_MAX
                or not all(isinstance(x, str) for x in ids)):
            raise invalid(f"supersedes takes 1 to {SUPERSEDES_MAX} claim ids (^c-…).")
        if (op != "append_to_section" or kind != "me" or nid not in claims.PAGES
                or "\n" in text.strip()):
            raise invalid("supersedes works only when adding one line to me/"
                          + ", me/".join(claims.PAGES) + ".")
        day, hidden = local_day(self._now()), review._private(self.vault)
        live = [r for r in claims.rows(self.vault)
                if claims.is_live(r, day) and r["page"] not in hidden]
        out = []
        for cid in dict.fromkeys(x.strip().removeprefix("^") for x in ids):
            hit = [r for r in live if r["id"] == cid]
            if len(hit) != 1:
                raise invalid(f"supersedes: {cid!r} is not a live claim id; read the me/* page "
                              "for the line's ^c-… id.")
            out.append({"page": hit[0]["page"], "claim": cid})
        return out

    def _proposed_supersedes(self, page: str, text: str) -> list[dict] | None:
        """RR-4: the `supersedes` of the latest proposal of `text` for me/<page>, its lines
        still live; None when it named none. Approve writes the pair the proposal named."""
        h, sup = review.text_hash(text), None
        voided = {e.get("ref") for e in self.log.iter_events() if e.get("kind") == "void"}
        for e in self.log.iter_events():
            pl = e.get("payload") if e.get("kind") == "change" else None
            c = pl.get("change") if isinstance(pl, dict) else None
            if (isinstance(c, dict) and c.get("tool") == "propose" and c.get("id") == page
                    and c.get("hash") == h and e.get("id") not in voided):
                sup = c.get("supersedes") if isinstance(c.get("supersedes"), list) else None
        live = {(r["page"], r["id"]) for r in claims.rows(self.vault)
                if claims.is_live(r, local_day(self._now()))}
        sup = [s for s in sup or () if isinstance(s, dict)
               and (s.get("page"), s.get("claim")) in live]
        return sup or None

    def _tier(self, confirmed: bool, clicked: bool = False) -> dict[str, str]:
        """v5 F3: the server-computed authorship tier of a claim this session writes; with a yes,
        idea 11's `confirm`: `clicked` in Keepfold's own dialog or the Keepfold app, else `relayed`
        by the model. An app click has no model in between, so no exposure either (plan 5a)."""
        app = self.app_clicks
        out = {"tier": "exposed" if self.session.exposed and not app
               else "confirmed" if confirmed else "agent"}
        if confirmed:
            out["confirm"] = ("clicked" if clicked or app else "tagged" if self.tagged
                              else "relayed")
        return out

    def _unpropose(self, text: str) -> None:
        """A confirmed line no longer waits under `## About me (unconfirmed)` in inbox.md."""
        with self.vault.lock():  # reentrant
            try:
                inbox, _ = claims.unpropose(self._raw("inbox.md"), text)
            except VaultError:
                return  # it was never proposed
            self.vault.write_text(self.vault.root / "inbox.md", inbox)

    def _dismiss(self, page: str, section: str, text: str, waiting: bool = True
                 ) -> dict[str, Any]:
        """Plan 5a, the app's Dismiss on a page with no `## Not me` (the contract, the
        portrait): the waiting line leaves inbox.md and a `dismiss` change (its text's hash,
        never the text) keeps it from being proposed again (`review.dismissed`). Not `waiting`
        (plan 5b, a repeated correction, page `corrections`): only the change."""
        if not self.app_clicks:
            raise invalid("Dismiss is the Keepfold app's own action.")
        if waiting:
            with self.vault.lock():
                inbox, _ = claims.unpropose(self._raw("inbox.md"), f"{section}: {text}")
                self.vault.write_text(self.vault.root / "inbox.md", inbox)
        self.sessions.note("patched", change={
            "tool": "dismiss", "id": page, "kind": "me", "op": "dismiss", "target": section,
            "hash": review.text_hash(text), "confirm": "clicked"})
        return {"changed": True, "summary": f"dismissed a proposed line for me {page}"}

    def _remove_outside(self, page: str, key: str) -> dict[str, Any]:
        """R5a, the app's Remove on a line changed outside Keepfold that is no claim line (a
        contract line, a heading or other line on me/core or me/setup; a claim line is retired
        instead): the first line of me/<page> with card key `key` is deleted and a `remove`
        change recorded, so `keepfold undo` puts it back."""
        if not self.app_clicks:
            raise invalid("Remove is the Keepfold app's own action.")
        if page not in review.GUARDED:
            raise invalid(f"me/{page} is not a guarded page.")
        gone = []

        def drop(text: str) -> str:
            lines = text.splitlines(keepends=True)
            at = next((i for i, ln in enumerate(lines) if ln.strip()
                       and review.line_key(page, ln) == key), None)
            gone[:] = [] if at is None else [at]
            return text if at is None else "".join(lines[:at] + lines[at + 1:])

        self.vault.update_text(self.vault.path_for("me", page), drop)
        if not gone:
            raise invalid("That line is no longer on the page.")
        self.sessions.note("patched", change={
            "tool": "remove", "id": page, "kind": "me", "op": "remove", "target": key,
            "confirm": "clicked"})
        return {"changed": True,
                "summary": f"removed a line changed outside Keepfold from me {page}"}

    def _decide(self, title: str, fields: dict, hash: str) -> dict[str, Any]:
        """RR-9, the app's Approve on a decision found in a commit subject: a decision page (as
        keepfold_add writes it) and a `decide` change holding the proposal's key, so the Review
        does not offer it again (more_sources.decision_proposals)."""
        if not self.app_clicks:
            raise invalid("Approve is the Keepfold app's own action.")
        out = self.keepfold_add("decision", title, fields=fields, force=True)
        self.sessions.note("patched", change={
            "tool": "decide", "id": "decisions", "kind": "decision", "op": "decide",
            "target": out["id"], "hash": hash, "confirm": "clicked"})
        return out

    def _keep_both(self, page: str, pair: str) -> dict[str, Any]:
        """Plan 5b, the app's Keep both on a conflict pair (`review.pair_key`): a `keep_both`
        change, so the Review stops showing the pair; neither line changes."""
        if not self.app_clicks:
            raise invalid("Keep both is the Keepfold app's own action.")
        self.sessions.note("patched", change={
            "tool": "keep_both", "id": page, "kind": "me", "op": "keep_both", "pair": pair,
            "confirm": "clicked"})
        return {"changed": True, "summary": "kept both lines"}

    def _resume(self, producer: str) -> dict[str, Any]:
        """RR-10, the app's Resume on a paused Review producer (review.ledger): a `resume`
        change, after which its cards are offered again and its count starts over."""
        if not self.app_clicks:
            raise invalid("Resume is the Keepfold app's own action.")
        self.sessions.note("patched", change={
            "tool": "resume", "id": "review", "kind": "review", "op": "resume",
            "producer": producer, "confirm": "clicked"})
        return {"changed": True, "summary": f"resumed {producer} in Review"}

    def _claim_op(self, nid: str, op: str, target: str, text: str,
                  confirmed: bool, until: str | None = None,
                  clicked: bool = False) -> dict[str, Any]:
        """v5 F2: revise or retire a live claim (`target` its id), or reject a proposed line
        (`target` its text) into `## Not me`. `until` (H2) dates the `## Past` line."""
        if not target.strip():
            raise invalid(f"{op} needs target: " + ("the proposed line's text." if op == "reject"
                                                 else "the claim id (^c-…)."))
        day = local_day(self._now())
        if until is not None and not (isinstance(until, str) and len(until) == 10
                                      and loops.parse_day(until) and until <= day):
            raise invalid("until must be YYYY-MM-DD, not after today.")
        path = self.vault.path_for("me", nid)
        line = None
        if op == "revise":
            line = " ".join(text.split()).removeprefix("- ")
            if not line:
                raise invalid("revise needs text: the new wording.")
            if nid in CONFIRM_PAGES:
                if not confirmed:
                    raise invalid(f"revise on me/{nid} needs user_confirmed=true; ask the user "
                               "first.")
                line += f" (confirmed {day})"
            scan_secrets(line)
        claim, kept = None, None
        with self.vault.lock():
            if op == "reject":
                inbox, said = claims.unpropose(self._raw("inbox.md"), target)
                self.vault._mutate(path, lambda t: claims.add_not_me(t, said, day))
                self.vault.write_text(self.vault.root / "inbox.md", inbox)
            else:
                if op == "retire" and self.sessions.stamp.get("card") == "conflict":
                    kept = self._kept_by_click(nid, target, day)  # R13a: before it goes
                done: dict[str, str] = {}

                def edit(t: str) -> str:
                    done["line"] = line
                    old = next((r for r in claims.parse(nid, t) if line is not None
                                and r["id"] == target.strip().lstrip("^")
                                and r["status"] == "live"), None)
                    if (old and not claims.since(line) and "(until" not in line
                            and (s := review.since_for(old, {"text": line}, day, True))):
                        done["line"] = f"{line} (since {s})"  # R13a: what it replaces
                    new, done["id"] = claims.move_to_past(t, nid, target, day, done["line"],
                                                          until)
                    return new

                self.vault._mutate(path, edit)
                claim, line = done["id"], done["line"]
        change = {"tool": "patch", "id": nid, "kind": "me", "op": op, "target": target}
        if kept:
            change.update(self._stamp_since(*kept))
        if line is not None and nid in review.GUARDED:  # R5a: the revised line Keepfold wrote
            change["lines"] = [review.line_hash(f"- {claims._ANY_ID.sub('', line)}")]
        if claim:
            change.update(claim=claim, **self._tier(confirmed, clicked))
        if self.app_clicks:
            change["confirm"] = "clicked"
        hint = self.sessions.note("patched", change=change)
        return {"changed": True, **({"claim": claim} if claim else {}),
                "summary": f"{op} {target!r} on me {nid}", "next": self._next(MORE_NEXT) + hint}

    def _portrait_section(self, month: str) -> None:
        """me/portrait.md gets its `## YYYY-MM` heading on first use (no other page does this)."""
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise invalid("A portrait section is named YYYY-MM, e.g. 2026-11.")
        def add(t: str) -> str:  # B13: an owner's edit made meanwhile is reread, not lost
            return t if f"## {month}" in t.splitlines() else t.rstrip("\n") + f"\n\n## {month}\n"

        self.vault.update_text(self.vault.root / "me" / "portrait.md", add, "# Portrait\n")

    def _voice_section(self, audience: str) -> None:
        """V2: me/voice.md gets its `## <audience>` heading on first use, before `## Past`."""
        if audience != "Everyone":
            voice.check_audience(audience)
        self._ensure_section("voice", audience, "# How I write\n")

    def _harvest_patch(self, kind: str, nid: str, op: str, target: str, text: str,
                       claim_page: bool, confirmed: bool) -> None:
        """The patch guards while a harvest has served chats (harvest verdict item 4):
        imported chat pages stay read-only, people pages are refused, each proposed about-me
        line cites a served [[chat-…]] (a yes on the card is the user's own word, so needs
        none), and a topic hub gets a missing section heading."""
        from keepfold import harvest, semantic, topics

        if kind == "person":  # a bare id; the gate refuses people/<id> before any session
            raise VaultError("harvesting", "keepfold_patch on a people page is refused while "
                             "harvesting imported chats.")

        n = self.index._notes.get(nid) if kind in topics.PAGE_KINDS else None
        if n is not None and semantic.is_chat(n):
            raise invalid("Imported chat pages are read-only while harvesting; link to the "
                          "chat from a note or a hub line instead.")
        if op != "append_to_section":
            return
        if claim_page and not confirmed and not all(set(link_targets(ln)) & self.sessions.served
                                  for ln in text.splitlines() if ln.strip()):
            raise invalid("Cite the chat: end each about-me line with (from [[chat-…]]), "
                          "naming a chat this harvest served.")
        if n is not None and topics.is_hub(n):
            if target.strip().lstrip("#").strip() == harvest.OLD_KNOW:
                raise invalid("Harvest lines go under What I explored: the user's questions "
                              "show what they explored, not what they know.")
            self._ensure_section(nid, target, kind="note", before=HUB_GENERATED)

    def _ensure_section(self, page: str, heading: str, new: str = "", kind: str = "me",
                        before=("## Past", "## Not me")) -> None:
        """<kind>/<page> (me/ by default) gets its `## <heading>` on first use, before the
        first `before` heading, else at the end."""
        if not heading.strip() or "\n" in heading or heading.startswith("#"):
            raise invalid("target must be a one-line section name.")
        heading = heading.strip()

        def add(text: str) -> str:  # B13: reruns on the reread page if the owner edited it
            nl = "\r\n" if "\r\n" in text else "\n"
            lines = text.replace("\r\n", "\n").rstrip("\n").split("\n")
            if f"## {heading}" in lines:
                return text
            at = next((i for i, ln in enumerate(lines) if ln in before), None)
            if at is None:
                lines += ["", f"## {heading}"]
            else:
                lines[at:at] = [f"## {heading}", ""]
            return nl.join(lines) + nl

        self.vault.update_text(self.vault.path_for(kind, page), add, new)
