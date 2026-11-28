"""keepfold_prime and keepfold_as_me: what an AI app reads about the user before it answers or
writes as them. A mixin of `keepfold.mcp_server.Keepfold`."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from keepfold import claims, delta, people, prime, render, review, share, voice, xray
from keepfold.events import local_day
from keepfold.index import _wrap
from keepfold.mcp_server import (
    AS_ME_ASKS,
    AS_ME_NEXT,
    AS_ME_TASK,
    CHAT_NEXT,
    COVERAGE_KINDS,
    COVERAGE_MAX,
    COVERAGE_SAYS,
    LATELY_DAYS,
    LATELY_HEAD,
    PRIME_LATELY,
    PRIME_MISTAKES,
    PRIME_POOL,
    PRIME_RECALLS,
    TAGS_NEXT,
    page_label,
)
from keepfold.prime import (
    LEGEND,
    PLAYBOOK_SAYS,
    PRIME_CAPS,
    PRIME_MAX,
    PRIME_PLAYBOOKS,
    PRIME_PROMPT,
)
from keepfold.vault import VaultError, invalid, scan_secrets

if TYPE_CHECKING:  # build_server puts the SDK's names in this module's globals
    from mcp.server.mcpserver import Context


class PrimeTools:
    """keepfold_prime, keepfold_as_me, `_prime_select` and `_lately`."""

    def _coverage(self) -> str:
        """RR-5: the sources the index holds, with their month ranges, most pages first:
        imports by `Source:` label, imported chats by app, browsing, journal and the user's
        own notes. Only what MCP sees: the index has no private page and no private browsing.
        "" when there is nothing."""
        from keepfold.semantic import is_chat
        from keepfold.topics import page_date

        span: dict[str, list] = {}  # shortcut: a walk per prime (ms on 5k pages); cache if slow
        for i, n in self.index._notes.items():
            if n["kind"] not in COVERAGE_KINDS:
                continue
            label = " ".join(page_label(n).split())[:30]
            name = ("browsing" if n["kind"] == "browse" else "journal" if n["kind"] == "journal"
                    else f"{label} chats" if label and is_chat(n) else label or "notes")
            s = span.setdefault(name, [0, "", ""])
            s[0] += 1
            if d := page_date(i, n["fields"], n["body"])[:7]:
                s[1], s[2] = min(s[1] or d, d), max(s[2], d)
        rows = sorted(span.items(), key=lambda kv: (-kv[1][0], kv[0]))
        out = [k + (f" {a}" if a else "") + (f" to {b}" if b != a else "")
               for k, (_, a, b) in rows[:COVERAGE_MAX]]
        return ", ".join(out) + (f", +{len(rows) - COVERAGE_MAX} more sources"
                                 if len(rows) > COVERAGE_MAX else "")

    def _lately(self, events: list[dict]) -> list[str]:
        """Page ids a `change` event (keepfold_add, keepfold_patch) or a `touch` log named in the
        last LATELY_DAYS local days, newest first; the caller filters what it may show."""
        since = (datetime.fromisoformat(local_day(self._now()))
                 - timedelta(days=LATELY_DAYS)).date().isoformat()
        out: list[str] = []
        for e in reversed(events):
            if str(e.get("day") or "") < since:
                continue
            p = e.get("payload") if isinstance(e.get("payload"), dict) else {}
            c = p.get("change") if e.get("kind") == "change" else None
            ids = [c.get("id")] if isinstance(c, dict) else (
                p.get("ids") if e.get("kind") == "log" and p.get("kind") == "touch" else None)
            out += [i for i in ids or [] if isinstance(i, str) and i not in out]
        return out

    def keepfold_prime(self, prompt: str, related_only: bool = False,
                     ctx: Context | None = None) -> dict[str, Any]:
        """The first call of every chat: the profile, the preference and personality lines
        that match the prompt, the contract core (keepfold.prime.core) and the top recalls within
        PRIME_MAX, plus the topic hub the prompt is about (topics.prompt_hub; none when
        unsure) unless this session logged it.
        `related_only` (a chat the hook primed) skips the light blocks. A full prime (a new
        chat) closes this owner process's open ambient session first and, in a Claude app
        Keepfold has no transcript of, starts the chat's live log with the prompt (_chat_log,
        returned as `chat`). Nothing else is written but the X-ray's row of served ids (no
        text, keepfold.xray), never a recall-log row. X-ray pins lead Related, mutes are left
        out."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise invalid("prompt must be the user's message.")
        prompt = prompt[:PRIME_PROMPT]
        chat = self._chat_log(prompt, new=not related_only)
        s = self.sessions.current
        if not related_only and self.role == "owner" and s is not None and s.mode == "ambient":
            # a full prime starts a new chat: one ambient session per chat, so undo is per chat
            self.sessions.close(summary="auto-closed: new chat")
            self.session = None
            self._reset()
        tagged = self._tags(prompt, hooked=related_only)
        if self.role == "owner" and not getattr(self, "_guard_taken", False):
            # R5a, first run: the guarded lines present at the upgrade are the baseline
            review.record_baseline(self.vault)
            self._guard_taken = True
        parts, xr, hits = self._prime_select(prompt, related_only)
        context = "\n\n".join(parts)
        if chat and not related_only:  # G3: a later turn's delta never repeats these
            self._served(chat).update(c for c, _ in delta.claim_lines(self.vault, parts))
        xray.record(self.vault, "keepfold_prime", self.sessions.client, xr["served"],
                    xr["trimmed"], len(context))
        touch = xr["served"]["hub"]
        # one ask per chat: a named problem's Attempt first line (prime.core, or the hook's
        # copy of it) wins over a due concept's offer
        attempt = (prime.named_problem(self.vault, prompt) is not None if related_only
                   else any(b.startswith(f"## {prime.ATTEMPT_HEAD}\n") for b in parts))
        offer = "" if attempt else self._offer(
            [h["id"] for h in hits if h.get("kind") == "concept" and h.get("match") != "linked"])
        return {"context": context, "touch": touch, **({"chat": chat} if chat else {}),
                **({"tags": tagged} if tagged else {}),
                "next": self._next(
            "Answer with this in mind; cite at most 2 pages as [[id]], only if they fit. "
            + (f"If the message is substantive, record keepfold_log(kind='touch', "
               f"payload={{'ids': {touch}}}) once. " if touch else "Nothing to touch. ")
            + "Record everything durable at once, unasked; about them: their own words -> "
              "keepfold_patch with user_confirmed=true, your inference without it (saved as a "
              "guess); ask only when it clashes with a line they gave." + offer)
            + (TAGS_NEXT if tagged else "") + (CHAT_NEXT.format(chat=chat) if chat else "")}

    def _prime_select(self, prompt: str, related_only: bool = False, save: bool = True
                      ) -> tuple[list[str], dict, list[dict]]:
        """keepfold_prime's selection: (its blocks, the X-ray's {served, trimmed} ids by block,
        the recall hits). It writes nothing itself but the index's snapshot; `save` False
        (dry_prime) also keeps recall and the topic hub from writing the vector cache."""
        from keepfold import semantic, topics  # numpy and the model load on first use

        xs, xr = xray.settings(self.vault), {"served": {}, "trimmed": {}}
        evs = list(self.log.iter_events())
        parts, left = ([], [PRIME_MAX]) if related_only else prime.core(
            self.vault, prompt, evs, self._now(), self._withheld(), xr, self._allow(),
            self.sessions.client)
        put = prime.putter(parts, left)
        self._refresh_index()
        # RR-5: the coverage line goes last, its room kept from the start
        cov = self._coverage()
        room = len(f"## Coverage\n{_wrap('coverage', cov)}\n{COVERAGE_SAYS}") + 2 if cov else 0
        left[0] -= room
        hits = semantic.search(self.index, prompt, PRIME_POOL, 1, quiet=self._quiet(),
                               save=save)[0]
        hub = topics.prompt_hub(self.index, semantic.load_embedder(), prompt, hits, save)
        hub = None if hub in xs["mute"] else hub
        # _notes holds no private or hidden page, so neither a pin nor a hit can serve one
        notes = self.index._notes
        said = {h["id"]: h.get("summary") or h["title"] for h in hits}
        # G4: the playbooks the prompt matches lead, out of the Related budget, and are not
        # repeated in Related or Lately
        before, found = left[0], self._playbooks(prompt, hits, notes, xs["mute"], evs)
        say = len(PLAYBOOK_SAYS) + 1  # the instruction line follows the wrapped lines
        cap = PRIME_CAPS["related"] * 3 // 4  # Related keeps a quarter, about one line
        lines = prime.fit([ln for _, ln in found[:PRIME_PLAYBOOKS]], "playbooks", cap - say,
                          left[0] - say)
        if any(ln.endswith(" (ask)") for ln in lines) and not any(
                p.startswith("## How to read the claims\n") for p in parts):
            put("How to read the claims", LEGEND)
        if lines:
            put("Playbooks", "\n".join(lines), "playbooks", cap, PLAYBOOK_SAYS)
        if not (parts and parts[-1].startswith("## Playbooks\n")):
            lines = []  # put found no room
        pcost = before - left[0]
        books = ([i for i, _ in found[:len(lines)]], [i for i, _ in found[len(lines):]])
        xr["served"]["playbooks"], xr["trimmed"]["playbooks"] = books
        # L10: pages already served as a block (the playbooks, the Attempt first problem) and
        # me/ pages (their lines are the blocks above, or the hook's) stay out of Related and
        # Lately
        shown = set(books[0]) | {m for b in parts if b.startswith(f"## {prime.ATTEMPT_HEAD}\n")
                                 for m in re.findall(r"\[\[([^\]|#^]+)", b)}
        ids = list(dict.fromkeys(  # X-ray pins first, then the ranked hits
            [i for i in sorted(xs["pin"]) if i in notes and not topics.is_hub(notes[i])]
            + [h["id"] for h in hits if (n := notes.get(h["id"])) and not topics.is_hub(n)
               and n["kind"] != "me" and h["id"] not in xs["mute"] and h["id"] not in shown
               ][:PRIME_RECALLS]))
        ids = [i for i in ids if i not in shown and notes[i]["kind"] != "me"]  # pins too
        # RR-21: pages touched lately, out of the Related budget; `notes` and the mutes keep
        # private, hidden and withheld pages out, as for Related
        recent = [i for i in self._lately(evs) if i in notes and i not in xs["mute"]
                  and i not in ids and i not in shown and notes[i]["kind"] != "me" and i != hub
                  and not semantic.is_chat(notes[i])][:PRIME_LATELY]
        lat = prime.fit([f"- [[{i}]] — {notes[i]['title']}" for i in recent], "lately",
                        min(PRIME_CAPS["related"] // 2, PRIME_CAPS["related"] - pcost), left[0])
        cost = pcost + (len(_wrap("lately", "\n".join(lat))) + len(LATELY_HEAD) + 6 if lat else 0)
        rel = prime.fit([f"- [[{i}]] — " + (said.get(i) or notes[i]["fields"].get("summary")
                                            or notes[i]["title"]) for i in ids],
                        "related", PRIME_CAPS["related"] - cost, left[0] - cost + pcost,
                        "related")
        if lat:  # before Related: it holds for the day, Related is this prompt's
            put(LATELY_HEAD, "\n".join(lat), "lately", cost)
        xr["served"]["lately"] = recent[:len(lat)]
        if rel:
            put("Related", "\n".join(rel), "related", PRIME_CAPS["related"] - cost,
                prime.more(len(ids) - len(rel), "related"))
        xr["served"]["related"], xr["trimmed"]["related"] = ids[:len(rel)], ids[len(rel):]
        # open mistakes on the hit concepts, as keepfold_context lists them
        ids = list(dict.fromkeys(
            m for h in hits if h.get("kind") == "concept" and h.get("match") != "linked"
            for m in getattr(self.index.states.get(h["id"]), "open_mistakes", None) or []
            if notes.get(m, {}).get("kind") == "mistake" and m not in xs["mute"]
        ))[:PRIME_MISTAKES]
        mis = prime.fit([f"- [[{m}]] — {notes[m]['fields'].get('summary') or notes[m]['title']}"
                         for m in ids], "mistakes", PRIME_CAPS["mistakes"], left[0], "mistake")
        if mis:
            put("Open mistakes", "\n".join(mis), "mistakes", PRIME_CAPS["mistakes"],
                prime.more(len(ids) - len(mis), "mistake"))
        xr["served"]["mistakes"], xr["trimmed"]["mistakes"] = ids[:len(mis)], ids[len(mis):]
        left[0] += room
        if cov:
            put("Coverage", cov, "coverage", room, COVERAGE_SAYS)
        xr["served"]["hub"] = [hub] if hub and hub not in self.touched else []
        return parts, xr, hits

    def _playbooks(self, prompt: str, hits: list[dict], notes: dict, mute: set[str],
                   evs: list[dict]) -> list[tuple[str, str]]:
        """G4: prime.playbooks under this client's profile."""
        return prime.playbooks(self.vault, prompt, hits, notes, mute, self._scopes(), evs)

    def keepfold_as_me(self, task: str, audience: str, act: str | None = None,
                     channel: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
        """PHASE4 V3: what writing as the user to `audience` may draw on, act-grade only; one
        event read; `_call` refuses it while the kill switch is on."""
        if not isinstance(task, str) or not task.strip() or len(task) > AS_ME_TASK:
            raise invalid(f"task must be 1 to {AS_ME_TASK} characters.")
        scan_secrets(task)
        voice.check_audience(audience)
        if act is not None and act not in voice.ACTS:
            raise invalid(f"act must be one of {', '.join(voice.ACTS)}.")
        if channel is not None and channel not in voice.CHANNELS:
            raise invalid(f"channel must be one of {', '.join(voice.CHANNELS)}.")
        person = people.get(self.vault, audience)
        if audience.startswith(people.PREFIX) and person is None:
            raise VaultError("unknown_id", f"No people page '{audience}'; add it with "
                             "keepfold_add(kind='person') or use a tag such as professor.")
        tags = {audience, "everyone"} | ({person["relation"]} if person and person["relation"]
                                         else set())
        events, now = list(self.log.iter_events()), self._now()
        served = self._served_claims(events)
        allowed = share.allowed(self.vault, audience) - self._withheld()
        mine, card, asks, other, withheld = [], [], [], 0, 0
        for i, r in enumerate(claims.tiers(self.vault, events)):
            if claims.expired(r["text"], local_day(now)):  # agenda 4: past its own until day
                continue
            if r["page"] == "voice":
                if r["section"].lower() not in tags:
                    continue
            elif r["page"] not in allowed:  # share tier: nothing of it leaves the server
                withheld += 1
                continue
            label, score = served.get((r["page"], r["section"], r["text"]), ("", 0.5))
            if label == " (ask)":
                asks.append((-score, -i, r["text"]))
            if not claims.act_grade(r, label):
                other += r["page"] != "voice"
                continue
            row = {"section": r["section"], "text": _wrap("claim", r["text"])}
            if r["page"] == "voice":
                card.append((r["section"].lower() == "everyone", -score, -i, row))
            else:
                mine.append((-score, -i, {"page": r["page"], **row}))
        mine, card = [x[-1] for x in sorted(mine)], [x[-1] for x in sorted(card)]
        aud = tags - {"everyone"}
        samples = voice.exemplars(self.vault, aud, act, channel)
        unknowns = [_wrap("claim", t) for *_, t in sorted(asks)[:AS_ME_ASKS]]
        if not any(s.get("audience") in aud for s in samples):
            unknowns.append(f"no voice samples for {audience}")
        if not any(c["section"].lower() != "everyone" for c in card):
            unknowns.append(f"no voice card for {audience}")
        out = {
            "self": {"lines": mine, "withheld": withheld, "unconfirmed": other,
                     **({"hint": f"{other} more lines are not confirmed; confirm them in "
                         "/keepfold reflect to use them here."} if other else {})},
            "voice": {"card": card, "exemplars": [
                {**{k: render.plain(str(s.get(k, "")), 80) for k in ("audience", "act",
                                                                      "channel", "day")},
                 "text": _wrap("voice", s["text"])} for s in samples]},
            "unknowns": unknowns,
            "next": AS_ME_NEXT}
        if person:
            out["person"] = {"id": person["id"], "title": render.plain(person["title"], 80),
                             "relation": person["relation"], "tier": person["tier"],
                             "text": _wrap(person["id"], person["body"])}
        self._access("as_me", task=task, audience=audience)
        return out
