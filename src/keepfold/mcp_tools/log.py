"""keepfold_log and its kinds (touch, taught, journal, chat turns, assessments, problems,
void), plus the //tags a user turn carries. A mixin of `keepfold.mcp_server.Keepfold`."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Literal

from keepfold import (
    chats,
    claims,
    dates,
    delta,
    learning_eval,
    levels,
    loops,
    planner,
    prime,
    render,
    tags,
    voice,
    xray,
)
from keepfold.catalog import KINDS as CATALOG_KINDS
from keepfold.catalog import inbox_add, page_slug, slugify
from keepfold.events import check_assessment_result, local_day, log_assessment, void_event
from keepfold.mcp_server import (
    ASSESSMENT_KINDS,
    MORE_NEXT,
    TAGS_NEXT,
    TOUCH_MAX,
    VERDICTS,
    _pin_day,
)
from keepfold.vault import ENUMS, ID_RE, VaultError, invalid

if TYPE_CHECKING:  # build_server puts the SDK's names in this module's globals
    from mcp.server.mcpserver import Context


class LogTools:
    """keepfold_log, `_tags` and `_tag`."""

    def keepfold_log(self, kind: Literal["assessment_result", "self_report", "taught",
                                       "problem_status", "capture", "journal",
                                       "chat_saved", "resurfaced", "need", "need_skip",
                                       "chat_learned", "reconfirm", "voice_sample", "touch",
                                       "harvested", "void", "spot_check", "chat_turn"],
                   payload: dict[str, Any], key: str | None = None,
                   ctx: Context | None = None) -> dict[str, Any]:
        if kind == "access":  # V5: written by the server only
            raise invalid("access events are written by the server; they cannot be logged.")
        if not isinstance(payload, dict):
            raise invalid("payload must be an object.")
        p = payload
        cid, extra = None, {}
        if kind == "void":  # cancels one earlier event: its own result
            return self._void(p)
        if kind == "journal":  # a note, not evidence: no event, never enters levels or planner
            return self._log_journal(p)
        if kind in ("need", "need_skip"):  # R3-9: an old client's call; old events still load
            raise invalid(f"keepfold_log kind '{kind}' was removed (21 Nov 2026); nothing was "
                          "written. Say the missing fact in one line instead.")
        if kind == "touch":  # a repeat in this session writes nothing: its own result
            return self._log_touch(p, key)
        if kind == "harvested":  # a retry of a logged key writes nothing: its own result
            return self._log_harvested(p, key)
        if kind == "chat_turn":  # the live chat log, outside the vault: no event
            return self._log_chat_turn(p)
        if kind == "assessment_result":
            line = self._log_assessment(p, key)
        elif kind == "self_report":
            if p.get("verdict") not in VERDICTS:
                raise invalid("self_report needs verdict: correct, partial or wrong.")
            h = p.get("hints")
            if h is not None and (isinstance(h, bool) or not isinstance(h, int) or not 0 <= h <= 3):
                raise invalid("hints must be a whole number from 0 to 3.")
            obj, made = self._log_concept(p, "self_report")  # after the checks: it may create
            pl = {"kind": "self_report", "concept": obj.split(":", 1)[1],
                  "verdict": p["verdict"]}
            for name in ("hints", "note"):
                if p.get(name) is not None:
                    pl[name] = p[name]
            cid = obj.split(":", 1)[1] if obj.startswith("concept:") else None
            sp = None
            if cid:  # server-stamped like ask and grade, so eval learning can pair it
                evs = self._events()  # also refreshes self._pc
                sp = levels.predicted_p(evs, cid, self._now(), self._pc)
            self._append("log", {"object": obj, "payload": pl}, key, predicted_p=sp,
                         source="self")
            line = f"{render.esc(self._title(cid) if cid else p['concept'])}: {p['verdict']}"
        elif kind == "taught":
            summary = str(p.get("summary") or "")
            if len(summary) > 300:
                raise invalid("summary must be 300 characters or fewer.")
            obj, made = self._log_concept(p, "taught")  # after the check: it may create
            pl = {"kind": "taught", **({"summary": summary} if summary else {})}
            self._append("taught", {"object": obj, "payload": pl}, key)
            cid = obj.split(":", 1)[1] if obj.startswith("concept:") else None
            if cid:
                self.taught[cid] = self._title(cid)
            name = self._title(cid) if cid else p["concept"]
            line = f"Taught, not yet checked: {render.esc(name)}"
        elif kind == "capture":
            line = self._log_capture(p)
        elif kind == "chat_saved":
            line = self._log_chat_saved(p, key)
        elif kind == "resurfaced":
            ids = p.get("ids")
            if not isinstance(ids, list) or not 1 <= len(ids) <= 20 or not all(
                    isinstance(x, str) and ID_RE.fullmatch(x) for x in ids):
                raise invalid("resurfaced needs payload.ids: 1 to 20 page ids.")
            self._append("log", {"payload": {"kind": "resurfaced", "ids": ids}}, key)
            line = f"Recorded {len(ids)} resurfaced"
        elif kind == "chat_learned":
            line = self._log_chat_learned(p, key)
        elif kind == "reconfirm":
            line = self._log_reconfirm(p, key)
        elif kind == "spot_check":
            line = self._log_spot_check(p, key)
        elif kind == "voice_sample":  # V1: voice/, not the event log
            extra["id"] = voice.add(self.vault, voice.SAMPLES, [voice.fields(p)], self._now())[0]
            line = "Voice sample saved"
        else:
            line = self._log_problem(p, key)
        if kind in ("self_report", "taught") and cid is None:
            line += " (added to inbox.md, not linked to a concept)"
        elif kind in ("self_report", "taught") and made:
            e = self.catalog.entries[cid]
            where = " › ".join(self._title(i) for i in (e.domain, e.parent) if i)
            line = f"Created {render.esc(e.title)} under {render.esc(where)}. " + line
            extra["id"] = cid
        hint = self.sessions.note("logged", cid, log_kind=kind)
        return {"recorded": 1, **extra, "display_line": line,
                "next": self._next(MORE_NEXT) + hint}

    def _log_touch(self, p: dict, key: str | None) -> dict[str, Any]:
        """The user's message was about these pages (usually keepfold_prime's topic hubs). Each
        page is logged at most once per session, so a chat never counts a topic twice; no
        object, so levels and the planner ignore it."""
        ids = p.get("ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= TOUCH_MAX or not all(
                isinstance(x, str) and ID_RE.fullmatch(x) for x in ids):
            raise invalid(f"touch needs payload.ids: 1 to {TOUCH_MAX} page ids.")
        self._refresh_index()
        if unknown := [x for x in ids if x not in self.index._notes]:
            raise VaultError("unknown_id", f"No page {', '.join(unknown)}; use ids that "
                             "keepfold_prime or keepfold_recall returned.")
        new = [x for x in dict.fromkeys(ids) if x not in self.touched]
        if not new:
            return {"recorded": 0, "display_line": "Already recorded in this chat",
                    "next": self._next(MORE_NEXT)}
        self._append("log", {"payload": {"kind": "touch", "ids": new}}, key)
        self.touched.update(new)
        hint = self.sessions.note("logged", log_kind="touch")
        return {"recorded": 1, "display_line": f"Topic touch: {', '.join(new)}",
                "next": self._next(MORE_NEXT) + hint}

    def _log_reconfirm(self, p: dict, key: str | None) -> str:
        """v6 U3: the user's answer to "still yours?" about a line saved unasked. A yes in a
        reflect the user started, in a clean session, promotes an `agent` or `exposed` line to
        `confirmed` (`claims.tiers`); `promote` is server-stamped, never read from input. A no
        changes nothing (the user retires the line with keepfold_patch)."""
        if p.get("verdict") not in ("yes", "no"):
            raise invalid("reconfirm needs payload.verdict: yes or no.")
        cid = claims.resolve(self.vault, p.get("claim"), local_day(self._now()))
        promote = (p["verdict"] == "yes" and self.session.mode == "reflect"
                   and not self.session.exposed)
        self._append("log", {"payload": {"kind": "reconfirm", "claim": cid,
                                         "verdict": p["verdict"],
                                         **({"promote": True} if promote else {}),
                                         # plan 5a: Still true in the Keepfold app
                                         **({"confirm": "clicked"} if self.app_clicks
                                            else {})}}, key)
        return f"Still true: {p['verdict']} (^{cid})"

    def _log_spot_check(self, p: dict, key: str | None) -> str:
        """v6 idea 10: the user's verdict on a model-graded answer from the reflect pack. A
        flip voids the grade and regrades it as a self_report (source self, no predicted_p, so
        eval learning does not pair it); `was` is copied from the grade event, never read from
        input. The void, the regrade and the spot_check row are written under one vault lock."""
        eid, verdict = p.get("event_id"), p.get("verdict")
        if not isinstance(eid, str) or verdict not in VERDICTS:
            raise invalid("spot_check needs payload {event_id, verdict}: a spot_check row's "
                          "event_id and the user's verdict (correct, partial or wrong).")
        with self.vault.lock():  # reentrant: each append below takes it again
            if key is not None and self.log.has_key(key):  # a retry after a lost reply
                return "Spot-check: already recorded"
            hide = planner.hidden(self.vault, self.catalog)
            if eid not in {r["event_id"]
                           for r in learning_eval.spot_checks(self._events(), None, hide)}:
                raise invalid(f"Event '{eid}' is not a model-graded answer left to spot-check; "
                              "take event_id from keepfold_reflect's spot_check.")
            target = next(e for e in self.log.iter_events() if e["id"] == eid)
            was = target.get("verdict")
            if verdict != was:
                self._void({"event_id": eid, "reason": f"spot-check: user flipped {was} to "
                                                        f"{verdict}"})
                pl = {"kind": "self_report", "concept": target["object"].split(":", 1)[1],
                      "verdict": verdict}
                if isinstance(target.get("hints"), int):
                    pl["hints"] = target["hints"]
                # B4: server-written, never tool input; levels fold it on the grade's own day
                pl["note"], pl["regrade_of"] = "spot-check flip", eid
                # review r4 L4, G1: keep the item's splits and links, so eval and levels see the ask
                ask = next((e for e in self.log.iter_events() if e["kind"] == "ask"
                            and e["id"] == target.get("ref")), {}) if target.get("ref") else {}
                same = {k: v for k in ("item_type", "confidence", "question", "problem",
                                       "also", "targets_mistakes", "ref")
                        if (v := target.get(k, ask.get(k))) is not None}
                if t := list(dict.fromkeys([*(ask.get("targets_mistakes") or []),
                                            *(target.get("targets_mistakes") or [])])):
                    same["targets_mistakes"] = t  # G1: the grade's and its ask's, as the fold
                self._append("log", {"object": target["object"], **same, "payload": pl},
                             source="self")
            self._append("log", {"payload": {"kind": "spot_check", "event": eid,
                                             "verdict": verdict, "was": was}}, key)
        return f"Spot-check: {'kept' if verdict == was else f'flipped {was} to {verdict}'}"

    def _log_capture(self, p: dict) -> str:
        """An ingest: one log.md line; no event (phase4-wiki.md §3)."""
        pages = p.get("pages")
        if not isinstance(pages, list) or len(pages) > 50 or \
                not all(isinstance(x, str) and ID_RE.fullmatch(x) for x in pages):
            raise invalid("capture needs payload.pages: a list of up to 50 page ids.")
        rid = p.get("resource")
        if not isinstance(rid, str) or not ID_RE.fullmatch(rid):
            raise invalid("capture needs payload.resource: the id of the resource page the "
                          "pages came from.")
        title = str(self.vault.read("resource", rid).fields.get("title") or rid)
        self.vault.append_log(local_day(self._now()), "capture",
                              f"{render.plain(title, 80)} ({len(pages)} pages)")
        return f"Captured {render.esc(title)}: {len(pages)} pages"

    @staticmethod
    def _chat_ref(p: dict, kind: str) -> tuple[str, str]:
        """The checked (source, chat_id) of a chat ledger payload."""
        source, chat_id = p.get("source"), p.get("chat_id")
        if source not in chats.SOURCES:
            raise invalid(f"{kind} needs payload.source: one of {', '.join(chats.SOURCES)}.")
        if not isinstance(chat_id, str) or not chat_id.strip() or len(chat_id) > 300:
            raise invalid(f"{kind} needs payload.chat_id (the chat's id, up to 300 chars).")
        return source, chat_id

    def _log_chat_saved(self, p: dict, key: str | None) -> str:
        """The dedupe ledger for /keepfold save and history: an event, a log.md line and a
        journal line. Levels ignore it (no concept)."""
        source, chat_id = self._chat_ref(p, "chat_saved")
        title, pages = p.get("title"), p.get("pages", [])
        if not isinstance(title, str) or not title.strip():
            raise invalid("chat_saved needs payload.title.")
        if not isinstance(pages, list) or len(pages) > 50 or not all(
                isinstance(x, str) and ID_RE.fullmatch(x) for x in pages):
            raise invalid("chat_saved payload.pages must be a list of up to 50 page ids.")
        saved = chats.saved_ids(self.log.iter_events())
        if p.get("again") is not True and (source, chat_id) in saved:
            raise VaultError("already_saved", f"Chat {chat_id} was already saved.")
        self._append("log", {"payload": {"kind": "chat_saved", "source": source,
                                         "chat_id": chat_id, "title": title, "pages": pages}},
                     key)
        now = self._now()
        day, name = local_day(now), render.plain(title, 80)
        self.vault.append_log(day, "chat", f"{name} ({len(pages)} pages)")
        self.vault.append_journal(day, f"{now.astimezone():%H:%M}",
                                  f"Saved chat: {name} ({len(pages)} pages)")
        return f"Saved chat {render.esc(title)}: {len(pages)} pages"

    def _log_harvested(self, p: dict, key: str | None) -> dict[str, Any]:
        """The harvest ledger: one event per bundle (chats already logged are dropped) and a
        log.md line, no journal line (the month pages exist). Outcome `skipped` takes a hub or
        area id instead and drops its subtree from the queue; a void re-queues either."""
        from keepfold import harvest, semantic, topics

        if key is not None and self.log.has_key(key):  # a retry after a crash or lost reply
            return {"recorded": 0, "display_line": "Already recorded",
                    "next": self._next(MORE_NEXT)}
        outcome = p.get("outcome", "harvested")
        if outcome not in harvest.OUTCOMES:
            raise invalid("harvested payload.outcome must be harvested, empty or skipped.")
        self._refresh_index()
        notes = self.index._notes
        if outcome == "skipped":
            hub = p.get("hub")
            private = isinstance(hub, str) and harvest.private_hub(self.vault, hub, notes)
            if not private and not (isinstance(hub, str) and hub in notes
                                    and topics.is_hub(notes[hub])):
                raise invalid("A skipped harvest needs payload.hub: a topic hub or area id.")
            pl = {"kind": "harvested", "outcome": outcome, "hub": hub}
            what = "skipped a private hub" if private else \
                f"skipped {render.plain(notes[hub]['title'], 80)}"
        else:
            ids, dupes = p.get("chats"), p.get("dupes") or {}
            pages, found = p.get("pages", []), p.get("found", 0)
            if isinstance(found, list):  # dogfood r4: the lines themselves; store their count
                found = len(found)
            if isinstance(pages, list):  # dogfood r4: "me/personality" means "personality"
                pages = [x.removeprefix("me/") if isinstance(x, str) else x for x in pages]
            if not isinstance(ids, list) or not 1 <= len(ids) <= harvest.CHATS_MAX or not all(
                    isinstance(x, str) and x in notes and semantic.is_chat(notes[x])
                    for x in ids):
                raise invalid(f"harvested needs payload.chats: 1 to {harvest.CHATS_MAX} "
                              "imported chat page ids, as the bundle served them.")
            if not isinstance(dupes, dict) or not all(
                    isinstance(c, str) and c != k and c in ids and k in ids
                    and self._chat_key(notes[c], c) == self._chat_key(notes[k], k)
                    for c, k in dupes.items()):
                raise invalid("harvested payload.dupes maps a copy to its keeper: both in "
                              "chats, from the same day and app.")
            if not isinstance(pages, list) or len(pages) > harvest.PAGES_MAX or not all(
                    isinstance(x, str) and ID_RE.fullmatch(x) for x in pages):
                raise invalid(f"harvested payload.pages: up to {harvest.PAGES_MAX} bare page "
                              "ids (personality, not me/personality).")
            if type(found) is not int or not 0 <= found <= harvest.FOUND_MAX:
                raise invalid(f"harvested payload.found: the number of about-me lines "
                              f"proposed, 0-{harvest.FOUND_MAX}.")
            done = harvest.fold(self.log.iter_events())[0]
            new = [x for x in dict.fromkeys(ids) if x not in done]
            if not new:
                raise VaultError("already_harvested", "Every chat in payload.chats is "
                                 "harvested already.")
            pl = {"kind": "harvested", "outcome": outcome, "chats": new,
                  "dupes": {c: k for c, k in dupes.items() if c in new}, "pages": pages,
                  "found": found}
            what = f"{len(new)} chats → {len(pages)} pages"
        self._append("log", {"payload": pl}, key)
        self.vault.append_log(local_day(self._now()), "harvest", what)
        hint = self.sessions.note("logged", log_kind="harvested")
        return {"recorded": 1, "display_line": f"Harvest: {render.esc(what)}",
                "next": self._next(MORE_NEXT) + hint}

    @staticmethod
    def _chat_key(n: dict, id: str) -> tuple[str, str]:
        from keepfold import topics

        return topics.page_date(id, n["fields"], n["body"]), topics.page_source(n["body"])

    def _log_chat_learned(self, p: dict, key: str | None) -> str:
        """G3 ledger: a past chat was read for what it says about the user."""
        source, chat_id = self._chat_ref(p, "chat_learned")
        found = p.get("found")
        if type(found) is not int or not 0 <= found <= 100:
            raise invalid("chat_learned needs payload.found: the number of lines proposed, 0-100.")
        self._append("log", {"payload": {"kind": "chat_learned", "source": source,
                                         "chat_id": chat_id, "found": found}}, key)
        return f"Learned from chat {render.esc(chat_id)}: {found} proposed"

    def _log_chat_turn(self, p: dict) -> dict[str, Any]:
        user, gist = p.get("user"), p.get("gist") or ""
        if not isinstance(user, str) or not user.strip():
            raise invalid("chat_turn needs payload.user: the user's new message, verbatim.")
        if not isinstance(gist, str):
            raise invalid("chat_turn payload.gist must be text: your last answer in one line.")
        cid = self._chat_log(user, gist, p.get("chat"))
        tagged = {"tags": t} if (t := self._tags(user)) else {}
        more = TAGS_NEXT if tagged else ""
        if cid is None:
            return {"recorded": 0, **tagged, "display_line": "Not logged here: Keepfold saves "
                    "this app's chats from its own transcripts (or this server is not the "
                    "owner's).", "next": "Continue; no chat_turn calls are needed in this app."
                    + more}
        ctx = self._turn_delta(cid, user)
        nudge = delta.nudge(self._chat_turns(cid))
        return {"recorded": 1, "chat": cid, **tagged, **({"context": ctx} if ctx else {}),
                "display_line": "Chat logged",
                "next": ("Answer with `context` in mind; log" if ctx else "Continue; log")
                + f" the next turn the same way (chat '{cid}')." + (f" {nudge}" if nudge else "")
                + more}

    def _turn_delta(self, cid: str, user: str) -> str:
        """G3: the Profile and Preferences lines a turn brings that its chat was not served
        (keepfold.delta, from prime.core with this client's scopes and withheld pages), recorded
        in the X-ray; "" for a greeting, an ack, a slash command, none or an error."""
        if delta.trivial(user):
            return ""
        try:
            parts, _ = prime.core(self.vault, user, list(self.log.iter_events()), self._now(),
                                  self._withheld(), {}, self._allow(), self.sessions.client)
            seen = self._served(cid)
            text, ids = delta.lines(self.vault, user, parts, seen)
        except Exception:  # noqa: BLE001  (the turn is logged either way)
            logging.getLogger(__name__).warning("chat_turn delta skipped", exc_info=True)
            return ""
        seen.update(ids)
        if ids:
            xray.record(self.vault, "chat_turn", self.sessions.client, {"preferences": ids}, {},
                        len(text))
        return text

    @staticmethod
    def _chat_turns(cid: str) -> int:
        """G5: the user turns in chat `cid`'s live log; 0 when it cannot be read."""
        try:
            rows = (chats.export_root() / chats.LIVE / f"{cid}.jsonl").read_text(
                "utf-8").splitlines()
        except OSError:
            return 0
        n = 0
        for x in rows:
            try:
                n += json.loads(x).get("type") == "user"
            except (ValueError, AttributeError):
                continue
        return n

    def _tags(self, user: str, hooked: bool = False) -> list[str]:
        """R3-5: act on the //tags in a user turn (keepfold.tags); the display lines. `hooked`
        (keepfold_prime related_only): the prime hook saved them moments ago, maybe before the
        04:00 day change, so an hour ago's day key counts as saved too. D-15: the text came
        from the model, so its tags only propose (tags.SAID["mcp"])."""
        found = tags.find(user)
        now = self._now()
        if hooked:
            before = local_day(now - timedelta(hours=1))
            found = [(t, x) for t, x in found if not self.log.has_key(tags.key(t, x, before))]
        day = local_day(now)
        return tags.apply(self, [(t, x, day) for t, x in found],
                          said=tags.SAID["mcp"]) if found else []

    def _tag(self, tag: str, text: str, said: bool, key: str | None = None) -> dict[str, Any]:
        """One //tag, through `_call` (its `key` dedupes); an exposed chat only proposes."""
        return tags.act(self, tag, text, said and not self.session.exposed)

    def _log_journal(self, p: dict) -> dict[str, Any]:
        text = " ".join(chats.drop_private(str(p.get("text") or "")).split())  # L1
        if not text:
            raise invalid("journal needs payload.text.")
        for name in ("mood", "energy"):
            v = p.get(name)
            if v is None:
                continue
            if type(v) is not int or not 1 <= v <= 5:
                raise invalid(f"{name} must be a whole number from 1 to 5.")
            text += f" · {name} {v}"
        now = self._now()
        day, hhmm = local_day(now), f"{now.astimezone():%H:%M}"
        if p.get("date") is not None:  # BIO-1: a backdated line; events keep the real day
            d = p["date"]
            if not (isinstance(d, str) and len(d) == 10 and loops.parse_day(d)) \
                    or not "2000-01-01" <= d <= day:
                raise invalid("date must be YYYY-MM-DD, from 2000-01-01 to today.")
            if d != day:
                text, hhmm = f"{text} (added {day})", "—"
                day = d
        if _pin_day(self):  # L1: as of the line's own day (a backdated line's, not today's)
            text = dates.pin(text, date.fromisoformat(day))
        self.vault.append_journal(day, hhmm, text)
        hint = self.sessions.note("journal", change={"tool": "journal"})
        return {"recorded": 1, "display_line": f"Journal {day}: logged",
                "next": self._next(MORE_NEXT) + hint}

    def _log_concept(self, p: dict, what: str) -> tuple[str, bool]:
        """(object, created): 'concept:<id>' for a known or newly made concept (_link_names),
        else 'inbox:<slug>' (a refused or hidden name, parked in inbox.md)."""
        if not p.get("concept"):
            raise invalid(f"{what} needs payload.concept.")
        [(name, cid, made)] = self._link_names([p["concept"]],
                                               text=str(p.get("summary") or p.get("note") or ""))
        if cid:
            return f"concept:{cid}", made
        return inbox_add(self.vault, name, f"keepfold_log {what}"), False

    def _log_assessment(self, p: dict, key: str | None) -> str:
        outcome = p.get("outcome")
        if not isinstance(outcome, str) or outcome not in ENUMS[("assessment", "outcome")]:
            raise invalid("outcome must be pending (not taken yet), taken (done, result not out), "
                          "pass, fail or unknown.")
        akind = p.get("assessment_kind")
        if akind is not None and (not isinstance(akind, str) or akind not in ASSESSMENT_KINDS):
            raise invalid("assessment_kind must be oa, mock, interview or test.")
        aid = p.get("assessment") or slugify(
            "-".join(str(p[k]) for k in ("company", "assessment_kind", "date") if p.get(k)))
        if not aid or not ID_RE.fullmatch(str(aid)):
            raise invalid("Pass payload.assessment (an id), or company, assessment_kind and date.")
        check_assessment_result(p.get("score"), p.get("date"))  # before felt_weak hits the inbox
        weak = []
        felt = p.get("felt_weak") or []
        # a bare string ("None", "DP") was iterated letter by letter into the inbox
        for name in [felt] if isinstance(felt, str) else felt:
            if str(name).strip().lower() in ("", "none", "null"):
                continue
            try:
                weak.append(self.catalog.resolve(name, CATALOG_KINDS))
            except VaultError:  # an unknown name becomes a concept, as in taught
                [(_, cid, _)] = self._link_names([name])
                weak.append(cid or inbox_add(self.vault, name, "felt weak in an assessment"))
        log_assessment(self.log, session=self.session.id, source="tutor", assessment=str(aid),
                       outcome=outcome, score=p.get("score"), company=p.get("company"),
                       kind=akind, date=p.get("date"), felt_weak=weak or None, key=key,
                       mode=self.session.mode, grader=self._grader(), now=self._now())
        return f"Assessment {render.esc(aid)}: {outcome}"

    def _log_problem(self, p: dict, key: str | None) -> str:
        status, h = p.get("status"), p.get("hints")
        if not isinstance(status, str) or status not in ENUMS[("problem", "status")]:
            raise invalid("status must be todo, attempted or solved.")
        if h is not None and (isinstance(h, bool) or not isinstance(h, int) or not 0 <= h <= 3):
            raise invalid("hints must be a whole number from 0 to 3.")
        pid = self._problem_id(p, status)
        obj = f"problem:{pid}"
        pl = {"kind": "problem_status", "problem": pid, "status": status}
        if h is not None:
            pl["hints"] = h
        self._append("log", {"object": obj, "payload": pl}, key)
        self.vault.set_field("problem", pid, "status", status)
        if status == "solved":
            self.vault.set_field("problem", pid, "solved_on", local_day(self._now()))
        return f"Problem {render.esc(pid)}: {status}"

    def _problem_id(self, p: dict, status: str) -> str:
        """The problem page payload.problem names (an id or a title). An unknown one is created
        with that title, linked to payload.concepts (names, through _link_names); never in review
        mode, which keeps the unknown_id error."""
        name = " ".join(str(p.get("problem") or "").split())
        for i in (name, page_slug(name)):
            if i and ID_RE.fullmatch(i) and self.vault.path_for("problem", i).is_file():
                return i
        if not page_slug(name) or self.session.mode == "review":
            return self._problem(name).partition(":")[2]  # unknown_id with a hint
        cs = p.get("concepts")
        names = [cs] if isinstance(cs, str) else cs if isinstance(cs, list) else []
        linked = [f"[[{c}]]" for _, c, _ in self._link_names(names, text=name) if c]
        fields = {"status": status, **({"concepts": linked} if linked else {})}
        try:
            pid = self._add_note("problem", name, None, fields, False)
        except VaultError as e:
            if e.code != "duplicate_suggested":
                raise
            same = [d for d in e.did_you_mean or [] if self.vault.path_for("problem", d).is_file()
                    and str(self.vault.read("problem", d).fields.get("title") or "").casefold()
                    == name.casefold()]
            if same:
                return same[0]
            pid = self._add_note("problem", name, None, fields, True)  # only a similar title
        self.sessions.note("added", change={"tool": "add", "id": pid, "kind": "problem",
                                            "op": "create", "target": None})
        return pid

    def _void(self, p: dict) -> dict[str, Any]:
        event_id, reason = p.get("event_id"), p.get("reason")
        if not isinstance(event_id, str) or not event_id:
            raise invalid("void needs payload {event_id, reason}: the id of the event to cancel.")
        _, target = void_event(self.log, session=self.session.id, event_id=event_id,
                               reason=reason, source="tutor", mode=self.session.mode,
                               now=self._now())
        self.sessions.note("voided", change={"tool": "void", "id": event_id,
                                             "kind": target.get("kind", "event"),
                                             "reason": reason.strip()})
        return {"voided": event_id, "next": self._next(MORE_NEXT)}
