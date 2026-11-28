"""The dashboard: the app's live home (`render_live`) and the offline snapshot.

The snapshot (Export snapshot) is one self-contained page: the data blob, d3 v7 and the page
script are inlined, and the CSP allows only those scripts by hash. Note text reaches the page as
plain strings (links, images and tags stripped) and the page script inserts it with
`textContent`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import operator
import re
import statistics
import string
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from keepfold import (
    brainmap,
    chat_pages,
    claims,
    index,
    know,
    learning_eval,
    levels,
    loops,
    planner,
    profiles,
    reflect,
    taint,
    topics,
)
from keepfold import vault as vault_mod
from keepfold.catalog import Catalog
from keepfold.events import EventLog, local_day
from keepfold.index import _edge_targets, link_targets
from keepfold.render import index_md, plain
from keepfold.vault import TOOL_PROPS, Vault, is_private

ASSETS = Path(__file__).parent / "assets"
YOU = "@you"  # not a valid note id, so it never collides with one
EVIDENCE_SHOWN, WEEKS, MISTAKE_DAYS = 10, 8, 30
# answer time, display only (never a rating): the median of the last 10 ask-to-grade gaps, from
# 3 gaps, leaving out gaps over 15 minutes (the person left)
ANSWER_LAST, ANSWER_MIN, ANSWER_CUT = 10, 3, 15 * 60
ITEM_TYPE = {"problem_status": "problem", "self_report": "self report"}
_SLOT = re.compile(r"\{\{(\w+)\}\}")
BODY_MAX = 20 * 1024
TRUNCATED = "\n\n*… truncated here; open the note in Obsidian for the rest.*\n"
SHOWN_PROPS = 30  # list items and characters below are capped too, to keep the blob small
REPO = Path(__file__).resolve().parents[2]  # a source checkout; installed, assets/help instead
GLOSSARY = "The names, in plain words"  # the GUIDE heading after its number
_NAME_RE = re.compile(r"\*\*Name:\*\*\s*(\S+)")
# an import's header (doctor._IMPORT_HEAD): `Source: <label> · Date:`, maybe on a Timeline line
_IMPORTED = re.compile(r"^(?:Timeline: [^\n]*?)?Source: ([^·\n]+?) · Date: ", re.M)
UNFILED = "unfiled"
GROUP_TITLES = {"journal": "Journal", "you": "About you", "sessions": "Study sessions",
                "placements": "Tests and applications",
                "projects": "Projects, decisions and loops", "people": "People",
                "media": "YouTube and NotebookLM", "chats": "Unsorted chats",
                "browsing": "Browsing"}
GROUPS = {"journal": "journal", "me": "you", "session": "sessions",  # page kind -> group lobe
          "browse": "browsing",  # private browse pages are never read
          "rollup": "journal",  # generated week, month and year pages: time, like the journal
          **dict.fromkeys(("assessment", "application", "track"), "placements")}
NO_HUB = {"project": "projects", "decision": "projects", "loop": "projects", "person": "people"}
MEDIA = {"youtube", "notebooklm"}
CHATS = {"claude", "claude code", "chatgpt", "gemini", "perplexity"}  # page_source, lowercased


_SESSION_ID = re.compile(r"(\d{4}-\d{2}-\d{2})-([\w-]+)-\w{8}")
_STARTED = re.compile(r"^- Started: \d{4}-\d{2}-\d{2} (\d{1,2}:\d{2})\s*$", re.M)


def _is_ambient(kind: str, nid: str) -> bool:
    """A receipt session.py saved when an AI chat wrote to the vault (`<day>-ambient-<8>`)
    or when Keepfold tidied the about-me lines (`<day>-tidy-<8>`) or saved some from imported
    chats (`<day>-harvest-<8>`)."""
    m = _SESSION_ID.fullmatch(nid)
    return kind == "session" and bool(m) and m[2] in ("ambient", "tidy", "harvest")


def _title(note) -> str:
    if note.kind == "me" and not note.fields.get("title"):  # me/teaching-contract -> words
        return note.id.replace("-", " ").capitalize()
    if note.kind == "session" and not note.fields.get("title") and (
            m := _SESSION_ID.fullmatch(note.id)):  # a receipt has no title: say what it was
        t = _STARTED.search(note.body)
        when = t[1] if t else m[1]
        return (f"Chat with an AI app, {when}" if m[2] == "ambient"
                else f"Keepfold tidied lines about you, {when}" if m[2] == "tidy"
                else f"Keepfold saved lines about you from your chats, {when}"
                if m[2] == "harvest"
                else f"Study session ({m[2]}), {when}")
    return plain(note.fields.get("title") or note.id) or note.id


def _plans(vault, catalog, hidden, pc) -> list[dict]:
    """Each non-private plan without dates (v8): its title, next undone steps and counts."""
    return [{"id": p["id"], "title": plain(p["title"]) or p["id"],
             "next": [plain(s["title"], 200) for s in p["steps"][:planner.STEPS_SHOWN]],
             "done": p["done"], "left": len(p["steps"])}
            for p in planner.plans(vault, catalog, hidden, pc)]


STUDY_KINDS = ("concept", "track", "assessment")
SUGGEST_DAYS, SUGGESTED = 60, 3
_EXAM_WORD = re.compile(rf"\b{chat_pages._EVENT}\b", re.I)
_DEADLINE_WORD = re.compile(r"\bdeadlines?\b", re.I)


def exam_suggestions(notes: dict, today: date, shut: set) -> list[dict]:
    """R31: up to SUGGESTED imported calendar events (`calendar-*` month pages' lines, as
    chat_pages reads them) in the next SUGGEST_DAYS days whose title looks like an exam or a
    deadline (chat_pages._EVENT) and that no assessment page holds on the same day by the same
    name (case-folded), soonest first: {day, title, kind (exam or deadline), page}. A fold over
    the pages, never stored; a page in `shut` (private, or under a private subject) is never
    read, and an added one goes because its assessment page now exists."""
    have = {(str(n.fields.get("date") or "")[:10], str(n.fields.get("title") or "").casefold())
            for n in notes["assessment"]}
    lo, hi, out = today.isoformat(), (today + timedelta(days=SUGGEST_DAYS)).isoformat(), {}
    for n in notes["resource"]:
        if not n.id.startswith("calendar-") or n.id in shut or is_private(n):
            continue
        for ln in n.body.splitlines():
            if not (m := chat_pages._CAL_LINE.fullmatch(ln.strip())) or not lo <= m[1] <= hi:
                continue
            title = plain(m[2].split(" · ")[0], 120)
            key = (m[1], title.casefold())
            if title and _EXAM_WORD.search(title) and key not in have and key not in out:
                out[key] = {"day": m[1], "title": title, "page": n.id,
                            "kind": "deadline" if _DEADLINE_WORD.search(title) else "exam"}
    return sorted(out.values(), key=lambda r: (r["day"], r["title"]))[:SUGGESTED]


def studies(vault: Vault, events: list[dict], notes: dict | None = None) -> bool:
    """Whether the person studies: any concept, track (study plan) or assessment page, private
    ones too, or any grade not voided (a removed sample's seed grades are voided). A fold over
    pages and events, never stored. `notes`, build_data's parsed notes by kind; without them
    the kind folders are only listed."""
    if any(notes[k] if notes is not None else vault.note_paths(k) for k in STUDY_KINDS):
        return True
    voided = {e.get("ref") for e in events if e.get("kind") == "void"}
    return any(e.get("kind") == "grade" and e.get("id") not in voided for e in events)


# UX-E: Home's boards by the signal they serve (dashboard.js homeGroups)
HOME_GROUPS = ("study", "build", "research")
RECENT_DAYS = 30  # a page or event this recent counts twice
BUILD_KINDS = ("project", "decision", "loop")  # the "Projects, decisions and loops" lobe
STUDY_EVENTS = ("taught", "ask", "grade")
CODING_LABELS = ("Claude Code", "Git")  # `Source:` labels of coding apps' chats and repo pages
# MCP client names of coding apps, normalised (profiles.norm), as profiles.DEFAULT matches them
CODING_CLIENTS = ("claudecode", "cursor", "codex", "visualstudiocode", "copilot", "geminicli",
                  "windsurf", "antigravity", "cline", "opencode", "jetbrains")
# a resource saved by hand (no label: a link, a paper) or from Zotero, documents, highlights or
# notes; not calendar, mail, photos, chats and the like
RESEARCH_LABELS = ("", "Zotero", "Documents", "Highlights", "Pocket", "Obsidian", "Notion",
                   "Notes")
NEW_PAGES = 10  # Get started: a vault with fewer pages of its own is new
# page id -> (its row's body, its import `Source:` label): a build reuses an unchanged page's
# row and body, so `signals` scans each body once (the scan cost ~8 ms on 5k pages every build)
_LABELS: dict[str, tuple[str, str]] = {}


def _label(p: dict) -> str:
    body = p["body"]
    hit = _LABELS.get(p["id"])
    if hit is not None and hit[0] is body:  # the same string object: the page did not change
        return hit[1]
    if len(_LABELS) > 100_000:  # many vaults in one process (tests): start again
        _LABELS.clear()
    x = ("Source: " in body and (m := _IMPORTED.search(body)) and m[1]) or ""
    _LABELS[p["id"]] = (body, x)
    return x


def signals(pages: list[dict], events: list[dict], day: str, study: bool) -> dict:
    """How much of the vault serves study, build and research: pages and events, those of the
    last RECENT_DAYS days counted twice. Study: concept, track and assessment pages and taught,
    ask, grade and self-report events, 0 for someone who does not study (`studies`). Build:
    project, decision and loop pages, coding apps' chats and repo pages, chats opened from a
    coding app. Research: resources saved by hand or from a research source, and notes that
    link one. A fold over the page rows and events, never stored."""
    cut = (date.fromisoformat(day) - timedelta(days=RECENT_DAYS)).isoformat()
    out = dict.fromkeys(HOME_GROUPS, 0)
    by_kind = {**dict.fromkeys(STUDY_KINDS, "study"), **dict.fromkeys(BUILD_KINDS, "build")}
    found, notes = set(), []  # research resources' ids; notes that may link one (counted after)
    for p in pages:  # one plain pass: it runs over every page on each build
        g = by_kind.get(k := p["kind"])
        if g is None and ((res := k == "resource") or p["source"]):
            x = p["source"] or _label(p)  # a resource or chat page's `Source:` label
            if x in CODING_LABELS:
                g = "build"
            elif res and x in RESEARCH_LABELS:
                g = "research"
                found.add(p["id"])
        if g is None:
            if k == "note" and p["links"]:
                notes.append(p)
            continue
        out[g] += 2 if (p["date"] or p["updated"]) > cut else 1  # both ISO day strings
    for p in notes if found else ():
        if not found.isdisjoint(p["links"]):
            out["research"] += 2 if (p["date"] or p["updated"]) > cut else 1
    coding: dict[str, bool] = {}  # client name -> a coding app's, asked once per name
    for e in events:
        if (k := e.get("kind")) in STUDY_EVENTS:
            g = "study"
        elif k in ("log", "open") and isinstance(pl := e.get("payload"), dict):
            if k == "log" and pl.get("kind") == "self_report":
                g = "study"
            elif k == "open" and isinstance(c := pl.get("client"), str) and (
                    coding[c] if c in coding else coding.setdefault(
                        c, any(x in profiles.norm(c) for x in CODING_CLIENTS))):
                g = "build"
            else:
                continue
        else:
            continue
        out[g] += 2 if str(e.get("day") or "") > cut else 1
    if not study:
        out["study"] = 0
    return out


def get_started(pages: list[dict], known: dict) -> dict | None:
    """A new vault's first steps, or None: fewer than NEW_PAGES pages besides me/, rollups
    and Keepfold's session receipts. `fact`: a live line on a me/ page; `imported`: any page an
    import or an AI app's chat import wrote. Derived on each build, never stored; the app adds
    whether an AI app is connected."""
    own = 0
    for p in pages:  # stops at NEW_PAGES: a big vault costs nothing here
        if p["kind"] not in ("me", "rollup") and not (
                p["kind"] == "session" and _is_ambient(p["kind"], p["id"])):
            own += 1
            if own >= NEW_PAGES:
                return None
    return {"fact": known["total"]["live"] > 0, "imported": any(map(_imported, pages))}


def home_layout(cfg: dict | None = None) -> list[str] | None:
    """`app.home_layout` in config.yaml: Home's pinned group order, or None (automatic)."""
    a = (taint.config() if cfg is None else cfg).get("app")
    v = a.get("home_layout") if isinstance(a, dict) else None
    ok = isinstance(v, list) and 0 < len(v) == len(set(v)) and all(x in HOME_GROUPS for x in v)
    return v if ok else None


def build_data(vault: Vault, *, now: datetime, relayout: bool = False,
               auto_relayout: bool = True, memo: dict | None = None) -> dict:
    """The dashboard blob; private notes never enter it.
    `relayout` lays the brain out from scratch (brainmap); `auto_relayout=False` never does it
    by itself. `memo`, a dict the caller keeps between builds (the app's home), reuses the pages
    and the brain while their notes are the same objects, and receives the build's pieces
    under "parts" for the home's own sections."""
    catalog = Catalog.load(vault)
    day = local_day(now)
    today_d = date.fromisoformat(day)
    events = list(EventLog(vault).iter_events())
    notes = {k: list(vault.iter_notes(k)) for k in Vault.KIND_DIRS}  # parsing dominates; once
    pc = levels.problem_concepts(vault)
    if memo is not None and memo.get("events") != events:  # a generation per event log content
        memo["events"], memo["gen"] = events, memo.get("gen", 0) + 1
    gen = memo["gen"] if memo is not None else None
    exam = planner.exam_concepts(vault, catalog, day)
    states, mstates = _once(memo, "fold", (gen, day, pc, exam),
                            lambda: levels.fold(events, day, pc, exam))
    private = {n.id for ns in notes.values() for n in ns if is_private(n)}
    hidden = planner.hidden(vault, catalog, private)
    private |= planner.hidden_mistakes(mstates, hidden)  # OQ-22: mistakes about private subjects
    entries = [e for e in catalog.entries.values() if e.id not in hidden]
    concepts = [e for e in entries if e.kind == "concept"]
    visible = {e.id for e in concepts}
    shown = {e.id for e in entries}

    def state(c):
        return states[c].state if c in states else "not_started"

    tally = Counter(state(e.id) for e in concepts)
    counts = {s: tally[s] for s in ("solid", "learning", "needs_refresh", "unverified",
                                    "not_started")}

    items = [i for i in planner.today(vault, catalog, states, now=now, events=events)
             if i["id"] not in hidden]
    today = [{"kind": i["kind"], "id": i["id"], "title": plain(i["title"]),
              "state": i["state"], "why": plain(i["why"])} for i in items]

    scores = {n.id: n.fields.get("score") for n in notes["assessment"]}
    titles = {n.id: plain(str(n.fields.get("title") or "")) for n in notes["assessment"]}
    pairs, calibrated = _once(memo, "calibrated", (gen, pc),
                              lambda: learning_eval.calibrated(events, pc))

    def assessment(a):
        d = planner._day(a["date"]) if a["date"] else None
        tested = [c for c in planner._weights(catalog, a["tests"]) if c in visible]
        doms = {catalog.entries[c].domain for c in tested}
        score = scores.get(a["id"])
        share = planner.readiness(vault, catalog, states, a)
        return {"id": a["id"], "company": plain(a["company"] or "") or None,
                "title": titles.get(a["id"]) or None,
                "kind": plain(a["kind"] or "") or None, "date": d.isoformat() if d else None,
                "days_left": (d - today_d).days if d else None,
                "outcome": a["outcome"] or "pending",
                "score": plain(str(score)) if score is not None else None,
                "readiness": {"solid": sum(state(c) == "solid" for c in tested),
                              "total": len(tested),
                              # `weighted` names the weighted share; `share` stays for
                              # old readers
                              "share": share, "weighted": share,
                              # a forecast beside the Solid share, only for a day still ahead
                              # and once calibrated, as on the home card; until then, why not
                              "forecast": planner.recall_on_day(vault, catalog, states, a, now)
                              if d and d >= today_d and calibrated else None,
                              "calibrating": None if calibrated or not d or d < today_d
                              else {"n": pairs, "need": learning_eval.MIN_PAIRS}},
                "domains": sorted(catalog.entries[x].title for x in doms if x in catalog.entries)}

    rows = [assessment(a) for a in planner.assessments(vault) if a["id"] not in private]
    coming = [a for a in rows if a["outcome"] == "pending"
              and (a["days_left"] is None or a["days_left"] >= 0)]
    # a plan's real deadline sits beside the exams (plan steps themselves have no dates)
    coming += [{"id": d["id"], "company": plain(d["title"]) or d["id"], "kind": "deadline",
                "date": d["date"], "days_left": d["days"], "outcome": "pending", "score": None,
                "readiness": {"solid": 0, "total": 0, "share": None, "weighted": None,
                              "forecast": None, "calibrating": None}, "domains": []}
               for d in planner.deadlines(vault, today_d)]
    coming.sort(key=lambda a: (a["days_left"] is None, a["days_left"] or 0))

    mtitle = {n.id: _title(n) for n in notes["mistake"]}
    mistakes = sorted(({"id": m.id, "title": mtitle.get(m.id, m.id), "count": m.count,
                        "last_day": m.last_day, "status": m.status}
                       for m in mstates.values() if m.id not in private),
                      key=lambda m: (m["status"] != "open", -_ord(m["last_day"]), m["id"]))

    # graph
    nodes = [{"id": YOU, "kind": "you", "title": "You", "state": None, "domain": None,
              "area": None, "degree": 0, "open_mistakes": 0, "next_due": None}]
    links = []
    for e in entries:
        st = states.get(e.id)
        nodes.append({"id": e.id, "kind": e.kind, "title": plain(e.title) or e.id,
                      "state": state(e.id) if e.kind == "concept" else None,
                      "domain": e.domain, "area": e.parent if e.kind == "concept" else
                      e.id if e.kind == "area" else None, "degree": 0,
                      "open_mistakes": len([m for m in st.open_mistakes if m not in private])
                      if st else 0, "next_due": st.next_due if st else None})
        links.append({"s": YOU if e.kind == "domain" else e.parent, "t": e.id,
                      "type": "you" if e.kind == "domain" else "hier"})
        links += [{"s": e.id, "t": r, "type": "requires"} for r in e.requires
                  if r in visible and r != e.id]
        # extra areas are thin non-tree links; the parent edge above stays the tree
        links += [{"s": a, "t": e.id, "type": "area"} for a in e.areas if a in shown]
    degree = Counter([lk["s"] for lk in links] + [lk["t"] for lk in links])
    for n in nodes:
        n["degree"] = degree[n["id"]]

    # concept details
    def evidence():
        asks = {e["id"]: e for e in events if e.get("kind") == "ask"}
        trail: dict[str, list[dict]] = {}
        answers = Counter()
        took: dict[str, list[float]] = {}  # concept -> seconds from each ask to its grade
        for it in levels._items(events, pc):  # void and remap applied, as in the levels fold
            ev = it.ev
            if ev.get("kind") == "grade" and it.concept not in hidden:
                answers[ev.get("day")] += 1
                if it.concept in visible and ev.get("ref") in asks:
                    try:  # a hand-edited stamp skips this gap, never the dashboard
                        took.setdefault(it.concept, []).append(
                            (datetime.fromisoformat(ev["ts"])
                             - datetime.fromisoformat(asks[ev["ref"]]["ts"])).total_seconds())
                    except (KeyError, TypeError, ValueError):
                        pass
            if it.concept in visible and it.verdict is not None:
                payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
                kind = (ev.get("item_type") or asks.get(ev.get("ref"), {}).get("item_type")
                        or ITEM_TYPE.get(payload.get("kind")))
                trail.setdefault(it.concept, []).append({
                    "day": ev.get("day"), "verdict": it.verdict,
                    "item_type": plain(kind) if kind else None,
                    "source": plain(ev.get("source") or "") or None})
        return trail, answers, {c: n for c, xs in took.items()
                                if (n := _answer_secs(xs)) is not None}

    trail, answers, answer_secs = _once(memo, "evidence", (gen, pc, hidden, visible), evidence)
    study = studies(vault, events, notes)
    leeches = sorted(({"id": e.id, "title": plain(e.title) or e.id, "lapses": states[e.id].lapses}
                      for e in concepts if study and levels.is_leech(states.get(e.id))),
                     key=lambda r: (-r["lapses"], r["id"]))
    leech_ids = {r["id"] for r in leeches}
    required_by: dict[str, list[str]] = {}
    for lk in links:
        if lk["type"] == "requires":
            required_by.setdefault(lk["t"], []).append(lk["s"])
    problems: dict[str, list[dict]] = {}
    for p in notes["problem"]:
        if p.id in private:
            continue
        row = {"id": p.id, "title": _title(p), "status": plain(p.fields.get("status") or "")
               or None}
        for c in dict.fromkeys(pc.get(p.id, [])):
            if c in visible:
                problems.setdefault(c, []).append(row)
    details = {}
    for e in concepts:
        st = states.get(e.id)
        d = {"evidence": trail.get(e.id, [])[-EVIDENCE_SHOWN:][::-1],
             "requires": [r for r in e.requires if r in visible and r != e.id],
             "required_by": required_by.get(e.id, []),
             "problems": problems.get(e.id, []),
             "mistakes": [m for m in st.open_mistakes if m not in private] if st else []}
        if e.id in leech_ids:
            d["leech"] = planner.leech_why(st)
        if e.id in answer_secs:
            d["answer_secs"] = answer_secs[e.id]
        if any(d.values()):
            details[e.id] = d

    # history
    monday = today_d - timedelta(days=today_d.weekday())
    weeks = []
    for i in range(WEEKS - 1, -1, -1):
        start = monday - timedelta(weeks=i)
        week = {(start + timedelta(days=j)).isoformat() for j in range(7)}
        weeks.append({"start": start.isoformat(),
                      "answers": sum(n for d, n in answers.items() if d in week)})
    cut = (today_d - timedelta(days=MISTAKE_DAYS)).isoformat()
    old = [e for e in events if str(e.get("day", "")) <= cut]
    exam_cut = planner.exam_concepts(vault, catalog, cut)
    _, before = _once(memo, "before", (old, cut, pc, exam_cut),
                      lambda: levels.fold(old, cut, pc, exam_cut))

    def moved(status):
        return sum(1 for m in mstates.values() if m.id not in private and m.status == status
                   and getattr(before.get(m.id), "status", None) != status)

    pages, clusters = _pages(vault, notes, hidden, catalog, pc, memo)
    if memo is not None:
        memo["parts"] = {"notes": notes, "events": events, "states": states, "hidden": hidden,
                         "private": private, "catalog": catalog, "items": items}
    parsed = _Parsed(vault.root, notes)  # loops and know read notes through this: parsed once
    created = _created(memo)
    loop_notes = {n.id: n for n in notes["loop"]}
    # it reads only the events, the me notes and the claim pages' text (even one that won't parse)
    claimed = [p.read_bytes() if p.is_file() else None
               for p in (vault.root / "me" / f"{c}.md" for c in claims.PAGES)]
    known = _once(memo, "know", (gen, day, notes["me"], claimed),
                  lambda: know.report(parsed, events, day))
    profile = next((n for n in notes["me"] if n.id == "profile" and not is_private(n)), None)
    m = _NAME_RE.search(profile.body) if profile else None
    name = plain(m.group(1).strip(string.punctuation)) if m else ""

    return {
        "as_of": now.astimezone().isoformat(timespec="seconds"),
        "day": day,  # the 04:00 Keepfold day, which the calendar date in `as_of` is not
        "plans": _plans(vault, catalog, hidden, pc),
        "counts": counts,
        "today": today,
        # an empty plan is "done" only after work today; never graded at all is a first run
        "plan_cleared": not today and any(e.get("day") == day for e in events),
        "first_run": not today and not any(e.get("kind") == "grade" for e in events),
        # study features (the Study tab, exams, Tomorrow) show only for someone who studies
        "studies": study,
        # UX-E: what Home's boards are ordered by, and a new vault's Get started
        "signals": signals(pages, events, day, study),
        "get_started": get_started(pages, known),
        # concepts forgotten LEECH_LAPSES+ times after they were learned, most lapses first
        "leeches": leeches,
        # any imported page yet (chats, notes, calendar, browsing), which ends the everyone
        # Start here; only someone who does not study is asked (a body scan: ~20 µs a page)
        "imported": not study and any(map(_imported, pages)),
        "assessments": coming,
        # R31: calendar events that look like exams, offered on Home's board to add as one
        "exam_suggestions": exam_suggestions(notes, today_d, private | hidden),
        "mistakes": mistakes,
        "graph": {"nodes": nodes, "links": links},
        "vault_name": vault.root.name,
        "pages": pages,
        "details": details,
        "history": {"weeks": weeks, "assessments": rows,
                    "mistakes": {"opened": moved("open"), "resolved": moved("resolved")}},
        # how often each stated confidence (1-4) was right; display-only
        "confidence": _once(memo, "mirror", (gen, pc, hidden),
                            lambda: learning_eval.confidence_mirror(events, pc, hidden)),
        "owner": {"name": name} if name else None,
        "clusters": clusters,
        "brain": brainmap.build(vault, pages, clusters, relayout=relayout,
                                auto_relayout=auto_relayout,
                                memo=None if memo is None else memo.setdefault("brain", {})),
        "counts_by_kind": dict(Counter(p["kind"] for p in pages)),
        # pages the owner worked on today, for the brain's Today (the feed adds the events)
        "today_ids": _today_ids(pages, notes["note"], day),
        "loops": [_plained(r, "title", "summary", "loop_type")
                  | {"created": created(loop_notes[r["id"]]).isoformat()}
                  for r in loops.loops(parsed)],
        "follow_ups": [_plained(r, "title") for r in loops.follow_ups(parsed, day)],
        "resurface": [_plained(r, "title", "summary")
                      for r in loops.resurface(parsed, day, limit=5, events=events,
                                               created_of=created)],
        "you": {"know": known, "unconfirmed": reflect.unconfirmed(vault, day)[0],
                # R27a Mind Mirror: replaced lines and decisions, a fold (nothing stored)
                "changes": changes(notes, events, private | hidden, day)},
        "help": _help(),
    }


def _claim_rows(notes: dict, shut: set) -> tuple[dict, set]:
    """({(page, claim id): row} of the live and past claims of every shown me/ page, the claim
    ids of the private or hidden ones)."""
    rows, closed = {}, set()
    for n in notes["me"]:
        got = claims.parse(n.id, n.body)
        if n.id in shut or is_private(n):
            closed |= {r["id"] for r in got}
            continue
        rows |= {(n.id, r["id"]): r for r in got if r["status"] in ("live", "past")}
    return rows, closed


def _chain(start, nxt: dict) -> list:
    out, seen = [], set()
    while start is not None and start not in seen:
        seen.add(start)
        out.append(start)
        start = nxt.get(start)
    return out


def changes(notes: dict, events: list, shut: set, today: str) -> list[dict]:
    """R27a Mind Mirror: each chain of versions, newest change first: {kind: line|decision,
    page, versions: [{id, page, text, from, until, open_start, status}]}, oldest version first.
    A line chain is a me/ claim and every line its `## Past` line points to (`→ ^id`, on its own
    page first, else on another); a decision chain is a decision and the ones that replaced it
    (chat_pages.replaced_by). Only chains with a past or retired version. A line starts on its
    `(since …)` day, else the day of the first change event that wrote its id, else when the
    one before it ended, else at its page's earliest known day (`open_start`). A pure fold over
    pages and events; pages in `shut` and private pages never show, and a line that went on to
    one ends as past, never retired."""
    voided = {e.get("ref") for e in events if e.get("kind") == "void"}
    wrote, first = {}, {}  # (page, claim id) -> first day written; page -> earliest day seen
    for e in events:
        pl = e.get("payload") if e.get("kind") == "change" else None
        c = pl.get("change") if isinstance(pl, dict) else None
        day = str(e.get("day") or "")
        if not isinstance(c, dict) or e.get("id") in voided or not day:
            continue
        page = str(c.get("id"))
        first[page] = min(first.get(page, day), day)
        for cid in c["claim"] if isinstance(c.get("claim"), list) else [c.get("claim")]:
            if isinstance(cid, str):
                wrote[(page, cid)] = min(wrote.get((page, cid), day), day)
    rows, closed = _claim_rows(notes, shut)
    by_id, link, nxt = {}, {}, {}
    for k in rows:
        by_id.setdefault(k[1], k)
    for k, r in rows.items():
        if r["status"] == "past":
            link[k] = until, to = claims.past_link(r["text"])
            t = (k[0], to) if (k[0], to) in rows else by_id.get(to)
            if to and t:
                nxt[k] = t
        for d in (claims.since(r["text"]), link.get(k, (None,))[0]):
            if d:
                first[k[0]] = min(first.get(k[0], d), d)
    out = []
    for head in rows.keys() - set(nxt.values()):
        keys = _chain(head, nxt)
        if len(keys) == 1 and rows[head]["status"] == "live":
            continue
        versions, prev = [], None
        for n, k in enumerate(keys):
            r, (until, to) = rows[k], link.get(k, (None, None))
            start = claims.since(r["text"]) or wrote.get(k) or prev
            status = ("live" if r["status"] == "live" else "past" if n < len(keys) - 1
                      or to in closed else "retired")
            versions.append({"id": k[1], "page": k[0], "text": plain(claims.bare(r["text"]), 300),
                             "from": start or first.get(k[0]) or until or today,
                             "open_start": start is None, "status": status})
            versions[-1]["until"] = prev = None if status == "live" else max(
                until or versions[-1]["from"], versions[-1]["from"])
        out.append({"kind": "line", "page": keys[-1][0], "versions": versions})
    out += _decision_changes(notes, shut)
    out.sort(key=lambda c: (max(v["until"] or v["from"] for v in c["versions"]), c["page"],
                            c["versions"][0]["id"]), reverse=True)
    return out


def _decision_changes(notes: dict, shut: set) -> list[dict]:
    """`changes`' decision chains: a superseded or retired decision and the later shown one
    that replaced it, in turn; a last one that holds no more is retired, on its file's day."""
    decs = {n.id: n for n in notes["decision"] if n.id not in shut and not is_private(n)}
    day = {i: topics.page_date(i, n.fields, n.body) for i, n in decs.items()}
    bodies = sorted((day[i], i, n.body) for i, n in decs.items())
    gone = {i for i, n in decs.items() if str(n.fields.get("status")) in ("superseded", "retired")}
    nxt = {i: by for i in gone if (by := chat_pages.replaced_by(i, day[i], decs[i].body, bodies))}
    out = []
    for head in gone - set(nxt.values()):
        ids, versions = _chain(head, nxt), []
        for n, i in enumerate(ids):
            known = day[i] or None
            start = max(known or _file_day(decs[i]), versions[-1]["from"] if versions else "")
            versions.append({"id": i, "page": i, "text": _title(decs[i]), "from": start,
                             "open_start": known is None, "until": None,
                             "status": "past" if n < len(ids) - 1
                             else "retired" if i in gone else "live"})
        for a, b in zip(versions, versions[1:], strict=False):
            a["until"] = b["from"]
        if versions[-1]["status"] == "retired":
            versions[-1]["until"] = max(_file_day(decs[ids[-1]]), versions[-1]["from"])
        out.append({"kind": "decision", "page": ids[-1], "versions": versions})
    return out


def _file_day(n) -> str:
    """The Keepfold day a page's file was last saved: the only day an undated decision has."""
    return local_day(datetime.fromtimestamp(n.path.stat().st_mtime).astimezone())


def _answer_secs(gaps: list[float]) -> int | None:
    """Median seconds of the last ANSWER_LAST gaps under ANSWER_CUT; None under ANSWER_MIN."""
    kept = [g for g in gaps[-ANSWER_LAST:] if 0 <= g <= ANSWER_CUT]
    return round(statistics.median(kept)) if len(kept) >= ANSWER_MIN else None


def _once(memo: dict | None, name: str, key, fn):
    """fn() once per `key` while the caller keeps `memo` (the app's home); without it, fn()."""
    if memo is None:
        return fn()
    hit = memo.get(name)
    if hit is None or hit[0] != key:
        hit = memo[name] = (key, fn())
    return hit[1]


def _created(memo: dict | None):
    """loops.created (a file stat each), once per note object while the caller keeps `memo`."""
    if memo is None:
        return loops.created
    old, new = memo.get("created", {}), {}
    memo["created"] = new

    def created(n):
        hit = old.get(n.path)
        new[n.path] = hit = hit if hit is not None and hit[0] is n else (n, loops.created(n))
        return hit[1]
    return created


class _Parsed(SimpleNamespace):
    """A read-only stand-in for the vault that serves the notes build_data already parsed."""

    def __init__(self, root: Path, notes: dict):
        super().__init__(root=root, notes=notes)

    def iter_notes(self, kind: str | None = None):
        return iter(self.notes[kind] if kind else [n for ns in self.notes.values() for n in ns])


def _plained(row: dict, *keys: str) -> dict:
    return row | {k: plain(row[k], 300) for k in keys if row.get(k) is not None}


def _read_help(name: str, *repo_path: str) -> str | None:
    """A help file: the wheel's copy (pyproject force-include), else the checkout's."""
    for p in (ASSETS / "help" / name, REPO.joinpath(*repo_path)):
        if p.is_file():
            return p.read_text(encoding="utf-8")
    return None


def _help() -> dict:
    """The `/keepfold help` reference and the GUIDE glossary table (trusted shipped files)."""
    cmds = _read_help("help.md", "skills", "keepfold", "modes", "help.md")
    text = _read_help("GUIDE.md", "docs", "GUIDE.md") or ""
    # match the heading by name, so renumbering the guide's sections can't lose the glossary
    head = next((ln[3:].strip() for ln in text.splitlines()
                 if ln.startswith("## ") and ln.rstrip().endswith(GLOSSARY)), None)
    rows = [ln for ln in loops.section_text(text, head).splitlines()
            if ln.startswith("|")] if head else []
    return {"commands_md": cmds,
            "glossary_md": "\n".join(rows) or None}


def _lobes(keep: dict, catalog: Catalog, hidden: set, pc: dict, facts: dict | None = None
           ) -> tuple[dict[str, str], list[dict]]:
    """Page id -> its brain lobe id, and the lobes [{id, title, kind, count}], biggest first.
    A lobe is a top hub (no parent, or a child of the topic map; never the map itself) under its
    hub id, a catalog domain as `domain:<id>`, a GROUPS lobe, or `unfiled`. A hub sits in its
    top hub's lobe; a concept, area or domain page in its domain's; a problem or mistake in the
    domain of its first visible concept; journal, me, session, placement and browse pages in
    their group; any other page in the lobe of its first hub under a top hub, else (projects,
    decisions, loops, people, media and chat resources) in a group; the rest are unfiled.
    A group lobe id that a hub already uses gets a `-group` suffix. `facts`: each page's
    `_facts`, when the caller kept them."""
    facts = facts or {i: _facts(n) for i, n in keep.items()}
    idx = {i: {"kind": n.kind, "title": facts[i][0], "fields": n.fields} for i, n in keep.items()}
    t = topics.tree(SimpleNamespace(_notes=idx))  # tree reads only `_notes`; no search index

    def top(h):
        p = t.path(h)
        return p[1] if p[0] == topics.ROOT and len(p) > 1 else p[0]

    def domain(i, fields=None):
        e = catalog.entries.get(i)
        d = (e.domain if e else "") or str((fields or {}).get("domain") or "")
        return f"domain:{d}" if d in catalog.entries and not {d, i} & hidden else None

    gid = {g: g + "-group" if g in t.parent else g for g in GROUP_TITLES}

    lobe = {}
    for i, n in idx.items():
        k, f = n["kind"], n["fields"]
        if i in t.parent:
            lobe[i] = top(i)
        elif k in ("concept", "area", "domain"):
            lobe[i] = domain(i, f)
        elif k in ("problem", "mistake"):
            cs = pc.get(i, []) if k == "problem" else _edge_targets(f.get("concepts") or [])
            lobe[i] = next(filter(None, map(domain, cs)), None)
        elif k in GROUPS:
            lobe[i] = gid[GROUPS[k]]
        else:
            hubs = (h for h in dict.fromkeys(facts[i][1]) if h in t.parent)  # page_hubs
            lobe[i] = next((x for x in map(top, hubs) if x != topics.ROOT), None) \
                or gid.get(facts[i][2])
        if lobe[i] in (None, topics.ROOT):
            lobe[i] = UNFILED

    titles = {gid[g]: title for g, title in GROUP_TITLES.items()}

    def row(c, k):
        if c == UNFILED:
            return {"id": c, "title": "Unfiled", "kind": UNFILED, "count": k}
        if c in titles and c not in t.parent:
            return {"id": c, "title": titles[c], "kind": "group", "count": k}
        if c.startswith("domain:"):
            return {"id": c, "title": plain(catalog.entries[c[7:]].title) or c[7:],
                    "kind": "domain", "count": k}
        return {"id": c, "title": idx[c]["title"], "kind": "topic", "count": k}

    count = Counter(lobe.values())
    return lobe, [row(c, k) for c, k in sorted(count.items(), key=lambda x: (-x[1], x[0]))]


def _facts(n) -> tuple[str, list[str], str | None]:
    """What `_lobes` reads of one note: its title, its `related` targets and the group it
    joins when it has no hub."""
    f = n.fields
    if n.kind != "resource":
        group = NO_HUB.get(n.kind)
    else:
        src = topics.page_source(n.body).lower()
        tags = f.get("tags")
        tags = tags if isinstance(tags, list) else str(tags or "").replace(",", " ").split()
        group = "media" if src in MEDIA or MEDIA & {str(x).lstrip("#").lower() for x in tags} \
            else "chats" if src in CHATS else None
    return _title(n), _edge_targets(f.get("related") or []), group


def _prop(v):
    if isinstance(v, list):
        return [str(x)[:300] for x in v[:SHOWN_PROPS]]
    return str(v)[:300]


def _pages(vault: Vault, notes: dict, hidden: set, catalog: Catalog, pc: dict,
           memo: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Every non-private note for the reader view (`raw/` is not a kind), and
    the brain lobes (`_lobes`). With `memo`, the same note objects and catalog give the same
    result, and a note object seen before keeps its row (the app's home reuses unchanged
    notes; a changed file is a new object)."""
    flat = [n for ns in notes.values() for n in ns]
    hit = memo.get("pages") if memo is not None else None
    if (hit and hit[1] == catalog.entries and hit[2] == hidden and len(hit[0]) == len(flat)
            and all(map(operator.is_, hit[0], flat, strict=True))):
        return hit[3]
    keep = {}
    for n in flat:
        if n.id not in hidden and not is_private(n):
            keep.setdefault(n.id, n)  # an id clash keeps the first kind, as wikilinks do
    old = memo.get("rows", {}) if memo is not None else {}
    got = {}
    for i, n in keep.items():
        hit = old.get(n.path)
        got[i] = hit if hit is not None and hit[0] is n else (n, *_row(vault, n), None)
    lobe, clusters = _lobes(keep, catalog, hidden, pc, {i: g[3] for i, g in got.items()})
    rows, pages = {}, []
    for i, (n, base, raw, facts, last) in got.items():
        links = [x for x in raw if x in keep and x != i]
        row = last if last and last["links"] == links and last["cluster"] == lobe[i] else \
            {**base, "links": links, "cluster": lobe[i]}
        rows[n.path] = (n, base, raw, facts, row)
        pages.append(row)
    if memo is not None:
        memo["rows"] = rows
        memo["pages"] = (flat, catalog.entries, hidden, (pages, clusters))
    return pages, clusters


def _row(vault: Vault, n) -> tuple[dict, list[str], tuple]:
    """A page's row without `links` and `cluster`, every link target its text names, and its
    `_facts`."""
    f = n.fields
    tags = f.get("tags")
    tags = tags if isinstance(tags, list) else str(tags or "").replace(",", " ").split()
    body = n.body
    if len(body.encode()) > BODY_MAX:
        body = body.encode()[:BODY_MAX].decode(errors="ignore") + TRUNCATED
    text = " ".join(str(v) for v in f.values()) + n.body
    facts = _facts(n)
    return {
        "id": n.id, "kind": n.kind, "title": facts[0],
        "summary": plain(f.get("summary") or "", 300) or None,
        "tags": [str(t).lstrip("#") for t in tags if str(t).strip("#")][:SHOWN_PROPS],
        "props": {str(k): _prop(v) for k, v in f.items()
                  if k not in TOOL_PROPS | {"id", "title", "summary", "private"}},
        "body": body,
        "links": None,
        "path": n.path.relative_to(vault.root).as_posix(),
        # the 04:00 Keepfold day, so a page saved at 1 a.m. counts as that night's Today
        "updated": local_day(datetime.fromtimestamp(n.path.stat().st_mtime).astimezone()),
        "date": topics.page_date(n.id, f, n.body) or None,
        "source": plain(topics.page_source(n.body)) or None,
        "cluster": None}, list(dict.fromkeys(link_targets(text))), facts


def _today_ids(pages: list[dict], notes: list, day: str) -> list[str]:
    """The shown pages the owner worked on today, not ones a Keepfold job rewrote: never a
    rollup or a topic hub (Keepfold builds and rewrites those); an imported page (a `Source:`
    line) or a browse page only when its own item is dated today, since an import rewrites
    the file; any other page by the Keepfold day it was last saved."""
    hubs = {n.id for n in notes if topics.is_hub({"kind": n.kind, "fields": n.fields})}
    # an AI chat's session receipt neither: the pages it changed are in Today already
    return [p["id"] for p in pages
            if p["kind"] != "rollup" and p["id"] not in hubs
            and not _is_ambient(p["kind"], p["id"])
            and (p["date"] if _imported(p) else p["updated"]) == day]


def _imported(p: dict) -> bool:
    """A page row an import wrote: a `Source:` line (an AI chat, notes, calendar) or browsing."""
    return bool(p["source"] or p["kind"] == "browse" or _IMPORTED.search(p["body"]))


def _ord(day) -> int:
    return date.fromisoformat(day).toordinal() if day else 0


def _sha(script: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode() + "'"


def render_html(data: dict) -> str:
    """One self-contained page: the template with the CSS, data blob and scripts inlined."""
    read = lambda name: (ASSETS / name).read_text(encoding="utf-8")  # noqa: E731
    lic = read("LICENSE-d3.txt").strip()
    # The XML namespace strings are not fetched; `\/` keeps them from reading as URLs.
    d3 = f"/*! d3 v7.9.0 | ISC License\n{lic}\n*/\n" + read("d3.v7.min.js").replace(
        '"http://www.w3.org', '"http:\\/\\/www.w3.org')
    brain, app = read("brain3d.js"), read("dashboard.js")
    csp = (f"default-src 'none'; script-src {_sha(d3)} {_sha(brain)} {_sha(app)}; "
           "style-src 'unsafe-inline'; img-src data:")
    # `:\/\/` is the same JSON string; note URLs then never read as fetchable URLs in the page
    blob = (json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace("://", ":\\/\\/"))
    slots = {"CSPMETA": f'<meta http-equiv="Content-Security-Policy" content="{csp}">',
             "STYLE": f"<style>{read('dashboard.css')}</style>",
             "SCRIPTS": f'<script type="application/json" id="data">{blob}</script>\n'
                        f"<script>{d3}</script>\n<script>{brain}</script>\n<script>{app}</script>"}
    return _SLOT.sub(lambda m: slots[m.group(1)], read("dashboard.html.tmpl"))


LIVE_FILES = ("d3.v7.min.js", "brain3d.js", "dashboard.js")


def render_live(key: str) -> str:
    """The app's live home: the same template with the CSS and scripts served by the app (its
    CSP header holds); no data blob, dashboard.js fetches /api/home. `key` busts the cache as
    on the app's other pages ('' when the page was opened without it)."""
    q = f"?t={key}"
    slots = {"CSPMETA": "", "STYLE": f'<link rel="stylesheet" href="/dashboard.css{q}">',
             "SCRIPTS": "\n".join(f'<script src="/{n}{q}" defer></script>' for n in LIVE_FILES)}
    return _SLOT.sub(lambda m: slots[m.group(1)],
                     (ASSETS / "dashboard.html.tmpl").read_text(encoding="utf-8"))


@contextmanager
def _snapshot_parses(vault: Vault):
    """Serve notes whose (mtime, size) still match the search index's snapshot from it, instead
    of opening and YAML-parsing every file again (most of a big vault's build time). Scoped:
    the entries leave the parse cache afterwards, so a later write in this process reads the
    file itself. Snapshot values are JSON (a date is its ISO string); the build reads them as
    text. Private entries hold no fields and are parsed as usual."""
    added = {}
    try:
        files = json.loads((vault.root / index.SNAPSHOT).read_bytes())
        files = files["files"] if files.get("schema") == index.SNAPSHOT_SCHEMA else {}
    except (OSError, ValueError, AttributeError, KeyError):
        files = {}
    for rel, e in files.items() if isinstance(files, dict) else ():
        if not (isinstance(e, list) and len(e) == 6 and isinstance(e[4], dict)
                and isinstance(e[5], str) and e[2] in Vault.KIND_DIRS and e[2] != "browse"):
            continue
        # the key is "<folder name>/<id>.md" (Index._rel): "chats/<id>.md" in archive/chats/
        d = next((d for d in Vault.dirs(e[2]) if d.rsplit("/", 1)[-1] == rel.split("/")[0]),
                 Vault.KIND_DIRS[e[2]])
        path = vault.root / d / f"{e[3]}.md"
        if path not in vault_mod._PARSED:
            added[path] = vault_mod._PARSED[path] = ((e[0], e[1]), e[4], e[5])
    try:
        yield
    finally:
        for path, hit in added.items():
            if vault_mod._PARSED.get(path) is hit:
                del vault_mod._PARSED[path]


def build(vault: Vault, *, now: datetime, relayout: bool = False) -> dict:
    """build_data, reading unchanged notes from the search index's snapshot."""
    with _snapshot_parses(vault):
        return build_data(vault, now=now, relayout=relayout)


def snapshot(vault: Vault, *, now: datetime) -> str:
    """The self-contained page Export snapshot saves where the owner chooses (never in the
    vault): non-private items only, CSP default-src 'none'. A pinned Home layout is baked in."""
    return render_html({**build(vault, now=now), "home_layout": home_layout()})


def retire_old(vault: Vault) -> Path | None:
    """`.keepfold/dashboard.html`, which Keepfold no longer writes, moves to `.keepfold/backups/`
    once (copied, then removed; never deleted outright). The new path, or None."""
    old = vault.root / ".keepfold" / "dashboard.html"
    if not old.is_file():
        return None
    day = datetime.fromtimestamp(old.stat().st_mtime).strftime("%Y-%m-%d")
    new = vault.write_text(vault.root / ".keepfold" / "backups" / f"dashboard-{day}.html",
                           old.read_bytes().decode("utf-8", "replace"))
    vault.remove(old)
    return new


def write_index(vault: Vault, *, now: datetime) -> Path:
    """Regenerate `<vault>/index.md`, the one-file text map of every non-private page. Built
    on demand (`keepfold dashboard`), never at session close."""
    catalog = Catalog.load(vault)
    with _snapshot_parses(vault):
        after, _ = planner.current_states(vault, local_day(now), catalog)
        private = planner.hidden(vault, catalog)
        notes = [n for n in vault.iter_notes() if not is_private(n) and n.id not in private]
        text = index_md(notes, catalog.entries, {c: s.state for c, s in after.items()})
    return vault.write_text(vault.root / "index.md", text)
