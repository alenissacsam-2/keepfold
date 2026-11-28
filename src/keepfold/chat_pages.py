"""Chat's typed answers read from the pages themselves (chat_facts routes to them, words them and
hands them to the model as its other facts are):

- inventory: "how many git repositories have I imported into Keepfold?", "list my projects": the
  pages of one kind (`THINGS`), counted, newest first
- schedule: "what upcoming tests or interviews do I have scheduled?", "what's coming up?":
  pending assessments dated today or later, soonest first, and imported calendar lines
  (`calendar-*` resource pages, a `- YYYY-MM-DD ...` line per event, sources.py's month pages)
  of the next CALENDAR_DAYS days
- decisions: "what did I decide about the storage engine?": decision pages holding the words,
  newest first; a superseded or retired one says which later decision replaced it (one whose
  page names its id, or that its own page names); a proposed one says "proposed, not confirmed".
  With no decision page, the user's own sentences in saved AI chats that hold the words and a
  decision phrase ("I've decided", "I'll go with", "switched from ... to", "instead of") are
  quoted, with the chat's title and day

Pages come under Chat's source switches: a saved AI chat under `chats`, any other page under
`notes`, a private page (its own `private: true` or under a private subject) only under
`private`. Titles and calendar lines are untrusted page text, carried as data."""

from __future__ import annotations

import re
from datetime import date, timedelta

from keepfold import chat_study

_LEAD = chat_study._LEAD
_TAIL = chat_study._TAIL
SHOWN = 3  # as chat_facts.SHOWN: titles an answer names
ROWS = 31  # rows the facts keep
CALENDAR_DAYS = 30
FIRST_DAY = "0001-01-01"  # as chat_facts.FIRST_DAY


def _resource(rtype: str):
    return lambda i, n: n["kind"] == "resource" and n["fields"].get("resource_type") == rtype


# what "how many <thing>" names -> (singular, plural, the test on a page id and index entry)
_KINDS = {
    "repo": ("git repository", "git repositories",
             lambda i, n: n["kind"] == "resource" and (
                 n["fields"].get("resource_type") == "repo" or i.startswith("repo-"))),
    "project": ("project", "projects", lambda i, n: n["kind"] == "project"),
    "decision": ("decision", "decisions", lambda i, n: n["kind"] == "decision"),
    "person": ("person", "people", lambda i, n: n["kind"] == "person"),
    "assessment": ("assessment", "assessments", lambda i, n: n["kind"] == "assessment"),
    "concept": ("concept", "concepts", lambda i, n: n["kind"] == "concept"),
    "mistake": ("mistake", "mistakes", lambda i, n: n["kind"] == "mistake"),
    "book": ("book", "books", _resource("book")),
    "paper": ("paper", "papers", _resource("paper")),
    "article": ("article", "articles", _resource("article")),
    "video": ("video", "videos", _resource("video")),
    "course": ("course", "courses", _resource("course")),
    "resource": ("saved source", "saved sources", lambda i, n: n["kind"] == "resource"),
}
THINGS = {**{k: k for k in _KINDS}, **{k + "s": k for k in _KINDS},
          "repository": "repo", "repositories": "repo", "people": "person", "persons": "person",
          "contacts": "person", "sources": "resource", "pdfs": "paper"}
_THING = r"(?:(?:git|code|github|imported|saved) )?(?P<{}>[a-z]+)"
_VERB = r"(?:have|imported|import|saved|save|added|add|got|kept|stored)"
_INVENTORY = re.compile(
    _LEAD + rf"(?:how many {_THING.format('a')}(?: (?:do|did|have) i {_VERB})?"
    r"(?: (?:in|into|to) keepfold)?(?: so far| in total| altogether)?"
    rf"|(?:(?:list|show)(?: me)?|what are|which are)(?: all)?(?: of)? my {_THING.format('b')}"
    r"(?: in keepfold)?)" + _TAIL)
_EVENT = r"(?:tests?|exams?|interviews?|deadlines?|assessments?|oas?|online assessments?|quiz)"
_EVENTS = rf"{_EVENT}(?:(?: or| and|,)+ (?:any )?{_EVENT})*"
_SOON = r"(?: (?:scheduled|coming up|lined up|ahead|left|upcoming|planned|soon))?"
_SCHEDULE = re.compile(
    _LEAD + rf"(?:(?:what|which|any)(?: are)?(?: my| the)?(?: upcoming| next| future| scheduled)?"
    rf" {_EVENTS}(?: (?:do|have|are) (?:i|there))?(?: (?:have|got))?{_SOON}"
    rf"|(?:do i have|are there|have i got) any(?: upcoming| more)? {_EVENTS}{_SOON}"
    rf"|(?:list|show)(?: me)? my(?: upcoming| next)? {_EVENTS}"
    r"|what is (?:coming up|ahead)(?: for me)?|what (?:do i have|have i got) coming up)" + _TAIL)
_DECIDE = re.compile(
    _LEAD + r"(?:what (?:did|have|had) i (?:decide|decided|choose|chose|chosen|pick|picked)"
    r"|(?:what (?:is|was|are|were) )?my (?:decisions?|choices?)"
    r"|(?:what|which) decisions? (?:did|have) i (?:make|made|take|taken)"
    r"|did i decide anything) (?:about|on|for|regarding) (?:the )?(?P<x>.+)")
_CAL_LINE = re.compile(r"- (\d{4}-\d{2}-\d{2})(?: (?:\d{1,2}:\d{2}(?: \([^)]*\))?|all day))?"
                       r"\s*[:·-]?\s*(.+)")
_WORD = re.compile(r"[a-z0-9]{4,}")
_SAID = re.compile(r"\bI(?:'ve| have|) decided\b|\bI(?:'ll| will) go with\b"
                   r"|\bswitched from\b.+?\bto\b|\binstead of\b", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def route(text: str, rest: str, got, today: date) -> dict | None:
    """{kind (inventory, schedule, decisions), term, lo, hi, focus, unit, today, and `thing`
    for an inventory} for `text`, whose `rest` is chat_study._norm'd with its date phrase
    (`got`, chat._when's) cut, or None. An inventory or decisions question with a date phrase is
    left to the passages; a schedule keeps it, looking forward ("on Friday" is the next one,
    chat_study._ahead) from today on: then lo and hi are its days."""
    from keepfold import chat_facts

    base = {"lo": FIRST_DAY, "hi": today.isoformat(), "focus": None, "unit": None,
            "today": today.isoformat()}
    if not got and (m := _INVENTORY.fullmatch(rest)):
        thing = THINGS.get(m["a"] or m["b"])
        return {**base, "kind": "inventory", "term": _KINDS[thing][1], "thing": thing} \
            if thing else None
    if _SCHEDULE.fullmatch(rest):
        q = {**base, "kind": "schedule", "term": ""}
        if got:
            lo, hi, _ = chat_study._ahead(text, got, today)
            if hi < today:
                return None
            q.update(lo=max(lo, today).isoformat(), hi=hi.isoformat())
            q["period"] = [q["lo"], q["hi"]]
        return q
    if not got and (m := _DECIDE.fullmatch(rest)):
        term = chat_facts._subject(m["x"])
        return {**base, "kind": "decisions", "term": term} if chat_facts.words(term) else None
    return None


def pages(vault, sources: dict) -> list[tuple[str, dict, bool]]:
    """[(id, page as the index keeps it, private)] of every page under Chat's switches."""
    from keepfold import ask, chat, semantic

    with chat._INDEX_LOCK:
        idx = chat._index(vault)
        out = [(i, n, False) for i, n in idx._notes.items()]
        if sources.get("private"):
            out += [(i, n, True) for i, (_, n) in ask.private_pages(vault, idx).items()]
    return [(i, n, p) for i, n, p in out
            if p or sources.get("chats" if semantic.is_chat(n) else "notes")]


def _day(i: str, n: dict) -> str:
    from keepfold.topics import page_date

    return page_date(i, n["fields"], n["body"])


def _title(i: str, n: dict) -> str:
    return chat_study._title(n["title"]) or i


def _out(q: dict, read: list, rows: list, sources: dict, **more) -> dict:
    return {"kind": q["kind"], "query": q["term"], "period": q.get("period"),
            "total": len(rows), "sources": {}, "rows": rows[:ROWS],
            "more": max(0, len(rows) - ROWS),
            "coverage": {"read": len(read), "matched": len(rows),
                         "off": [k for k, on in sources.items() if not on]}, **more}


def facts(vault, q: dict, sources: dict) -> dict:
    """The facts of an inventory, schedule or decisions route: chat_facts' shape, with rows
    {id, title, day} (and by kind days_left and source, or status and replaced_by)."""
    every = pages(vault, sources)
    if q["kind"] == "inventory":
        test = _KINDS[q["thing"]][2]
        rows = [{"id": i, "title": _title(i, n), "day": _day(i, n), "private": p}
                for i, n, p in every if test(i, n) and not (
                    q["thing"] == "resource" and _is_chat(n))]
        rows.sort(key=lambda r: (r["day"], r["title"]), reverse=True)
        return _out(q, every, rows, sources)
    if q["kind"] == "schedule":
        return _schedule(q, every, sources)
    return _decisions(q, every, sources)


def _is_chat(n: dict) -> bool:
    from keepfold import semantic

    return semantic.is_chat(n)


def _schedule(q: dict, every: list, sources: dict) -> dict:
    today = date.fromisoformat(q["today"])
    # no date phrase: every pending assessment from today on
    lo, hi = (q["lo"], q["hi"]) if q.get("period") else (q["today"], None)
    rows = []
    for i, n, _ in every:
        d = str(n["fields"].get("date") or "")[:10]
        if n["kind"] == "assessment" and str(n["fields"].get("outcome") or "pending") \
                == "pending" and lo <= d and (hi is None or d <= hi):
            rows.append({"id": i, "title": _title(i, n), "day": d, "source": "assessment",
                         "what": chat_study._title(n["fields"].get("kind") or "")})
    named = {(r["day"], w) for r in rows for w in _WORD.findall(r["title"].lower())}
    cal_hi = (today + timedelta(days=CALENDAR_DAYS)).isoformat()
    for i, n, _ in every:
        if n["kind"] != "resource" or not i.startswith("calendar-"):
            continue
        for ln in n["body"].splitlines():
            if not (m := _CAL_LINE.fullmatch(ln.strip())) or not (
                    lo <= m[1] <= (hi or cal_hi) and m[1] <= cal_hi):
                continue
            title = chat_study._title(m[2])[:120]
            if title and not {(m[1], w) for w in _WORD.findall(title.lower())} & named:
                rows.append({"id": i, "title": title, "day": m[1], "source": "calendar"})
    for r in rows:
        r["days_left"] = (date.fromisoformat(r["day"]) - today).days
    rows.sort(key=lambda r: (r["day"], r["source"] != "assessment", r["title"]))
    return _out(q, every, rows, sources, calendar_days=CALENDAR_DAYS)


def _names(text: str, i: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(i)}(?![\w-])", text) is not None


def replaced_by(i: str, d: str, body: str, decs: list) -> str | None:
    """The id of the decision that replaced decision `i` (dated `d`, its page text `body`), of
    `decs` [(day, id, body)]: a later one whose page names `i`, else one `i`'s page names; None.
    Chat's decisions and the dashboard's Mind Mirror (R27a) both use it."""
    later = [(d2, i2, b2) for d2, i2, b2 in decs if i2 != i and d2 >= d]
    by = next((x for x in later if _names(x[2], i)), None) or next(
        (x for x in later if _names(body, x[1])), None)
    return by[1] if by else None


def _decisions(q: dict, every: list, sources: dict) -> dict:
    from keepfold import chat_facts

    match = chat_facts._matcher(chat_facts.words(q["term"]))[0]
    decs = sorted(((_day(i, n), i, n) for i, n, _ in every if n["kind"] == "decision"),
                  key=lambda x: (x[0], x[1]))
    bodies, node = [(d, i, n["body"]) for d, i, n in decs], {i: n for _, i, n in decs}
    rows = []
    for d, i, n in reversed(decs):  # newest first
        if not match(f"{n['title']}\n{n['body']}"):
            continue
        status = str(n["fields"].get("status") or "")
        by = replaced_by(i, d, n["body"], bodies) if status in ("superseded", "retired") else None
        rows.append({"id": i, "title": _title(i, n), "day": d, "status": status,
                     "replaced": status in ("superseded", "retired"),
                     "replaced_by": _title(by, node[by]) if by else None})
    if rows:
        return _out(q, every, rows, sources)
    return _out(q, every, _said(every, match), sources, from_chats=True)


def _said(every: list, match) -> list:
    """For each saved AI chat in `every`, the first sentence of the user's own turns that holds
    the words and a decision phrase, newest chat first: {id, title (the chat's), day, quote}.
    `every` is already under Chat's switches (`pages`: a private chat only under `private`); a
    chat whose user turns cannot be told from the assistant's is skipped (harvest.user_turns)."""
    from keepfold import harvest, semantic

    rows = []
    for i, n, _ in every:
        if not semantic.is_chat(n) or (turns := harvest.user_turns(n["body"])) is None:
            continue
        for ln in (s for t in turns for s in _SENTENCE.split(t.replace("’", "'"))):
            if match(ln) and _SAID.search(ln):
                rows.append({"id": i, "title": _title(i, n), "day": _day(i, n),
                             "quote": chat_study._title(ln)})
                break
    return sorted(rows, key=lambda r: (r["day"], r["title"]), reverse=True)


# ---- words -------------------------------------------------------------------------------------

def _when(day: str, today: date) -> str:
    return chat_study._day(date.fromisoformat(day), today) if day else "undated"


def _left(n: int) -> str:
    return "today" if n == 0 else "tomorrow" if n == 1 else f"in {n} days"


def say(f: dict, today: date) -> str:
    """The facts in plain words, with no model."""
    out = _say(f, today)
    if not f["total"] and "notes" in f["coverage"]["off"]:
        out += " Notes are switched off in Chat settings."
    return out


def _say(f: dict, today: date) -> str:
    rows, n = f["rows"], f["total"]
    if f["kind"] == "inventory":
        if not n:
            return f"I found no {f['query']} in Keepfold."
        one = next(v for v in _KINDS.values() if v[1] == f["query"])[0]
        names = "; ".join(f'"{r["title"]}" ({_when(r["day"], today)})' for r in rows[:SHOWN])
        return (f"Keepfold holds {n} {one if n == 1 else f['query']}, newest first: {names}"
                + (f"; and {n - SHOWN} more" if n > SHOWN else "") + ".")
    if f["kind"] == "schedule":
        tests = [r for r in rows if r["source"] == "assessment"]
        cal = [r for r in rows if r["source"] == "calendar"]
        if not rows:
            return "I found no upcoming tests, interviews or calendar events in Keepfold."
        out = [f"You have {chat_study._plural(len(tests), 'upcoming assessment')}" + (
            ": " + "; ".join(f'"{r["title"]}" on {_when(r["day"], today)} '
                             f'({_left(r["days_left"])})' for r in tests) if tests else "")
               + "."]
        if cal:
            out.append(f"From your calendar in the next {f['calendar_days']} days: " + "; ".join(
                f'"{r["title"]}" on {_when(r["day"], today)}' for r in cal[:SHOWN * 2]) + (
                f"; and {len(cal) - SHOWN * 2} more" if len(cal) > SHOWN * 2 else "") + ".")
        return " ".join(out)
    if not n:
        return f'I found no decision about "{f["query"]}" in Keepfold.'
    if f.get("from_chats"):
        parts = [f'"{r["quote"]}" (chat "{r["title"]}", {_when(r["day"], today)})'
                 for r in rows[:SHOWN]]
        return (f'I found no decision page about "{f["query"]}", but in '
                f'{chat_study._plural(n, "saved chat")} you wrote: ' + "; ".join(parts)
                + (f"; and {n - SHOWN} more" if n > SHOWN else "") + ".")
    parts = []
    for r in rows[:SHOWN]:
        s = f'"{r["title"]}" ({_when(r["day"], today)}' + (
            f', {_status(r["status"])})' if r["status"] and not r["replaced"] else ")")
        if r["replaced"]:
            s += f', replaced by "{r["replaced_by"]}"' if r["replaced_by"] else ", replaced"
        parts.append(s)
    return (f'{chat_study._plural(n, "decision")} about "{f["query"]}", newest first: '
            + "; ".join(parts) + (f"; and {n - SHOWN} more" if n > SHOWN else "") + ".")


def _status(s: str) -> str:
    return "proposed, not confirmed" if s == "proposed" else s


def lines(f: dict) -> list[str]:
    """Each row as a line for the model (chat_facts._lines puts `say` first)."""
    if f["kind"] == "inventory":
        return [f"- {r['day'] or 'undated'}: {r['title']}" for r in f["rows"]]
    if f["kind"] == "schedule":
        return [f"- {r['day']} ({_left(r['days_left'])}): {r['title']} ({r['source']})"
                for r in f["rows"]]
    if f.get("from_chats"):
        return [f"- {r['day'] or 'undated'}: in chat \"{r['title']}\" you wrote: {r['quote']}"
                for r in f["rows"]]
    return [f"- {r['day'] or 'undated'}: {r['title']} ({_status(r['status']) or 'no status'}"
            + (f", replaced by {r['replaced_by']}" if r["replaced_by"] else "") + ")"
            for r in f["rows"]]
