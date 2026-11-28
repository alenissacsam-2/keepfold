"""keepfold_start and its home card, keepfold_ask, keepfold_grade and keepfold_end: a study session.
A mixin of `keepfold.mcp_server.Keepfold`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from keepfold import access, chats, learning_eval, levels, loops, planner, prime, render
from keepfold.catalog import page_slug
from keepfold.contract import MAX_CHARS as CONTRACT_MAX
from keepfold.events import SESSION_RE, STUDY_MODES, _check_fields, local_day
from keepfold.index import TRUNCATED
from keepfold.mcp_server import (
    ASK_FOCUS,
    ASK_LONG,
    ASK_NEXT,
    ASK_TRAPS,
    HOME_UNLEARNED,
    HOME_WAITING,
    MOCK_BRIEF,
    MOCK_NEXT,
    MORE_NEXT,
    PAPER_EMPTY_NEXT,
    PAPER_NEXT,
    QUIZ_MAX,
    START_NEXT,
    SWEEP_NEXT,
    WATCHED_PAGES,
    WHY_MAX,
    ItemType,
    Verdict,
)
from keepfold.prime import CONTEXT_MAX
from keepfold.reflect import unconfirmed
from keepfold.vault import ID_RE, VaultError, invalid, is_private

if TYPE_CHECKING:  # build_server puts the SDK's names in this module's globals
    from mcp.server.mcpserver import Context


class StudyTools:
    """keepfold_start, keepfold_ask, keepfold_grade and keepfold_end."""

    def keepfold_start(self, mode: Literal["home", "learn", "review", "log", "short", "about",
                                         "week", "capture", "reflect", "project", "save",
                                         "history", "sweep", "mock"] = "home",
                     topic: str | None = None, model: str | None = None,
                     minutes: int | None = None,
                     ctx: Context | None = None) -> dict[str, Any]:
        paper = mode == "mock" and (topic or "").lower().split()[:1] == ["paper"]
        if minutes is not None and not paper and mode not in ("learn", "short", "review"):
            raise invalid("minutes is for a learn, short or review plan, or a mock paper "
                          "(mode 'mock', topic 'paper').")
        minutes = planner.check_minutes(minutes, planner.PAPER_PRESETS if paper else None)
        s = self.sessions.start(mode, topic, model)
        self.session = s
        self._reset()
        today_s = local_day(s.started)
        events = self._events()  # one read for the fold, the plan and the home forecast gate
        states, mistakes = planner.current_states(self.vault, today_s, self.catalog, events)
        if mode == "home":
            return self._home(s, states, mistakes, today_s, events)
        today = planner.today(self.vault, self.catalog, states, now=s.started, mode=mode,
                              topic=topic, events=events)
        if minutes and not paper:  # council item 7: the plan's tail that does not fit goes
            today = planner.fit(today, minutes)
        notes = list(self.vault.iter_notes("mistake"))
        titles = {n.id: str(n.fields.get("title") or n.id) for n in notes}
        # D1: private mistakes and, OQ-22, those about a private subject
        hush = planner.hidden_mistakes(mistakes, hide := self._hidden())
        open_m = sorted((m for m in mistakes.values() if m.status == "open" and m.id not in hush),
                        key=lambda m: (-m.count, m.id))
        display = render.start_card(
            mode=mode, today=today,
            assessments=planner.assessments(self.vault),
            open_mistakes=[(titles.get(m.id, m.id), m.count) for m in open_m], date=today_s,
            deadlines=planner.deadlines(self.vault, today_s))
        extra = {}
        if mode == "week":
            w = extra["week"] = planner.week_summary(self.vault, self.catalog, now=s.started)
            display += (f"\n**This week:** {w['sessions']} sessions · {w['grades']} answers, "
                        f"{w['correct']} correct · {len(w['changed'])} concepts moved")
            # council item 6: display-only, over all answers so far, never fed to scheduling
            w["confidence"] = learning_eval.confidence_mirror(events, self._pc, hide)
            if lines := learning_eval.mirror_lines(w["confidence"]):
                display += ("\n**How sure vs how often right** (all answers so far):\n"
                            + "\n".join(f"- {line}" for line in lines))
        if minutes and not paper:
            display += f"\n**Time:** {planner.budget_note(today, minutes)}"
        nxt = START_NEXT[mode]
        if paper:  # council item 8: a practice paper the assistant writes from `paper`
            minutes = minutes or planner.PAPER_MINUTES
            p = extra["paper"] = planner.mock_paper(self.vault, self.catalog, states,
                                                    now=s.started, minutes=minutes)
            display += (f"\n**Practice paper:** {len(p)} questions, "
                        f"{planner.budget_note(p, minutes)}")
            nxt = PAPER_NEXT if p else PAPER_EMPTY_NEXT
        if topic and mode in ("learn", "short"):
            nxt = f"Focus: {topic}. " + nxt
        if mode == "short":
            nxt += " Offer to stop after 5 items."
        contract = self._contract(topic, today)
        if contract.endswith(TRUNCATED):
            nxt += (f" The teaching contract was cut at {CONTRACT_MAX:,} characters; suggest the "
                    "user runs `keepfold doctor` and trims me/teaching-contract.md.")
        brief = render.brief_text(self._raw("BRIEF.md"))
        if mode == "mock":
            brief = MOCK_BRIEF + "\n\n" + brief
            # dogfood r4 #5: the model has no clock; one server stamp keys every phase
            extra["stamp"] = s.started.astimezone(UTC).strftime("%Y%m%d%H%M%S")
        return {"session": s.id, "mode": mode, "display": display, "brief": brief,
                "contract": contract, "today": today, **extra, "next": nxt}

    def _home(self, s, states: dict, mistakes: dict, today_s: str,
              events: list[dict]) -> dict[str, Any]:
        """The lean /keepfold start: a map, the contract's core rules and the preferences."""
        from keepfold import dashboard  # lazy: the brain's imports stay off the server's start
        hide = self._hidden()
        hush = planner.hidden_mistakes(mistakes, hide)
        by_domain: dict[str, list[int]] = {}  # domain -> [in progress, solid, not started]
        for cid, e in self.catalog.entries.items():
            if e.kind != "concept" or cid in hide:
                continue
            st = self._state(states, cid)
            by_domain.setdefault(e.domain, [0, 0, 0])[
                1 if st == "solid" else 2 if st == "not_started" else 0] += 1
        ranked = sorted(by_domain.items(), key=lambda kv: (-(kv[1][0] + kv[1][1]), kv[0]))
        due = sorted(((cid, st) for cid, st in states.items()
                      if cid not in hide and planner.is_due(st, today_s)),
                     key=lambda kv: (kv[1].state != "needs_refresh", str(kv[1].next_due or ""),
                                     kv[0]))
        # idea loops are never shown as (overdue) commitments (COACH-6)
        open_loops = sorted(loops.loops(self.vault), key=lambda r: r["loop_type"] == "idea")
        week = planner.week_summary(self.vault, self.catalog, now=s.started)
        moved = [c for c in week["changed"] if c["after"] == "solid" and c["id"] not in hide]
        unlearned = chats.unlearned_count(self._learned())  # G3: past chats to learn from
        display = render.home_card(
            domains=[(self._title(d), *n) for d, n in ranked],
            due=[self._title(cid) for cid, _ in due],
            open_mistakes=sum(1 for m in mistakes.values() if m.status == "open"
                              and m.id not in hush),
            projects=[str(n.fields.get("title") or n.id) for n in self.vault.iter_notes("project")
                      if n.fields.get("status") == "active" and not is_private(n)
                      and access.scope_kind(self._scopes(), "project")],
            loops=[r["title"] + (f" (due {r['due']})" if r["due"] and r["loop_type"] != "idea"
                                 else "") for r in open_loops],
            ideas=[r["title"] + (f" \u2014 {r['summary']}" if r["summary"] else "")
                   for r in loops.resurface(self.vault, today_s, 2)],
            coming_up=self._coming_up(states, hide, today_s, events, s.started),
            solid=[c["title"] for c in moved if c["before"] != "needs_refresh"],
            back=[c["title"] for c in moved if c["before"] == "needs_refresh"],
            resolved=len(week["mistakes_resolved"]),
            unconfirmed=(waiting := len(unconfirmed(self.vault, today_s)[0])),
            revisit=[r["id"] for r in loops.follow_ups(self.vault, today_s)],
            edits=self._ambient_edits(s.id), unlearned=unlearned,
            undated=self._undated(today_s), studies=dashboard.studies(self.vault, events))
        return {"session": s.id, "mode": "home", "display": display, "unlearned": unlearned,
                "contract": self._contract(None, []),
                "preferences": self._home_preferences(),
                "next": START_NEXT["home"]
                + (HOME_UNLEARNED if unlearned else "")
                + (HOME_WAITING if unlearned or waiting else "")
                + (" If chats are left, offer to read 5 more." if unlearned else "")}

    def _coming_up(self, states: dict, hide: set, today_s: str, events: list[dict],
                   now: datetime) -> dict | None:
        """The soonest pending, non-private assessment within planner.WINDOW days, with the
        dashboard's readiness counts (tested concepts that are Solid of those tested) and,
        once calibrated, the recall forecast for its day (before that, `calibrating`)."""
        private = {n.id for n in self.vault.iter_notes("assessment") if is_private(n)}
        up = [a for a in planner._upcoming(self.vault, datetime.fromisoformat(today_s).date())
              if a["days"] <= planner.WINDOW and a["id"] not in private]
        if not up:
            return None
        tested = [c for c in planner._weights(self.catalog, up[0]["tests"]) if c not in hide]
        # v6 item 8, B5: the recall forecast joins the Solid count only once MIN_PAIRS cold
        # forecasts with history show it beats the base rate
        pairs, ok = learning_eval.calibrated(events, self._pc)
        return {**up[0], "total": len(tested), "more": len(up) - 1,
                "solid": sum(self._state(states, c) == "solid" for c in tested),
                "forecast": planner.recall_on_day(self.vault, self.catalog, states, up[0], now)
                if ok else None,
                "calibrating": None if ok else {"n": pairs, "need": learning_eval.MIN_PAIRS}}

    def _undated(self, today_s: str) -> list[str]:
        """Pending, non-private assessments with no date or a past one: the exam clock skips
        them until a date (or a result) goes in. `unknown` outcomes are never listed."""
        private = {n.id for n in self.vault.iter_notes("assessment") if is_private(n)}
        return [a["id"] for a in planner.assessments(self.vault)
                if (a["outcome"] or "pending") == "pending" and a["id"] not in private
                and str(planner._day(a["date"] or "") or "") < today_s]

    def _ambient_edits(self, sid: str) -> list[dict]:
        """SEC-1: sessions outside a /keepfold study mode that patched the teaching contract or
        the profile since the previous home start, latest first."""
        found: dict[str, dict] = {}
        for e in self.log.iter_events():
            if e.get("kind") == "open" and e.get("mode") == "home" and e.get("session") != sid:
                found = {}
            p = e.get("payload") if e.get("kind") == "change" else None
            c = p.get("change") if isinstance(p, dict) else None
            if (isinstance(c, dict) and c.get("tool") == "patch" and c.get("kind") == "me"
                    and c.get("id") in WATCHED_PAGES and e.get("mode") not in STUDY_MODES
                    and SESSION_RE.match(str(e.get("session")))):
                r = found.setdefault(e["session"], {"session": e["session"], "pages": []})
                r["day"] = e.get("day", "")
                if WATCHED_PAGES[c["id"]] not in r["pages"]:
                    r["pages"].append(WATCHED_PAGES[c["id"]])
        return sorted(found.values(), key=lambda r: r["day"], reverse=True)

    def _contract(self, topic: str | None, today: list[dict]) -> str:
        """The teaching contract, domain overrides scoped to the topic and today's top 5; ""
        for a client that may not get the page (private, or outside its profile)."""
        if "teaching-contract" in self._withheld():
            return ""
        ids = [i["id"] for i in today[:5] if i.get("id")]
        if topic:
            try:
                ids.append(self.catalog.resolve(topic, ("concept", "area", "domain")))
            except VaultError:
                pass
        return prime.contract(self.vault, {self.catalog.entries[i].domain for i in ids
                                           if i in self.catalog.entries})

    def _home_preferences(self) -> str:
        """Personality + preferences as keepfold_context(scope="all") builds them, capped alike."""
        parts: list[str] = []
        self._personality_prefs("all", prime.putter(parts, [CONTEXT_MAX]), nag=True)
        return "\n\n".join(parts)

    def keepfold_ask(self, concept: str, item_type: ItemType, question: str,
                   answer_key: str | None = None, problem: str | None = None,
                   also: list[str] | None = None, targets_mistakes: list[str] | None = None,
                   key: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
        hide = self._hidden()
        fields = self._item_fields(concept=concept, question=question, item_type=item_type,
                                   problem=problem, also=also, hide=hide)
        cid = fields["object"].split(":", 1)[1]
        if answer_key is not None:
            fields["answer_key"] = answer_key
        if targets_mistakes:
            fields["targets_mistakes"] = targets_mistakes
        events = self._events()
        p = levels.predicted_p(events, cid, self._now(), self._pc)
        ev = self._append("ask", fields, key, predicted_p=p)
        hint = self.sessions.note("asked", cid)
        self.pending.add(ev["id"])
        nxt = ASK_NEXT + hint
        if len(question) > ASK_LONG or question.count("?") >= 2:
            nxt += ASK_FOCUS
        open_m = [m for m in getattr(self._fold(events).get(cid), "open_mistakes", None) or []
                  if self._sees(m, hide)]
        if open_m and not targets_mistakes:
            nxt += f" Open mistakes here: {', '.join(open_m)}; target them in a later ask."
        if not targets_mistakes:  # v6 idea 9: imported mistake notes never opened by a grade
            idx = self.index
            idx.refresh()
            traps = sorted(m for m in idx.linked_mistakes(cid) if m not in open_m
                           and ID_RE.fullmatch(m)
                           and idx._notes.get(m, {}).get("kind") == "mistake")[:ASK_TRAPS]
            if traps:
                nxt += (f" Mistake notes linked here: {', '.join(traps)}; ask one as a trap "
                        "in a later ask (targets_mistakes).")
        # LEARN-5: predicted_p stays on the event only; showing it to the tutor anchors grading
        return {"item_id": ev["id"], "next": nxt}

    def keepfold_grade(self, learner_answer: str | None = None, item_id: str | None = None,
                     concept: str | None = None, question: str | None = None,
                     item_type: ItemType | None = None, problem: str | None = None,
                     also: list[str] | None = None, verdict: Verdict | None = None,
                     confidence: int | None = None, hints: int = 0,
                     mistakes: list[str] | None = None, new_mistake: dict | None = None,
                     targets_mistakes: list[str] | None = None,
                     key: str | None = None, items: list[dict[str, Any]] | None = None,
                     ctx: Context | None = None) -> dict[str, Any]:
        if items is not None:  # a batch: concept and key apply to the call, the rest per item
            if hints or any(x is not None for x in (
                    learner_answer, item_id, question, item_type, problem, also, verdict,
                    confidence, mistakes, new_mistake, targets_mistakes)):
                raise invalid("With items (a batch), put answers and verdicts on each item; "
                           "only concept and key go on the call.")
            return self._quiz_result(items, concept, key)
        if learner_answer is None:
            raise invalid("learner_answer is required (or items for a batch).")
        answer_key = None
        if item_id:
            ask = self._ask_event(item_id)
            fields = {"object": ask["object"], "ref": item_id}
            answer_key = ask.get("answer_key")
        elif concept and question:
            fields = self._item_fields(concept=concept, question=question,
                                       item_type=item_type, problem=problem, also=also)
        else:
            raise invalid("Pass item_id from keepfold_ask, or both concept and question.")
        cid = fields["object"].split(":", 1)[1]
        fields["learner_answer"] = learner_answer
        self._graded(fields, learner_answer, verdict, answer_key)
        fields["hints"] = hints
        if confidence is not None:
            fields["confidence"] = confidence
        if targets_mistakes:  # G1: a grade with no ask; the fold unions it with an ask's
            fields["targets_mistakes"] = targets_mistakes
        mistakes = list(mistakes or [])
        # never lose evidence: a confident miss without a named mistake is recorded, then nudged
        unnamed_miss = (fields["verdict"] == "wrong" and confidence == 4 and not mistakes
                        and not new_mistake)
        _check_fields({**fields, "mistakes": mistakes})  # validate before a note is created
        out: dict[str, Any] = {}
        if new_mistake:
            mid, similar = self._mint_mistake(cid, new_mistake)
            out["mistake"] = mid
            if similar:
                out["similar_mistakes"] = similar
            mistakes.append(mid)
        if mistakes:
            fields["mistakes"] = list(dict.fromkeys(mistakes))
        events = self._events()
        p = levels.predicted_p(events, cid, self._now(), self._pc)
        ev = self._append("grade", fields, key, predicted_p=p)
        prior = [e for e in events if e["id"] != ev["id"]]  # B11: a retry's grade is in events
        before, after = self._fold(prior), self._fold([*prior, ev])
        hint = self.sessions.note("graded", cid)
        self.pending.discard(item_id)
        self.taught.pop(cid, None)
        state = self._state(after, cid)
        note = levels.gap_note(after.get(cid))
        out = {"state_before": self._state(before, cid), "state_after": state,
               "display_line": self._line(cid, state) + (f" · {note}" if note else ""), **out}
        if ev.get("model_verdict"):
            out["verdict"] = ev["verdict"]
        mode = self.session.mode
        out["next"] = self._next(
            MOCK_NEXT if mode == "mock" else
            "Confident miss: ask what went wrong, then pass new_mistake when you re-test."
            if unnamed_miss and ev["verdict"] == "wrong" else
            "Ask the next question (keepfold_ask), or move on." if ev["verdict"] == "correct"
            else SWEEP_NEXT if mode == "sweep"
            else "Teach the gap briefly, then re-test with a new keepfold_ask.")
        if out.get("similar_mistakes"):
            out["next"] += (f" Similar mistakes exist ({', '.join(out['similar_mistakes'])}); "
                            "if one is the same pattern, tag later answers with its id instead.")
        out["next"] += hint
        return out

    def _mint_mistake(self, cid: str, spec: dict) -> tuple[str, list[str]]:
        """Create a mistake note for `cid` unless that exact id exists; return (id, similar ids)."""
        title, why = str(spec.get("title") or "").strip(), str(spec.get("why") or "").strip()
        if len(why) > WHY_MAX:
            raise invalid(f"new_mistake.why is {len(why)} characters; keep it to {WHY_MAX}.")
        slug = page_slug(title)
        if not slug:
            raise invalid("new_mistake.title needs at least one letter or digit.")
        domain = self.catalog.entries[cid].domain if cid in self.catalog.entries else ""
        mid = (f"{domain}.{slug}" if domain else slug)[:81].rstrip("-.")
        self.index.refresh()
        similar = [h["id"] for h in self.index.find(title, "mistake", 4) if h["id"] != mid][:3]
        if self.vault.path_for("mistake", mid).is_file():
            return mid, similar
        self.vault.create_note("mistake", mid, {"title": title, "concepts": [f"[[{cid}]]"]},
                               f"# {title}\n\n## Pattern\n{why}\n\n## Fix\n\n## Seen\n")
        self.sessions.note("added", change={"tool": "add", "id": mid, "kind": "mistake"})
        self.new_mistakes.append(title)
        return mid, similar

    def _quiz_result(self, items: list[dict[str, Any]], concept: str | None,
                     key: str | None) -> dict[str, Any]:
        if not 1 <= len(items) <= QUIZ_MAX:
            raise invalid(f"Send 1 to {QUIZ_MAX} items.")
        batch = []
        hide = self._hidden()
        for i, it in enumerate(items, 1):
            if not isinstance(it, dict):
                raise invalid(f"Item {i} must be an object.")
            if it.get("item_id"):
                ask = self._ask_event(it["item_id"])
                fields = {"object": ask["object"], "ref": it["item_id"]}
                answer_key = ask.get("answer_key")
            else:
                name = it.get("concept") or concept
                if not name:
                    raise invalid(f"Item {i} has no concept; pass concept on the item or the "
                               "call.")
                fields = self._item_fields(concept=name, question=str(it.get("question", "")),
                                           item_type=it.get("item_type"), hide=hide)
                answer_key = None
            answer = str(it.get("answer", ""))
            fields["learner_answer"] = answer
            verdict = it.get("verdict")
            if verdict is None and isinstance(it.get("correct"), bool):
                verdict = "correct" if it["correct"] else "wrong"
            self._graded(fields, answer, verdict, answer_key)
            for name in ("confidence", "hints", "mistakes"):
                if it.get(name) is not None:
                    fields[name] = it[name]
            _check_fields(fields)  # the whole batch is valid before anything is written
            batch.append(fields)
        events = self._events()
        new = []
        now = self._now()
        with self.vault.lock():
            for i, fields in enumerate(batch, 1):
                p = levels.predicted_p(events, fields["object"].split(":", 1)[1], now, self._pc)
                new.append(self._append("grade", fields,
                                        None if key is None else f"{key}:{i}", predicted_p=p))
        ids = {e["id"] for e in new}
        prior = [e for e in events if e["id"] not in ids]  # B11: a retry's grades are in events
        before, after = self._fold(prior), self._fold([*prior, *new])
        cids = list(dict.fromkeys(f["object"].split(":", 1)[1] for f in batch))
        hint = ""
        for f in batch:
            cid = f["object"].split(":", 1)[1]
            hint += self.sessions.note("graded", cid)
            self.pending.discard(f.get("ref"))
            self.taught.pop(cid, None)
        per = [{"id": c, "state_before": self._state(before, c),
                "state_after": self._state(after, c)} for c in cids]
        right = sum(f["verdict"] == "correct" for f in batch)
        lines = [f"Quiz: {right}/{len(batch)} correct"]
        for p in per:
            note = levels.gap_note(after.get(p["id"]))
            lines.append(f"- {render.esc(self._title(p['id']))} "
                         f"{levels.GLYPH[p['state_before']]} → {levels.GLYPH[p['state_after']]} "
                         f"{levels.LABEL[p['state_after']]}" + (f" · {note}" if note else ""))
        mode = self.session.mode
        if (mode != "mock" and not self.asked_confidence  # mock.md: how sure on 2 phases only
                and all(f.get("confidence") is None for f in batch)):
            self.asked_confidence = True
            hint += " Next time, collect how sure (1-4) per question."
        nxt = (MOCK_NEXT if mode == "mock" else MORE_NEXT if right == len(batch)
               else SWEEP_NEXT if mode == "sweep" else "Teach the missed items, then re-test.")
        return {"recorded": len(batch), "per_concept": per, "display": "\n".join(lines),
                "next": self._next(nxt) + hint}

    def keepfold_end(self, summary: str = "", ctx: Context | None = None) -> dict[str, Any]:
        new_mistakes = list(self.new_mistakes)
        res = self.sessions.close(summary)
        self._reset()
        display = render.end_card(
            counts=res["counts"], moved=res["moved"],
            unchecked=[u["title"] for u in res["unchecked"]], new_mistakes=new_mistakes,
            next_items=res["today"], plan_cleared=not res["today"],
            duration_min=res["minutes"], changes=res["changes"],
            not_committed=res.get("not_committed", []))
        return {"display": display, "counts": res["counts"],
                "unchecked": [u["id"] for u in res["unchecked"]], "commit": res["commit"],
                "not_committed": res.get("not_committed", []), "next": "session closed"}
