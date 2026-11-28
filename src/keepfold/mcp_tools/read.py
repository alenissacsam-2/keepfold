"""keepfold_find, keepfold_recall, keepfold_read, keepfold_context and keepfold_chats: what the
vault holds, read back. A mixin of `keepfold.mcp_server.Keepfold`."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from keepfold import access, chats, levels, loops, people, prime, render, xray
from keepfold import session as session_mod
from keepfold import where as where_mod
from keepfold.events import live, local_day
from keepfold.index import (
    GLYPHS,
    MAX_HITS,
    READ_CAP_CHARS,
    TRUNCATED,
    _edge_targets,
    _neutralise,
    _preview,
    _wrap,
)
from keepfold.mcp_server import (
    CHECK_CHARS,
    CHECK_MIN,
    CHECK_NEXT,
    CHECK_SENTENCES,
    CONTEXT_CAPS,
    CORE_ONLY_NEXT,
    EVIDENCE,
    EXTERNAL_SOURCE,
    HARVEST_NEXT,
    MINE,
    RAW_TEXT_SUFFIXES,
    READ_SIMILAR,
    SENTENCE_RE,
    TIMELINE_DAYS,
    TIMELINE_KINDS,
    TIMELINE_MAX,
    _find_out,
    _hit_ids,
    _same_file,
    _wrap_fields,
    page_label,
)
from keepfold.prime import CONTEXT_MAX, NOT_RECORDED, WITHHELD
from keepfold.vault import VaultError, check_day, invalid, is_private

if TYPE_CHECKING:  # build_server puts the SDK's names in this module's globals
    from mcp.server.mcpserver import Context


class ReadTools:
    """keepfold_find, keepfold_recall, keepfold_read, keepfold_context and keepfold_chats."""

    def _day_of(self, id: str, n: dict, path) -> str:
        """A page's day as Chat dates it (ask._note_day): its page date, else for a note or
        resource its file's last change, read from the index's stat; "" when undated."""
        from keepfold.topics import page_date  # numpy: not at server import

        if d := page_date(id, n["fields"], n["body"]):
            return d
        st = self.index._stats.get(path) if n["kind"] in ("note", "resource") else None
        return local_day(datetime.fromtimestamp(st[0] / 1e9, UTC)) if st else ""

    def _keep(self, since: str | None, until: str | None, source: str | None,
              where: str | None = None):
        """RR-31: `keep(id)`, true for a page dated in [since, until] whose import label is
        `source` (any case; MINE: no import wrote it) and whose frontmatter passes `where`
        (FW, where.match), or None with no filter. An undated page never passes a date filter.
        Bad input raises invalid_argument."""
        for name, v in (("since", since), ("until", until)):
            if v is not None:
                check_day(name, v)
        if since and until and since > until:
            raise invalid("since must be on or before until.")
        if source is not None and not (isinstance(source, str) and source.strip()):
            raise invalid("source must be an import label such as Git, Notes or Highlights, "
                          f"or '{MINE}' for pages no import wrote.")
        clauses = where_mod.parse(where) if where is not None else None
        if since is None and until is None and source is None and clauses is None:
            return None
        lo, hi = since or "", until or "9999"
        want = source.strip().casefold() if source else None
        notes, paths = self.index._notes, {i: p for p, i in self.index._ids.items()}

        def keep(i: str) -> bool:
            n = notes.get(i)
            if n is None or want is not None and (page_label(n).casefold() or MINE) != want:
                return False
            if clauses and not where_mod.match(clauses, n["fields"]):
                return False
            if not (since or until):
                return True
            d = self._day_of(i, n, paths.get(i))
            return bool(d) and lo <= d <= hi
        return keep

    def _searched(self, kinds: tuple, keep, by: str, chats: bool | None = None) -> dict:
        """RR-14: what an empty or weak search covered, so no answer reads as "no record":
        the pages it could see (of `kinds`, past `keep`), how, and what never takes part."""
        n = sum(1 for i, x in self.index._notes.items()
                if (not kinds or x["kind"] in kinds) and (keep is None or keep(i)))
        out = {"pages": n, "kinds": list(kinds) or "all", "by": by,
               "excluded": ["private pages", "private browsing"]
               + ["pages this app's profile withholds"] * bool(self._index_withheld())}
        if chats is False:
            out["ranked_lower"] = "imported chats (chats=true lifts them)"
        return out

    @staticmethod
    def _absent(s: dict, weak: bool, more: str) -> str:
        """RR-14's `next`: not found (or only weakly) in N pages, never "no record exists"."""
        return (("Only weak matches" if weak else "Not found") + f" in the {s['pages']} pages "
                f"searched ({s['by']})" + (", so check them before relying on them" if weak
                                           else "") + ": not recorded in Keepfold is not the "
                f"same as never happened. {more}")

    def _timeline(self, query: str, limit: int | None, since: str | None, until: str | None,
                  source: str | None) -> dict[str, Any]:
        """R26: the pages from the days around one page, oldest first. The anchor is `query`
        as a page id (keepfold_read's rule), else find's top hit for the words, and only a page in
        this client's index; the window is its day ±TIMELINE_DAYS, a bound each that since/until
        override (one alone also stretches the other to a window's width); at most TIMELINE_MAX, the
        nearest the anchor kept. Index pages only (so what find serves this client), dated as
        the since/until filter dates them (`_day_of`, `_keep`)."""
        self._keep(since, until, source)  # bad input raises before any lookup
        cap = TIMELINE_MAX if limit is None else max(1, min(TIMELINE_MAX, int(limit)))
        idx, q = self.index, (query or "").strip()
        try:
            aid = idx._resolve(q) if q else None
        except VaultError:
            aid = None
        if aid is None and q:
            aid = next((h["id"] for h in idx.find(q, None, 1, syntax=True)), None)
        if aid not in idx._notes:  # a page this client may not see answers as no anchor
            return {**_find_out([]), "next": self._next(
                "No page matched to centre a timeline on; pass a page id from a keepfold_find or "
                "keepfold_recall hit as query.")}
        paths = {i: p for p, i in idx._ids.items()}
        day = self._day_of(aid, idx._notes[aid], paths.get(aid))
        if not day and not (since and until):
            return {**_find_out([]), "next": self._next(
                f"[[{aid}]] has no date; pass since and until (YYYY-MM-DD) for the window.")}
        at = datetime.fromisoformat(day or since)

        def shift(d: str, days: int) -> str:
            return (datetime.fromisoformat(d) + timedelta(days=days)).date().isoformat()

        lo, hi = since or shift(day, -TIMELINE_DAYS), until or shift(day, TIMELINE_DAYS)
        # one bound given: the other is the anchor's, or a window's width past the given one
        if since and not until:
            hi = max(hi, shift(since, 2 * TIMELINE_DAYS))
        if until and not since:
            lo = min(lo, shift(until, -2 * TIMELINE_DAYS))
        by_source = self._keep(None, None, source)
        rows = [(d, i, n) for i, n in idx._notes.items()
                if (n["kind"] in TIMELINE_KINDS or i == aid)
                and (by_source is None or by_source(i))
                and (d := self._day_of(i, n, paths.get(i))) and lo <= d <= hi]
        o = at.toordinal()
        rows.sort(key=lambda r: (r[1] != aid, abs(datetime.fromisoformat(r[0]).toordinal() - o),
                                 r[0], r[1]))
        rows = sorted(rows[:cap], key=lambda r: (r[0], r[1]))
        lines = [f"{'▸' if i == aid else '-'} {d} · {n['kind']} · [[{i}]] · "
                 + " ".join(str(n["title"]).split()) for d, i, n in rows]
        return {"hits": [{"id": i, "kind": n["kind"], "day": d, **({"anchor": True} if i == aid
                                                                  else {})} for d, i, n in rows],
                "text": _wrap("find", "\n".join(lines)) if rows else "",
                "next": self._next(f"Pages from {lo} to {hi}, oldest first" + (
                    f"; ▸ marks [[{aid}]]" if rows and aid in {r[1] for r in rows} else "")
                    + ". Call keepfold_read on an id for details.")}

    def keepfold_find(self, query: str = "", kind: Literal[
            "concept", "area", "domain", "mistake", "problem", "assessment", "application",
            "resource", "note", "project", "decision", "journal", "loop", "due",
            "ready", "weak", "resurface", "timeline"] | None = None,
            limit: int | None = None, since: str | None = None, until: str | None = None,
            source: str | None = None, where: str | None = None,
            ctx: Context | None = None) -> dict[str, Any]:
        out = self._find(query, kind, limit, since, until, source, where)
        # R27b: the X-ray keeps the ids it returned (never the query), for the broad-read flag
        xray.record(self.vault, "keepfold_find", self.sessions.client, {"found": _hit_ids(
            out.get("hits") or out.get("items"))}, {}, len(str(out.get("text") or "")))
        return out

    def _find(self, query: str, kind: str | None, limit: int | None, since: str | None,
              until: str | None, source: str | None, where: str | None = None
              ) -> dict[str, Any]:
        if where is not None and kind in ("resurface", "timeline"):
            raise invalid(f"where does not work with kind={kind}; drop where, or use "
                          "another kind (resurface and timeline take none).")
        if kind == "resurface":  # ideas due to be shown again; query is not used
            return self._resurface(3 if limit is None else limit)
        if kind == "timeline":
            self._refresh_index()
            return self._timeline(query, limit, since, until, source)
        limit = 8 if limit is None else limit
        self._refresh_index()
        keep = self._keep(since, until, source, where)
        listing = where is not None and not (query or "").strip()
        if kind == "loop":  # open first; with no query, list them (FTS needs a query)
            rows = [r for r in loops.loops(self.vault, None) if keep is None or keep(r["id"])]
            if query:
                found = {h["id"] for h in self.index.find(query, kind, 8, syntax=True)}
                rows = [r for r in rows if r["id"] in found]
            hits = rows[:max(1, min(limit, 8))]
            return {**_find_out(hits), "next": self._next(
                "Call keepfold_read on an id for details." if hits else "No loops.")}
        if kind == "problem":  # every problem in rank order, then the filter and the limit
            hits = [h for h in self.index.problems(query, 10**6, syntax=True)
                    if keep is None or keep(h["id"])][:max(1, min(limit, 8))]
        elif listing and kind not in ("due", "ready", "weak"):
            hits = self._listing(kind, keep, limit)
        else:
            hits = self.index.find(query, kind, limit, keep=keep, syntax=True)
        if hits:
            return {**_find_out(hits), "next": self._next("Call keepfold_read on an id for "
                                                          "details.")}
        s = self._searched(("concept",) if kind in ("due", "ready", "weak") else
                           (kind,) if kind else (), keep, "where filter" if listing else "keyword")
        return {**_find_out(hits), "searched": s, "next": self._next(self._absent(
            s, False, "Loosen the where filter." if listing else "Try other words or "
            "keepfold_recall (by meaning); if the user says it is new, keepfold_add it."))}

    def _listing(self, kind: str | None, keep, limit: int) -> list[dict]:
        """FW: with `where` and no query, the index pages (of `kind`) past `keep`, newest first
        by `_day_of` (undated last), then by id."""
        idx = self.index
        paths = {i: p for p, i in idx._ids.items()}
        rows = sorted(i for i, n in idx._notes.items()
                      if (kind is None or n["kind"] == kind) and keep(i))
        day = {i: self._day_of(i, idx._notes[i], paths.get(i)) for i in rows}
        rows.sort(key=day.__getitem__, reverse=True)  # stable: ties stay in id order
        cap = max(1, min(MAX_HITS, int(limit)))
        return [idx._hit(i, n["kind"], n["title"], _preview(n["body"]))
                for i in rows[:cap] for n in (idx._notes[i],)]

    def keepfold_recall(self, query: str, limit: int = 8, hops: int = 1, check: bool = False,
                      chats: bool = False, since: str | None = None, until: str | None = None,
                      source: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
        from keepfold import semantic  # numpy and the model load on first use, not at start

        if check:  # G1 Honest Double: check text written as the user against their levels
            sentences = [t for s in SENTENCE_RE.split(query or "") if (t := s.strip(" \t-*•"))]
            if len(query or "") > CHECK_CHARS or len(sentences) > CHECK_SENTENCES:
                raise invalid(f"check takes at most {CHECK_SENTENCES} sentences and "
                           f"{CHECK_CHARS:,} characters; check the text in parts.")
        self._refresh_index()
        keep = self._keep(since, until, source)
        # shortcut: a filter picks from recall's widest pool (MAX_LIMIT), so a match ranked
        # past it is missed; filter inside semantic._rank if that shows up
        hits, notice = semantic.search(self.index, query,
                                       limit if keep is None else semantic.MAX_LIMIT, hops,
                                       chats=chats, quiet=self._quiet())
        if keep is not None:
            hits = [h for h in hits if keep(h["id"])][:max(1, min(semantic.MAX_LIMIT,
                                                                  int(limit)))]
        if not check:  # a check is a draft to vet, not a search: no eval row
            self._note_recall("keepfold_recall", query, [h["id"] for h in hits])
        outside = session_mod.exposed_pages(self.log.iter_events()) if hits else set()
        lines = "\n".join(f"- [[{h['id']}]] {h['title']}"
                          + (" (from outside text)" if h["id"] in outside else "")
                          + (f" — {h['summary']}" if h["summary"] else "") for h in hits)
        out = {"hits": [{k: h[k] for k in ("id", "kind", "score", "match", "via") if k in h}
                        for h in hits],
               "text": _wrap("recall", lines) if hits else ""}
        if not check:  # RR-12: the evidence of the top hits, where in the page and what
            for h, row in zip(hits[:EVIDENCE], out["hits"], strict=False):
                n = self.index._notes[h["id"]]
                row["at"], text = semantic.passage(n, h.get("at", 0), query)
                row["passage"] = _wrap(h["id"], text)
                self._expose_on(h["id"], n["kind"], n["fields"])
        if notice:
            out["notice"] = notice
        # RR-14: weak when, with the model, no hit cleared the meaning floor (keyword or
        # linked hits only)
        weak = bool(hits) and not notice and all(h["match"] != "similar" for h in hits)
        if hits and not weak:
            out["next"] = self._next("Call keepfold_read on an id for details (with a hit's at= "
                                     "to read around its passage); cite pages as [[id]].")
        else:
            out["searched"] = self._searched((), keep, "keyword" if notice else
                                             "keyword + meaning", chats)
            out["next"] = self._next(self._absent(out["searched"], weak, "Try other words"
                                                  + ("." if chats else " or chats=true.")))
        if check:
            out["checks"] = self._checks(sentences)
            out["next"] = CHECK_NEXT
        xray.record(self.vault, "keepfold_recall", self.sessions.client,  # R27b: ids only
                    {"recalled": _hit_ids(hits)}, {}, len(str(out.get("text") or "")))
        return out

    def _checks(self, sentences: list[str]) -> list[dict]:
        """G1: per sentence, the top concept hit (recall's scoring and floors; private pages
        are not in the index) and a verdict from its state and applied projects."""
        from keepfold import semantic

        applied = {c for n in self.vault.iter_notes("project")
                   if n.fields.get("status") in ("active", "shipped") and not is_private(n)
                   for c in _edge_targets(n.fields.get("applies") or [])}
        out = []
        for s in sentences:
            hits = semantic.search(self.index, s, semantic.MAX_LIMIT, 0, rerank=False)[0]
            hit = next((h for h in hits if h["kind"] == "concept" and h["match"] == "similar"
                        and h["score"] >= CHECK_MIN), None)
            if hit is None:
                continue
            cid = hit["id"]
            st = self._state(self.index.states, cid)
            row = {"sentence": s, "concept": cid, "title": render.plain(hit["title"], 80),
                   "state": st}
            if st == "solid" or cid in applied:
                row["verdict"] = "ok"
                if cid in applied:
                    row["applied"] = True
            else:
                row["verdict"] = "hedge" if st in ("learning", "needs_refresh") else "unverified"
            out.append(row)
        return out

    def _expose_on(self, id: str, kind: str, fields: dict) -> None:
        """Outside text served from a page exposes the session: a resource or note whose
        `source` is a URL or raw/ path, a browse page (third-party titles, search terms) or an
        imported chat page (another assistant's text)."""
        from keepfold import semantic

        if (kind in ("resource", "note") and EXTERNAL_SOURCE.match(
                str(fields.get("source") or ""))) or kind == "browse" or (
                kind == "resource" and (n := self.index._notes.get(id)) and semantic.is_chat(n)):
            self.sessions.expose()

    def keepfold_read(self, id: str, sections: list[str] | None = None,
                    at: int | str | None = None, window: int | None = None,
                    ctx: Context | None = None) -> dict[str, Any]:
        if self._core_only():  # R12: no page, raw source or people page either
            return {"id": str(id), "text": None, "next": CORE_ONLY_NEXT}
        if str(id).startswith("raw/"):
            return self._read_raw(str(id))
        if str(id).startswith(people.PREFIX):  # V4: every people page, private or not
            p = people.get(self.vault, str(id))
            if p is None:
                raise VaultError("unknown_id", f"No people page '{id}'.")
            private = is_private(self.vault.read("person", p["id"][len(people.PREFIX):]))
            # R27b: the bare id, so the X-ray shows a private person as "a private page"
            xray.record(self.vault, "keepfold_read", self.sessions.client,
                        {"read": [p["id"][len(people.PREFIX):]]}, {}, len(p["body"]))
            return {"id": p["id"], "kind": "person", "title": render.plain(p["title"], 80),
                    "fields": _wrap_fields(p["id"], {"relation": p["relation"],
                                                      "tier": p["tier"]}),
                    "text": _wrap(p["id"], p["body"]),
                    "next": "Private: use it for this task only; never quote it elsewhere."
                    if private else "Cite it as [[" + p["id"] + "]] where it helps."}
        self._refresh_index()
        out = self.index.read(id, sections, at, window)
        self._note_open(out["id"])
        objs = {f"{k}:{out['id']}" for k in ("concept", "problem", "mistake", "assessment",
                                              "application", "area", "domain")}
        recent = [{k: ev[k] for k in ("day", "kind", "verdict", "item_type") if k in ev}
                  for ev in live(self.log.iter_events()) if ev.get("object") in objs]
        out["recent"] = recent[-5:]
        self._expose_on(out["id"], out["kind"], out["fields"])
        out["fields"] = _wrap_fields(out["id"], out["fields"])
        out["title"] = render.plain(out["title"], 120)  # outside <untrusted_note>
        if similar := self._similar(out["id"]):
            out["similar"] = similar
        on = (f" The page goes on: keepfold_read(id='{out['id']}', at={out['next_at']}) reads "
              "the next part, or at=<a heading listed at the end> jumps there."
              if "next_at" in out else "")
        out["next"] = self._next(("Ask one closed-book question on it (keepfold_ask) before "
                                  "teaching." if out["kind"] == "concept"  # dogfood r4 #13
                                  else "Use it as context; cite it as [[id]] where it helps.")
                                 + on)
        xray.record(self.vault, "keepfold_read", self.sessions.client,  # R27b: ids only
                    {"read": _hit_ids([out])}, {}, len(str(out.get("text") or "")))
        return out

    def _similar(self, id: str) -> list[dict]:
        """RR-23: up to READ_SIMILAR pages whose head vector is within semantic.MIN_COS of
        `id`'s, not linked to it either way, as {id, title (plain)}. Only vectors this process
        already holds: never loads or downloads the embedder, so [] until a recall built them.
        `_notes` holds no private, hidden or withheld page."""
        import numpy as np

        from keepfold import semantic

        notes = self.index._notes
        cache = semantic._MEM.get(self.vault.root, (None, {}, None))[1]
        heads = {i: e[3][e[2] == 0] for i, e in cache.items() if i in notes}
        mine = heads.pop(id, None)
        if mine is None or not len(mine):
            return []
        linked = {r[0] for r in self.index.db.execute(
            "SELECT dst FROM edges WHERE src = ?1 UNION SELECT src FROM edges WHERE dst = ?1",
            (id,))}
        ids = [i for i, v in heads.items() if len(v) and i not in linked]
        if not ids:
            return []
        cos = np.stack([heads[i][0] for i in ids]) @ mine[0]
        best = [ids[k] for k in np.argsort(-cos, kind="stable") if cos[k] >= semantic.MIN_COS]
        return [{"id": i, "title": render.plain(notes[i]["title"], 120)}
                for i in best[:READ_SIMILAR]]

    def _note_recall(self, tool: str, query: str, ids: list[str]) -> None:
        from keepfold import recall_log

        rid = recall_log.log_recall(self.vault, tool, query, ids)
        self._recalls = [*self._recalls[-19:], (time.monotonic(), rid, ids)]

    def _note_open(self, id: str) -> None:
        """A read within WINDOW_S of a recall in this process: an "opened" row linked to the
        latest recent recall that returned `id`, else to the latest recent one."""
        from keepfold import recall_log

        now = time.monotonic()
        recent = [r for r in self._recalls if now - r[0] <= recall_log.WINDOW_S]
        if recent:
            rid = next((r[1] for r in reversed(recent) if id in r[2]), recent[-1][1])
            recall_log.log_open(self.vault, rid, id)

    def _read_raw(self, rel: str) -> dict[str, Any]:
        p = self._raw_path(rel)
        private = any(is_private(n) and n.fields.get("source")
                      and _same_file(self.vault.root / str(n.fields["source"]), p)
                      for n in self.vault.iter_notes("resource"))
        if private or not p.is_file():  # a private resource's raw text looks like no file
            raise VaultError("unknown_id", f"{rel} does not exist; check the resource's "
                             "source field.")
        out = {"id": rel, "kind": "raw", "title": p.name, "size": p.stat().st_size}
        if p.suffix.lower() not in RAW_TEXT_SUFFIXES:
            # shortcut: metadata only for PDFs and other binaries; add text extraction if needed
            return {**out, "text": None, "next": "Only .md and .txt sources can be read; ask "
                    "the user to paste the parts that matter."}
        text = p.read_bytes().decode("utf-8", errors="replace")
        if len(text) > READ_CAP_CHARS:
            text = text[:READ_CAP_CHARS].rstrip() + TRUNCATED
        self.sessions.expose()
        return {**out, "text": _wrap(rel, text), "next": self._next(
            "Summarise it in the user's own words; cite the resource with [[id]].")}

    def keepfold_context(self, scope: Literal[
            "all", "coding", "communication", "learning", "projects", "environment", "career",
            "personal"] = "all", topic: str | None = None,
            ctx: Context | None = None) -> dict[str, Any]:
        parts: list[str] = []
        left = [CONTEXT_MAX]
        put = prime.putter(parts, left)
        hide = "profile" in self._withheld()
        claims_served = self._served_claims()
        summary = "" if hide else prime.profile_summary(self.vault, local_day(self._now()),
                                                        claims_served, self._held())
        put("Profile", summary or (WITHHELD.format(n=1) if hide
                                   else f"me/profile.md: {NOT_RECORDED}"),
            "me/profile" if summary else None, CONTEXT_CAPS["profile"])
        mute = xray.settings(self.vault)["mute"]
        served: dict = {"profile": ["me/profile"] if summary else []}
        trimmed: dict = {}
        if topic:
            self._refresh_index()
            from keepfold import semantic  # numpy and the model load only when a topic is asked

            embed = semantic.load_embedder()
            hits = semantic.recall(self.index, topic, embed, 5, 1, semantic.load_reranker(),
                                   quiet=self._quiet()) if embed else self.index.related(topic, 5)
            self._note_recall("keepfold_context", topic, [h["id"] for h in hits])
            hits = [h for h in hits if h["id"] not in mute]  # the owner's X-ray mutes
            # Topic concepts come from the recall hits; an all-words match misses phrases
            concepts = [h for h in hits if h["kind"] == "concept"][:3] if embed else [
                h for h in self.index.find(topic, "concept", 3)
                or self.index.find(topic, "concept", 3, any_word=True) if h["id"] not in mute]
            served["topic"] = [h["id"] for h in concepts]
            lines = []
            hide = self._hidden()
            for h in concepts:
                st = self.index.states.get(h["id"])
                due, note = getattr(st, "next_due", None), levels.gap_note(st)
                mistakes = [str(m) for m in getattr(st, "open_mistakes", None) or []
                            if self._sees(str(m), hide)]
                lines.append(f"- {GLYPHS[self.index._state(h['id'])]} "
                             f"{render.esc(h['title'])} ({h['id']})"
                             + (f" · next due {due}" if due else "")
                             + (f" · {note}" if note else "")
                             + (f" · open mistakes: {', '.join(mistakes)}" if mistakes else ""))
            put(f"Topic: {render.esc(topic)}", "\n".join(lines) or "No matching concepts.",
                cap=CONTEXT_CAPS["topic"])
            rel = [f"- [[{h['id']}]] — {h.get('summary') or h['title']}" for h in hits]
            while rel and len(_wrap("related", "\n".join(rel))) + 12 > min(
                    CONTEXT_CAPS["related"], left[0]):  # 12: the "## Related\n" head
                rel.pop()  # trim whole lines, last (least relevant) first
            if rel:
                put("Related", "\n".join(rel), "related", CONTEXT_CAPS["related"])
            ids = [h["id"] for h in hits]
            served["related"], trimmed["related"] = ids[:len(rel)], ids[len(rel):]
        if scope in ("all", "projects") and access.scope_kind(self._scopes(), "project"):
            lines = [f"- {n.fields.get('title') or n.id}"
                     + (f" — {n.fields['summary']}" if n.fields.get("summary") else "")
                     for n in self.vault.iter_notes("project")
                     if n.fields.get("status") == "active" and not is_private(n)]
            put("Active projects", "\n".join(lines) or "none", "projects" if lines else None,
                CONTEXT_CAPS["projects"])
        got: list = []
        self._personality_prefs(scope, put, served=claims_served, asks=" (ask)" in summary,
                                lines=got)
        context = "\n\n".join(parts)
        for cid, line in got:  # a claim line is served if it survived the char budget whole
            (served if _neutralise(line) + "\n" in context else trimmed).setdefault(
                "claims", []).append(cid)
        xray.record(self.vault, "keepfold_context", self.sessions.client, served, trimmed,
                    len(context))
        out: dict[str, Any] = {"context": context}
        lead = ""
        if topic and (not hits or embed and all(h["match"] != "similar" for h in hits)):
            out["searched"] = self._searched((), None, "keyword + meaning" if embed
                                             else "keyword", False)  # RR-14
            lead = self._absent(out["searched"], bool(hits), "")
        out["next"] = self._next(
            lead + "Use this to tailor your answer; save durable new facts with keepfold_patch."
            + (self._offer([h["id"] for h in concepts]) if topic else ""))
        return out

    def keepfold_chats(self, action: Literal["list", "read", "harvest"] = "list",
                     id: str | None = None, since: str | None = None, limit: int = 20,
                     part: int = 1, unlearned: bool = False, who: Literal["all", "user"] = "all",
                     ctx: Context | None = None) -> dict[str, Any]:
        if action == "harvest":
            return self._harvest(id)
        if action == "list":
            saved = {c for _, c in chats.saved_ids(self.log.iter_events())}
            found = chats.list_chats(saved, since, limit, self._learned(), unlearned)
            return {"chats": found, "next": "Pick a chat not yet saved and read it with "
                    "action=read." if found else "No past chats found."}
        if not id:
            raise invalid("read needs id from keepfold_chats list.")
        parts = chats.read_parts(id, who)
        if not 1 <= part <= len(parts):
            raise invalid(f"part must be from 1 to {len(parts)}.")
        more = f"Read part {part + 1} of {len(parts)} next. " if part < len(parts) else ""
        if who != "user":  # the user's own typed turns are not outside text; assistant turns
            self.sessions.expose()  # carry tool output and web pages, so they expose the session
        return {"id": id, "part": part, "parts": len(parts),
                "text": _wrap(f"chat:{id}", parts[part - 1]),
                "next": more + ("Propose what this shows about the user as about-me lines "
                                "(keepfold_patch, never user_confirmed); then "
                                "keepfold_log(kind=\"chat_learned\", …)" if who == "user" else
                                "Harvest durable items with "
                                "keepfold_add/keepfold_patch/keepfold_log; "
                                "propose personal traits for confirmation; then "
                                "keepfold_log(kind=\"chat_saved\", …)")}

    def _harvest(self, within: str | None) -> dict[str, Any]:
        """The next bundle of imported chats (harvest.bundle). It never exposes: the text is
        the user's own typed turns. Its ids arm the harvest guards (sessions.served)."""
        from keepfold import harvest, semantic, topics

        embed = semantic.load_embedder()  # never downloads
        if embed is None:
            raise invalid(semantic.NOT_SET_UP)
        self._refresh_index()
        t = topics.tree(self.index)
        if within is not None and within not in t.parent:
            raise invalid("id must be a topic hub or area id, or left out for the queue's head.")
        done, skipped, _ = harvest.fold(self.log.iter_events())
        b = harvest.bundle(self.index, t, topics.head_stack(self.index, embed), done, skipped,
                           within, self._now().isoformat())
        if b is None:
            return {"bundle": None, "next": "Nothing is left to harvest"
                    + (" under this hub." if within else "; tell the user, then keepfold_end.")}
        if self.sessions.current is None:  # a read opens no session; the guards need one
            self.session = self.sessions.start("history")
            self._reset()
        if not self.sessions.harvesting:  # a new harvest: earlier proposals are not its
            self.proposed.clear()
        self.sessions.serve(i for c in b["chats"] for i in (c["id"], *c["dupes"]))
        return {**b, "budget": {"about_me": self._about_me_budget()}, "next": HARVEST_NEXT}

    def _resurface(self, limit: int) -> dict[str, Any]:
        rows = self._resurface_rows(limit)
        return {"items": rows, "next": "Show at most one or two, naturally. If you show them, "
                "record it with keepfold_log(kind='resurfaced', payload={ids: [...]})."
                if rows else "Nothing is due to resurface."}
