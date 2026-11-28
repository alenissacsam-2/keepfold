"""The app's local chat: multi-turn questions over the archive (notes, saved AI chats, mistakes
and browsing, private notes and browsing included under the `private` switch), answered by a
LOCAL model only (`localllm`, never cloudllm). Each turn retrieves afresh through
`ask._passages`, wrapped as untrusted data; the earlier turns go to the model as plain text only.

History is kept in `<vault>/.keepfold/browse/chat/<ulid>.jsonl`, one JSON line per message. That
folder is inside the private browse tree on purpose (an answer can quote private browsing):
never served over MCP, never on the dashboard, never logged."""

from __future__ import annotations

import bisect
import builtins
import json
import math
import os
import re
import threading
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import NamedTuple

from keepfold import (
    access,
    ask,
    askfile,
    chat_facts,
    chat_study,
    chats,
    dates,
    events,
    localllm,
    prime,
    render,
    semantic,
    taint,
    xray,
)
from keepfold.chat_study import study_answer  # noqa: F401  chat.study_answer, beside site_list

# dates in a question: the rules live in keepfold.dates (`pin` shares them); chat._when and the
# rest are still read from here
from keepfold.dates import (  # noqa: F401
    _D,
    _DATE,
    _M,
    _MONTHS,
    _NUM,
    _PAD,
    _RULES,
    _UNIT,
    _WEEKDAYS,
    _WORDS,
    _ago,
    _day,
    _last,
    _monday,
    _month,
    _n,
    _weekday,
    _weekend,
    _when,
)
from keepfold.index import STOPWORDS, _wrap, aliases
from keepfold.vault import PRIVATE_BROWSE_DIR, Vault, VaultError

when = dates.when  # chat.when, as before the move

DIR = ".keepfold/browse/chat"
ID_RE = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")  # a ULID, so a path can never leave DIR
MAX_TEXT = 4000
TITLE = 60
TURNS = 6  # earlier messages the model sees
HISTORY_BUDGET = 3000  # their characters, on top of ask.BUDGET of passages: a 2B model's context
ABOUT_MAX = 600  # the "About the user" block (`about`); out of HISTORY_BUDGET, not on top of it
ABOUT = ("\n\nAbout the user, the person asking (data, not a passage): use it only to understand "
         "who is asking; answer facts only from the passages, and never cite it.\n")
LENGTH = {"short": 300, "normal": 700, "long": 1500}  # answer length -> max_tokens
DEFAULTS = {"length": "normal", "temperature": 0.2, "passages": 5,  # of ask.PASSAGE characters
            "sources": dict(ask.SOURCES), "history": True}
NO_ANSWER = "I don't know from your archive."
SEARCHED = 5  # pages listed as "searched, not cited" when the answer cites none
PAGES_READ = 4  # pages of an inventory, schedule or decisions answer the model reads
FRONT_MAX = 2000  # characters of a routed half's data answer (a site list) the model gets
# the app's Chat only: semantic.NOT_SET_UP names a CLI command (fine for agents, not here)
NO_SEARCH_MODEL = ("Search by meaning is off: download the search model in Setup to find notes by "
                   "meaning.")
SAID_NOTHING = ("{model} gave no answer, twice. These are the pages it searched; open one to "
                "read it, or ask again in other words.")
CITE = "\n\n(Answer from the passages above and cite each one you use as [n].)"  # 2B models forget
SYSTEM = ask.SYSTEM + (" Earlier messages are the conversation so far: use them to understand a "
                       "follow-up question, but answer only from the passages in the latest "
                       "message. Today is {today}.")
_live: dict[str, list] = {}  # chats of a session with history off: memory only, never written
_LIVE_LOCK = threading.Lock()  # the stream thread writes _live while the page lists chats


# ---- settings (`chat:` in ~/.keepfold/config.yaml) -------------------------------------------

def _valid(key: str, v) -> bool:
    num = isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)
    return {"length": lambda: v in LENGTH,
            "temperature": lambda: num and 0 <= v <= 1,
            "passages": lambda: num and v == int(v) and 4 <= v <= 12,
            "history": lambda: isinstance(v, bool),
            "sources": lambda: isinstance(v, dict) and set(v) <= set(ask.SOURCES)
            and all(isinstance(x, bool) for x in v.values())}.get(key, lambda: False)()


def get_settings(config: dict | None = None) -> dict:
    """The chat settings; a missing or bad value is its default."""
    cfg = (taint.config() if config is None else config).get("chat")
    cfg = cfg if isinstance(cfg, dict) else {}
    out = {k: cfg[k] if k in cfg and _valid(k, cfg[k]) else v for k, v in DEFAULTS.items()}
    out["sources"] = {**ask.SOURCES, **out["sources"]}
    out["passages"] = int(out["passages"])
    return out


def save_settings(body: dict) -> dict:
    """Validate `body` (some of DEFAULTS' keys), merge it into the saved settings and return
    them; ValueError names the first bad field."""
    if extra := set(body) - set(DEFAULTS):
        raise ValueError(f"unknown setting {sorted(extra)[0]!r}")
    for k, v in body.items():
        if not _valid(k, v):
            raise ValueError({"length": "length must be short, normal or long",
                              "temperature": "temperature must be a number from 0 to 1",
                              "passages": "passages must be a whole number from 4 to 12",
                              "history": "history must be true or false",
                              "sources": "sources must map notes, chats, browsing and private "
                                         "to true or false"}[k])

    def merge(cfg: dict) -> dict:  # under the config lock
        cur, old = get_settings(cfg), cfg.get("chat")
        new = {**cur, **body, "sources": {**cur["sources"], **body.get("sources", {})}}
        return {"chat": {**(old if isinstance(old, dict) else {}), **new}}
    taint.write_config(merge)
    return get_settings()


_GENERIC = frozenset(
    "which what when where who whom did do does done i me my mine visit visited visiting see "
    "saw seen look looked looking site sites website websites page pages web read reading "
    "watched go went browse browsed open opened about on at the a an any anything something "
    "tell show find found was were have has had in of to for from there that this ago day "
    "days week weeks month months give example examples explain elaborate detail details "
    "please again".split())


def topic(text: str, span: tuple[int, int]) -> str:
    """`text` without the date phrase at `span` and without generic words: '' for "which
    sites did I visit yesterday?"."""
    rest = text[:span[0]] + " " + text[span[1]:]
    return " ".join(w for w in re.findall(r"\w+", rest.lower())
                    if w not in _GENERIC and w not in STOPWORDS)


_POINTS_BACK = frozenset(
    "it its it's that this these those they them their he she him his her same also else "
    "more former latter".split())
_OPENS_FOLLOW_UP = re.compile(r"\s*(?:and|but|so|also|what\s+about|how\s+about)\b", re.I)
# "the second one", "which one", "can a list be one?": a pick from the last answer ("one"
# elsewhere is too common: "one-hot encoding")
_PICKS_ONE = re.compile(
    r"\b(?:first|second|third|last|other|previous|which)\s+ones?\b|\bones?\W*$", re.I)
# "the algorithms that I know": a relative clause, not "that" pointing back
_RELATIVE_THAT = re.compile(r"\bthat\s+(?:i|you|we|they|he|she|my|your|our|their)\b", re.I)
# "this year" is a date (chat_facts reads years; _when does not), never "this" pointing back
_THIS_YEAR = re.compile(r"\bthis\s+year\b", re.I)


def follow_up(text: str, got=None) -> bool:
    """Whether `text` leans on the previous question ("and its running time?", "why?", "what
    about heaps"): then both are searched together. A question that names its own topic is
    searched alone, so a new subject is not mixed with the last one. `got`: its date phrase,
    whose words ("this month") never point back."""
    own = text[:got[2][0]] + " " + text[got[2][1]:] if got else text
    own = _THIS_YEAR.sub(" ", _RELATIVE_THAT.sub(" ", own))
    if _OPENS_FOLLOW_UP.match(text) or _PICKS_ONE.search(own) \
            or _POINTS_BACK & set(re.findall(r"[\w']+", own.lower())):
        return True
    return not got and not topic(text, (0, 0))  # nothing of its own to search for


_ITEM = re.compile(r"(?:\d{1,2}[.)]|[-*•])\s+(\S.*)")
_CITES = re.compile(r"\s*\[\d{1,3}(?:\s*,\s*\d{1,3})*\]")
_ORDS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4,
         "4th": 4, "fifth": 5, "5th": 5, "sixth": 6, "6th": 6, "seventh": 7, "7th": 7,
         "eighth": 8, "8th": 8, "ninth": 9, "9th": 9, "tenth": 10, "10th": 10, "last": -1}
_ORD = "|".join(_ORDS)
# "the second one", "the 2nd", "number 2", "#2", "the last one", "the first", "the other one";
# never "the first time I ..." or "the last week"; a bare "the first" picks only when at most one
# word ends the clause ("what does the 2nd cover?"), never "what was the first course I took?"
_PICK = re.compile(
    rf"\bthe\s+({_ORD})\b(?!\s+(?:times?|days?|weeks?|months?|years?|steps?|part|half|place"
    rf"|things?)\b)(?=\s*(?:[\w'-]+\s*)?(?:[?.!,;:]|$))|\b({_ORD})\s+one\b"
    rf"|\bnumber\s+(\d{{1,2}})\b|#(\d{{1,2}})\b|\bthe\s+other\s+one\b", re.I)


def list_items(answer: str) -> list[str]:
    """The items of the list in an assistant's `answer`: top-level lines starting 1. 1) - * or
    •, as plain text (citations, links and markup out; a trailing "(...)" kept)."""
    out = []
    for ln in answer.splitlines():
        if len(ln) - len(ln.lstrip()) < 2 and (m := _ITEM.fullmatch(ln.strip())) \
                and (item := chat_study._title(_CITES.sub("", m[1]))):
            out.append(item)
    return out


def picked(text: str, got, items: list[str], prev: str) -> str | None:
    """The item of the assistant's last list (`items`) that `text` picks ("the second one",
    "#2", "the last one", "the other one" of two: the one `prev` did not name), or with a pronoun
    and no pick the only item of a one-item list; None otherwise. `got`: `text`'s date phrase."""
    m = _PICK.search(text)
    if m and m[0].lower().startswith("the other"):
        named = [i for i in items if i.split(" (")[0].casefold() in prev.casefold()]
        return next(i for i in items if i not in named) if len(items) == 2 and len(named) == 1 \
            else None
    if m:
        w = (m[1] or m[2] or "").lower()
        n = _ORDS[w] if w else int(m[3] or m[4])
        return items[n - 1 if n > 0 else n] if items and -1 <= n <= len(items) and n else None
    own = text[:got[2][0]] + " " + text[got[2][1]:] if got else text
    own = _THIS_YEAR.sub(" ", _RELATIVE_THAT.sub(" ", own))
    if len(items) == 1 and _POINTS_BACK & set(re.findall(r"[\w']+", own.lower())):
        return items[0]
    return None


# ---- storage ---------------------------------------------------------------------------------

def _path(vault: Vault, id: str):
    if not isinstance(id, str) or not ID_RE.fullmatch(id):
        raise ValueError("not a chat id")
    return vault.root / DIR / f"{id}.jsonl"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def new() -> str:
    from ulid import ULID  # lazy, as in events: its import is slow
    return str(ULID())


def load(vault: Vault, id: str) -> list:
    """The messages of chat `id` ([] for a new one); torn or odd lines are skipped."""
    p = _path(vault, id)
    with _LIVE_LOCK:
        live = _live.get(id)
    if live is not None:
        return [*live]
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out = []
    for line in raw.splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") \
                and isinstance(m.get("text"), str):
            out.append(m)
    return out


def _save(vault: Vault, id: str, msgs: list, keep: bool) -> None:
    with _LIVE_LOCK:
        # a chat touched while history was off stays in memory for the session, even after
        # history is turned on again: writing it then would save the history-off turns
        if not keep or id in _live:
            _live[id] = msgs
            while len(_live) > 50:
                _live.pop(next(iter(_live)))
            return
    vault.write_text(_path(vault, id), "".join(  # a <private> span stays in memory only
        json.dumps({**m, "text": chats.drop_private(m["text"])} if isinstance(
            m.get("text"), str) else m, ensure_ascii=False) + "\n" for m in msgs))


def _summary(id: str, msgs: list) -> dict | None:
    first = next((m["text"] for m in msgs if m["role"] == "user"), None)
    if first is None:
        return None
    return {"id": id, "title": " ".join(first.split())[:TITLE],
            "updated": msgs[-1].get("ts", ""), "count": len(msgs)}


def list(vault: Vault) -> list:  # shadows the builtin on purpose: chat.list
    """[{id, title (the first question cut to TITLE characters), updated, count}], newest
    first."""
    folder = vault.root / DIR
    ids = {p.stem for p in folder.glob("*.jsonl") if ID_RE.fullmatch(p.stem)} \
        if folder.is_dir() else set()
    with _LIVE_LOCK:
        live = set(_live)
    out = [s for i in ids | live if (s := _summary(i, load(vault, i)))]
    return sorted(out, key=lambda s: (s["updated"], s["id"]), reverse=True)


def delete(vault: Vault, id: str) -> bool:
    p = _path(vault, id)
    with _LIVE_LOCK:
        gone = _live.pop(id, None) is not None
    return vault.remove(p) or gone


def delete_all(vault: Vault) -> int:
    folder = vault.root / DIR
    with _LIVE_LOCK:
        n = len(_live)
        _live.clear()
    for p in folder.glob("*.jsonl") if folder.is_dir() else ():
        if ID_RE.fullmatch(p.stem) and vault.remove(p):
            n += 1
    return n


# ---- a turn ------------------------------------------------------------------------------------

# "which sites did I visit in the past 4 months?": answered from the stored day counts, every
# site in the range, never from a few passages (a small model lists some and repeats them)
_SITES_Q = re.compile(r"\b(?:sites?|websites?|domains?)\b", re.I)
# "which sites did I visit", "sites I've been on", "my most visited sites": the person's own
# browsing, never "the domain of every function" or "the binding sites used by hemoglobin"
_LIST_Q = re.compile(r"\b(?:i|i've|ive|me|my)\b[\w\s',-]{0,40}?\b(?:visit\w*|went|go|gone|been"
                     r"|open\w*|brows\w*|use[ds]?|surf\w*)\b", re.I)
# a browser filter only as "in Comet", "on Edge": "sites to learn edge detection" is no browser
_BROWSER = re.compile(r"\b(?:in|on|using|from|with)\s+(?:the\s+)?(?:google\s+|microsoft\s+)?"
                      r"(chrome|edge|brave|comet|firefox|opera|vivaldi|arc)\b", re.I)
# "what's a good site to practise SQL?", "can you suggest websites I could use?": advice
_SUGGEST = re.compile(r"\b(?:good|best|better|suggest\w*|recommend\w*|should\s+i\s+use"
                      r"|could\s+use)\b", re.I)
# words a plain "list my sites" question holds besides its date and browser; any other word is
# a topic ("which sites do I use for DSA?"), for the passages
_LIST_WORDS = frozenset("list all unique distinct every most top many often frequently usually "
                        "visits use used using been ve s go gone surf surfed browser".split())
NOTES_OFF = ("Notes and mistakes are switched off in Chat settings, so I can't read your study "
             "plan.")
SITES_SHOWN = 2000  # "all the sites": every one, short of a runaway answer
_MD = re.compile(r"[*_`\[\]<>#|]")  # a model-written description stays plain text


def _count(x) -> int | None:
    """A stored count as an int (missing: 0), or None when it is not a number."""
    try:
        return int(x or 0)
    except (TypeError, ValueError, OverflowError):
        return None


def _site_counts(vault: Vault, got, sources: dict, browser: str | None = None):
    """(seen {site: {n, days, hidden, cat}}, visits left out for want of a stored browser,
    days, llm cache, browse settings) for `got`'s days (None: all days), under Chat's
    `browsing` and `private` switches and the browsing settings (`sites._sides`): the one count
    behind the site list and the digest."""
    from keepfold.browse import load_settings, sites

    days = sites.lean_days(vault)
    llm = sites.load_cache(vault)["llm"]
    settings = load_settings()
    side, scat = sites._sides(settings, llm, days)
    lo, hi = (got[0].isoformat(), got[1].isoformat()) if got else ("", "9999")
    seen: dict[str, dict] = {}
    meta: dict[str, tuple[str, str]] = {}  # key -> (site, category)
    unknown = 0  # visits of a named browser's range stored before Keepfold kept the browser
    for day, keys in days.items():
        if not lo <= day <= hi or not isinstance(keys, dict):
            continue
        for key, a in keys.items():
            h = side(day, key) if isinstance(a, dict) else None
            if h is None or not sources.get("private" if h else "browsing"):
                continue
            n = a.get("n", 0)
            if type(n) is not int and (n := _count(n)) is None:
                continue  # a malformed entry is skipped, never the whole answer
            if browser:
                w = a.get("w")
                if not isinstance(w, dict):
                    unknown += n
                    continue
                # ids are source keys ("comet/Default"); a bare browser name is accepted too
                per = [_count(c) for k, c in w.items()
                       if isinstance(k, str) and k.split("/")[0] == browser]
                if None in per:
                    continue
                b = sum(per)
                n = min(n, b)  # a forget lowers "n" only
                if n <= 0:
                    continue
            if (m := meta.get(key)) is None:
                site, cat, _pd = sites._split(key)
                m = meta[key] = (site, cat or scat(site))
            if (e := seen.get(m[0])) is None:
                e = seen[m[0]] = {"n": 0, "days": set(), "hidden": False, "cat": m[1]}
            e["n"] += n
            e["days"].add(day)
            e["hidden"] |= bool(h)
    return seen, unknown, days, llm, settings


def asks_sites(text: str, today: date | None = None) -> bool:
    """Whether `text` asks for a list of the sites the person visited (`site_list`): their own
    browsing, not a suggestion ("a good site for SQL") and no topic beyond dates and a browser."""
    if not (_SITES_Q.search(text) and _LIST_Q.search(text)) or _SUGGEST.search(text):
        return False
    b = _BROWSER.search(text)
    rest = text[:b.start()] + " " + text[b.end():] if b else text
    dated = _when(rest, today or _today())
    words = {w for w in topic(rest, dated[2] if dated else (0, 0)).split() if not w.isdigit()}
    return not words - _LIST_WORDS  # "top 3 sites": a number is no topic


def site_list(vault: Vault, text: str, got, sources: dict,
              today: date | None = None) -> str | None:
    """The answer to a question asking for a list of sites, or None for any other question:
    every site visited in `got`'s days (None: all days), most visits first, with its category
    and the local model's one-line description, under Chat's own source switches (`browsing`,
    `private`) and the browsing settings (a denied or Don't save site is never stored)."""
    today = today or _today()
    if not asks_sites(text, today):
        return None
    b = _BROWSER.search(text)
    if not (sources.get("browsing") or sources.get("private")):
        return "Browsing is switched off in Chat settings, so I cannot list sites."
    browser = b[1].lower() if b else None
    seen, unknown, days, llm, settings = _site_counts(vault, got, sources, browser)
    lo = got[0].isoformat() if got else ""
    when = f"from {_span(got[0], got[1], today)}" if got else "in everything Keepfold has stored"
    if browser:
        when = f"in {browser.title()} {when}"
    notes = []
    if unknown:
        notes.append(f"{unknown} visit{'s' * (unknown != 1)} in this range were imported "
                     "before Keepfold kept which browser they came from, so they are left out. "
                     "Days your browsers no longer fully hold cannot get it back.")
    stored = min((d for d in days if isinstance(days[d], dict)), default=None)
    if stored and lo < stored:
        notes.append(f"Keepfold has browsing from {stored} on; browsers keep about 90 days of "
                     "history, so older days are here only if they were imported then.")
    if skipped := sorted(settings.skip_categories):
        notes.append(f"Categories set to Don't save ({', '.join(skipped)}) are never stored, so "
                     "they cannot be listed; change that under Browsing in the app.")
    if not seen:
        return " ".join([f"No sites are stored {when}."] + notes)
    order = sorted(seen.items(), key=lambda kv: (-kv[1]["n"], kv[0]))
    lines = [f"**{len(order)} site{'s' * (len(order) != 1)}** {when}, most visits first:", ""]
    for site, e in order[:SITES_SHOWN]:
        entry = llm.get(site) if isinstance(llm.get(site), dict) else {}
        # a visible site never shows what the model wrote from private titles ("h")
        said = (entry.get("h") or entry.get("v")) if e["hidden"] else entry.get("v")
        said = said if isinstance(said, dict) else {}
        desc = _MD.sub("", str(said.get("desc") or "")).strip()
        cat = "" if e["cat"] in ("", "other") else f" · {e['cat']}"
        nd = len(e["days"])
        lines.append(f"- **{site}**{cat} · {e['n']} visit{'s' * (e['n'] != 1)} on {nd} "
                     f"day{'s' * (nd != 1)}" + (f": {desc}" if desc else ""))
    if len(order) > SITES_SHOWN:
        lines.append(f"- and {len(order) - SITES_SHOWN} more with fewer visits")
    return "\n".join(lines + ([""] + notes if notes else []))


DIGEST_SHOWN = 10  # lines per digest section
_plural = chat_study._plural
_NOTE_KINDS = ("note", "resource", "decision", "project", "loop")  # resource: chats too
_JOURNAL_LINE = re.compile(r"- (?:\d\d:\d\d|—) ")  # as reflect.portrait; — is back-dated
_INDEX: dict = {}  # vault root -> the digest's Index, kept between answers
_INDEX_LOCK = threading.Lock()  # two chat streams may digest at once
_DAYS: dict = {}  # vault root -> {(path, its stat): (ask._note_day, semantic.is_chat)}


def _capped(head: str, items: list, more: str = "more") -> list:
    return [head, "", *[f"- {x}" for x in items[:DIGEST_SHOWN]],
            *([f"- and {len(items) - DIGEST_SHOWN} {more}"] if len(items) > DIGEST_SHOWN else [])]


FRESH = 5.0  # UX-B: seconds the "#" list and Review's related lines reuse an unrefreshed index
_REFRESHED: dict = {}  # vault root -> time.monotonic() of its index's last refresh


def _index(vault: Vault, max_age: float = 0.0):
    """The kept Index of `vault` (the pages ask._passages searches), refreshed, not rebuilt
    (~3 s a build). `max_age`: skip the refresh (~90 ms at 5k) when the last one is that
    recent. Call it under _INDEX_LOCK."""
    from keepfold.catalog import Catalog
    from keepfold.index import Index

    idx, now = _INDEX.get(vault.root), time.monotonic()
    if idx is not None and now - _REFRESHED.get(vault.root, -1e9) < max_age:
        return idx
    catalog = Catalog.load(vault)
    if idx is None:
        idx = _INDEX[vault.root] = Index(vault, catalog)
    else:
        idx.catalog = catalog
        idx.refresh()
    _REFRESHED[vault.root] = time.monotonic()
    return idx


def _page_days(vault: Vault, sources: dict) -> list:
    """[(day, id, page, is_chat, is_private, switches)] of every page the index holds and,
    under the `private` switch, every private one (`ask.private_pages`), each dated as
    `ask._passages(between=…)` dates it (`ask._note_day`; '' undated), with the switches its
    passages fall under there (`ask._switch`; any one on lets the search return it)."""
    with _INDEX_LOCK:
        idx = _index(vault)
        paths = {i: p for p, i in idx._ids.items()}
        stats = dict(idx._stats)
        private = {i: n for i, (_, n) in ask.private_pages(vault, idx).items()} \
            if sources.get("private") else {}
        pages = [*idx._notes.items(), *private.items()]  # chat.list shadows the builtin
    # (day, is_chat) per file version: both read the whole body (~1 ms a note), so they are
    # kept between answers for the files that did not change
    old, memo = _DAYS.get(vault.root, {}), {}
    out = []
    for i, n in pages:
        if i not in paths:
            continue
        k = (paths[i], stats.get(paths[i]))
        d, is_chat = memo[k] = old.get(k) or (ask._note_day(vault, i, n, paths[i]),
                                              semantic.is_chat(n))
        own = "private" if i in private else "chats" if is_chat else "notes"
        # a browse/ page is a browse passage as a search hit, a note or chat as `ask._dated`'s
        switches = (own, "browsing") if own != "private" and idx._rel(
            paths[i]).startswith("browse/") else (own,)
        out.append((d, i, n, is_chat, i in private, switches))
    _DAYS[vault.root] = memo
    return out


def _pages_in(vault: Vault, lo: str, hi: str, sources: dict) -> list:
    """[(day, kind, id, title, body, is_chat, is_private, aliases)] of notes, saved AI chats and
    journal pages dated in [lo, hi] (`ask._note_day`: a date, else a note's last change), under
    Chat's `notes` and `chats` switches. Private pages (`ask.private_pages`: their own
    `private: true`, or under a private subject) come under the `private` switch instead: the
    local model may read them (owner's decision, 19 Nov 2026). `aliases`: index.aliases."""
    out = [(d, n["kind"], i, chat_study._title(n["title"]) or i, n["body"], is_chat, priv,
            aliases(n["fields"]))
           for d, i, n, is_chat, priv, _ in _page_days(vault, sources)
           if n["kind"] in (*_NOTE_KINDS, "journal") and lo <= d <= hi
           and (priv or sources.get("chats" if is_chat else "notes"))]
    return sorted(out, key=lambda p: (p[0], p[3]))


_HORIZON: dict = {}  # vault root -> (stamp, sorted days): `_horizon_days`


def _private_names(vault: Vault) -> tuple:
    """The files under browse/private/ (home.version leaves them out): their days are their
    names, so names alone stamp them."""
    top = vault.root / PRIVATE_BROWSE_DIR
    return tuple(sorted(os.path.join(d, f) for d, _, fs in os.walk(top) for f in fs))


def _horizon_days(vault: Vault, sources: dict) -> list[str]:
    """Every day (sorted, unique) that holds a page `ask._passages(between=…)` could return
    under `sources`: indexed and private pages as `_page_days` dates them, and browse pages by
    their file name (`ask._browse_day`; vectors.search embeds every browse/**/*.md first). Kept
    per home.version (its listings: browse/sites/ site pages are left out, but they are
    undated), the private browse names and the switches."""
    from keepfold import home  # lazy: home pulls in the dashboard

    key = (home.version(vault), _private_names(vault) if sources.get("private") else (),
           tuple(sorted((k, bool(v)) for k, v in sources.items())))
    hit = _HORIZON.get(vault.root)
    if hit and hit[0] == key:
        return hit[1]
    days = {d for d, _, _, _, _, switches in _page_days(vault, sources)
            if d and any(sources.get(k) for k in switches)}
    top = vault.root / "browse"
    hidden = os.path.normcase(str(vault.root / PRIVATE_BROWSE_DIR))
    for folder, _, files in os.walk(top):
        nc = os.path.normcase(folder)
        if sources.get("private" if nc == hidden or nc.startswith(hidden + os.sep)
                       else "browsing"):
            days |= {d for f in files if f.endswith(".md") and (d := ask._browse_day(f))}
    days = sorted(days)
    _HORIZON[vault.root] = (key, days)
    return days


def horizon(vault: Vault, sources: dict) -> dict | None:
    """{first, last, months: ["YYYY-MM", ...]}: the months holding any page Chat's search could
    read under `sources` (`_horizon_days`), or None when it could read none."""
    return _horizon(_horizon_days(vault, sources))


def _horizon(days: list[str]) -> dict | None:
    months = sorted({d[:7] for d in days})
    return {"first": months[0], "last": months[-1], "months": months} if months else None


def _month_name(ym: str) -> str:
    try:
        return f"{date.fromisoformat(ym + '-01'):%b %Y}"
    except ValueError:  # a page id's odd day ("2026-13-40") still counts, as the search's
        return ym


def _nothing(lo: date, hi: date, days: list[str], today: date) -> str | None:
    """Keepfold's own answer when no page `ask._passages(between=(lo, hi))` could return is
    dated in [lo, hi] (`days`: `_horizon_days`), else None. The same day check as the search's
    filter, so it never hides a page the search would find."""
    a, b = lo.isoformat(), hi.isoformat()
    i = bisect.bisect_left(days, a)
    if i < len(days) and days[i] <= b:
        return None
    when = f"on {_span(lo, hi, today)}" if lo == hi else f"from {_span(lo, hi, today)}"
    out = f"Keepfold has nothing {when}."
    if days and (a > days[-1] or b < days[0]):
        out += f" Your data covers {_month_name(days[0][:7])} – {_month_name(days[-1][:7])}."
    return out


def digest(vault: Vault, lo: date, hi: date, sources: dict, today: date,
           unit: str | None = None) -> str:
    """What Keepfold holds about the days `lo` to `hi`, read from its own data with no model: what
    was studied (chat_study's range answer), the most visited sites (`_site_counts`), notes,
    saved AI chats and private notes written or changed, and journal lines, each section capped
    and only when it has something, then one line on what is left out. `unit`: "day", "week" or
    "days" for the heading (chat_study.digest_days), else by the range."""
    when = f"on {_span(lo, hi, today)}" if lo == hi else f"from {_span(lo, hi, today)}"
    unit = unit or ("day" if lo == hi else "days")
    if unit == "week" and (lo, hi) == (_monday(today), today):
        unit = "week so far"  # on a Monday too, when it is one day so far
    sections = []
    studied = chat_study.answer(vault, "range", (lo, hi, None), today,
                                bool(sources.get("private"))) \
        if sources.get("notes") else chat_study.NO_STUDY
    if not studied.startswith(chat_study.NO_STUDY):
        sections.append(studied.splitlines())
    skipped: list = []
    if sources.get("browsing") or sources.get("private"):
        seen, _, _, _, settings = _site_counts(vault, (lo, hi), sources)
        skipped = sorted(settings.skip_categories)
        if seen:
            order = sorted(seen.items(), key=lambda kv: (-kv[1]["n"], kv[0]))
            visits = sum(e["n"] for e in seen.values())
            sections.append(_capped(
                f"**{_plural(len(order), 'site')}** visited ({_plural(visits, 'visit')}), "
                "most first:",
                [f"**{s}**" + ("" if e["cat"] in ("", "other") else f" · {e['cat']}")
                 + f" · {_plural(e['n'], 'visit')}" for s, e in order], "more sites"))
    pages = _pages_in(vault, lo.isoformat(), hi.isoformat(), sources)
    notes = [p[3] for p in reversed(pages) if p[1] != "journal" and not p[5] and not p[6]]
    if notes:
        sections.append(_capped(f"**{_plural(len(notes), 'note')}** written or changed:", notes))
    chats = [p[3] for p in reversed(pages) if p[5] and not p[6]]
    if chats:
        sections.append(_capped(f"**{_plural(len(chats), 'saved AI chat')}**:", chats))
    private = [p[3] for p in reversed(pages) if p[6]]  # any kind: under the private switch only
    if private:
        sections.append(_capped(f"**{_plural(len(private), 'private note')}** written or "
                                "changed:", private))
    journal = [(_span(d := date.fromisoformat(p[0]), d, today) + " " if lo != hi else "")
               + chat_study._title(ln[2:].strip())
               for p in pages if p[1] == "journal" and not p[6]
               for ln in p[4].splitlines() if _JOURNAL_LINE.match(ln)]
    if journal:
        sections.append(_capped(f"**Journal** ({_plural(len(journal), 'line')}):", journal))
    off = [label for k, label in (("notes", "notes, studying and journal"),
                                  ("chats", "saved AI chats"),
                                  ("browsing", "browsing"),
                                  ("private", "private notes, subjects and browsing"))
           if not sources.get(k)]
    left = [f"{', '.join(off)} (switched off in Chat settings)"] if off else []
    if skipped:
        left.append(f"site categories set to Don't save ({', '.join(skipped)}), never stored")
    tail = ["Not included: " + "; ".join(left) + "."] if left else []
    if not sections:
        return " ".join([f"Keepfold has nothing recorded {when}.", *tail])
    out = [f"**Your {unit}** {when}:"]
    for sec in sections:
        out += ["", *sec]
    return "\n".join([*out, *([""] + tail if tail else [])])


def _looping(text: str) -> bool:
    """A small model caught in a loop: some line of 20+ characters said three times."""
    seen = Counter(ln.strip() for ln in text.splitlines() if len(ln.strip()) >= 20)
    return bool(seen) and seen.most_common(1)[0][1] >= 3


def _once(text: str) -> str:
    """`text` with each repeated line of 20+ characters kept only the first time."""
    out, seen = [], set()
    for ln in text.splitlines():
        k = ln.strip()
        if len(k) >= 20 and k in seen:
            continue
        seen.add(k)
        out.append(ln)
    return "\n".join(out)


def _today(now: datetime | None = None) -> date:
    """The local day as events, site days and the dashboard count it: it starts at 04:00."""
    return date.fromisoformat(events.local_day(now or datetime.now(UTC)))


def _span(a: date, b: date, today: date | None = None) -> str:
    """'17 Nov', '10 Nov to 16 Nov' or '11 Sep 2001' (a year other than `today`'s)."""
    today = today or _today()
    day = chat_study._day
    return day(a, today) if a == b else f"{day(a, today)} to {day(b, today)}"


def _local(runner) -> bool:
    """Only a plain loopback localllm.Runner hears private passages: cloudllm's CloudRunner
    subclasses Runner, so isinstance would let it through."""
    if type(runner) is not localllm.Runner:
        return False
    try:
        localllm._check_url(runner.base)
    except ValueError:
        return False
    return True


def _facts_runner(runner):
    """The local runner to word a typed answer's facts, or None (then `chat_facts.say` words
    them): the app passes localllm.find, looked up only now that facts were found."""
    if callable(runner) and not isinstance(runner, localllm.Runner):
        try:
            runner = runner()
        except ValueError:  # a refused localllm setting: the facts in plain words instead
            return None
    return runner if _local(runner) else None


def _made_with_off(m: dict, sources: dict) -> bool:
    """Was answer `m` made with a source switch on that `sources` now has off? An answer saved
    before switches were recorded counts as made with every switch on."""
    was = m.get("switches")
    was = was if isinstance(was, dict) else {}
    return any(was.get(k, True) and not on for k, on in sources.items())


def about(vault: Vault, sources: dict, today: date) -> str:
    """The system message's "About the user" block: prime's pinned core (me/core.md) and
    Profile, wrapped as untrusted data and unnumbered (never a citable [n]), within ABOUT_MAX;
    "" when there is nothing to say. A page comes in only when Chat would show it as a passage
    (`access.may_see("chat_local", ...)` under the source switches), never on a weaker rule."""
    def shown(page: str) -> bool:
        note = prime.me(vault, page)
        return note is not None and access.may_see(
            "chat_local", access.audience_of_note(note), "note", sources)
    withheld = {p for p in ("core", "profile") if not shown(p)}
    if len(withheld) == 2:
        return ""
    evs, day = [*events.EventLog(vault).iter_events()], today.isoformat()  # chat.list shadows
    served, waits = prime.served_claims(vault, evs), prime.held(vault, evs, day)
    lines = prime.core_lines(vault, served, day, withheld, prime.claim_ids(vault),
                             xray.settings(vault)["mute"] | waits)  # as prime.core does
    if "profile" not in withheld and (summary := prime.profile_summary(vault, day, served,
                                                                       waits)):
        lines.append(summary)
    text = "\n".join(lines).strip()
    while text:  # trim until the wrapped block fits (escaping can lengthen the text)
        block = ABOUT + _wrap("about the user", text)
        if (over := len(block) - ABOUT_MAX) <= 0:
            return block
        if len(text) - over < 40:
            return ""
        text = text[: len(text) - over - 1].rstrip() + "…"
    return ""


def history(msgs: list, sources: dict | None = None, spent: int = 0) -> list:
    """The last TURNS messages as plain {role, content}, oldest dropped first to fit
    HISTORY_BUDGET, same-role neighbours joined (a failed turn leaves two user messages; some
    chat templates refuse that), starting with a user message. With `sources` (the switches
    now), an answer made with a switch now off is left out with the question(s) before it, so
    a private site listed earlier never reaches the model after Private is turned off. A turn
    the model never answered (`unanswered`: Keepfold's note, not the model's words) is left out."""
    out: list = []
    for _, m in _sent(msgs, sources, spent):
        text = m["text"].strip()
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"] += "\n\n" + text
        else:
            out.append({"role": m["role"], "content": text})
    return out


def _sent(msgs: list, sources: dict | None, spent: int = 0) -> list:
    """[(index in `msgs`, message)] of the messages `history` sends, oldest first; `spent`
    characters of HISTORY_BUDGET are taken already (the `about` block)."""
    pairs = [(i, m) for i, m in enumerate(msgs) if not m.get("unanswered")]
    if sources is not None:
        kept: list = []
        for i, m in pairs:
            if m["role"] == "assistant" and _made_with_off(m, sources):
                while kept and kept[-1][1]["role"] == "user":
                    kept.pop()
                continue
            kept.append((i, m))
        pairs = kept
    out, left = [], HISTORY_BUDGET - spent
    for i, m in reversed(pairs[-TURNS:]):
        text = m["text"].strip()
        if not text:
            continue
        if len(text) > left:
            break
        left -= len(text)
        out.append((i, m))
    out.reverse()
    while out and out[0][1]["role"] != "user":
        out.pop(0)
    return out


def remembers(msgs: list, sources: dict | None = None) -> int:
    """The index of the oldest message of `msgs` the model still gets on the next turn
    (`history`), for the Chat page's "The model remembers from here" line: 0 when it gets them
    all (no line), len(msgs) when it gets none."""
    sent = _sent(msgs, sources, ABOUT_MAX)  # the most `about` may take: never too generous
    return sent[0][0] if sent else len(msgs)


_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.M)


RELATIVES = 3  # names one "my <relation>" may add to the search
_MY = re.compile(r"\bmy\s+\w", re.I)


def relatives(vault: Vault, text: str) -> list[str]:
    """Names of the people pages whose `relation` field `text` names as "my <relation>" ("my
    sister", "my study-partner"), for the search: the question rarely holds the name itself.
    Public people pages only: the index never holds a private one, and none is read here."""
    if not _MY.search(text):
        return []
    with _INDEX_LOCK:
        people = sorted((n["title"], str(n["fields"].get("relation") or ""))
                        for n in _index(vault)._notes.values() if n["kind"] == "person")
    out = []
    for title, rel in people:
        tag = re.escape(rel).replace(r"\-", r"[\s-]")
        if rel and re.search(rf"\bmy\s+{tag}s?\b", text, re.I) and (name := chat_study._title(
                title)) and name not in out:
            out.append(name)
    return out[:RELATIVES]


PICK = 8  # UX-B: rows of the "#" list
PICKED = 3  # pages one message may carry


def _may_read(idx, i: str, paths: dict, sources: dict) -> bool:
    """UX-B: may Chat read page `i` under its switches `sources` now? The one rule for "#",
    the list and the send: the switch `passage` asks (`ask._switch`) must be on; a private
    page (left out of `idx._notes`) only under `private`. An id the index lacks: never."""
    if i not in paths:
        return False
    if i in idx._private:
        return bool(sources.get("private"))
    if (n := idx._notes.get(i)) is None:
        return False
    rel = idx._rel(paths[i])
    kind = "browse" if rel.startswith("browse/") else "chat" if semantic.is_chat(n) else "note"
    return bool(sources.get(ask._switch({"path": rel, "kind": kind})))


def pick_list(vault: Vault, q: str, sources: dict) -> list[dict]:
    """UX-B: the "#" list, [{id, title, kind}] of at most PICK pages Chat may read
    (`_may_read`): those whose title or aliases hold every word of `q` (the index's
    `title_matches`), or with no words the most recently changed. The shared index holds no
    private page, so none is offered."""
    from keepfold.index import fts_query
    with _INDEX_LOCK:
        idx = _index(vault, FRESH)
        paths = {i: p for p, i in idx._ids.items()}
        ids = idx.title_matches(q, None, 10 * PICK) if fts_query(q) else (
            idx._ids[p] for p in sorted(idx._ids, key=lambda p: idx._stats[p], reverse=True))
        out = []
        for i in ids:
            if i in idx._notes and _may_read(idx, i, paths, sources):
                n = idx._notes[i]
                out.append({"id": i, "title": chat_study._title(n["title"]) or i,
                            "kind": n["kind"]})
                if len(out) == PICK:
                    break
    return out


def picked_pages(vault: Vault, ids, sources: dict) -> list[dict]:
    """UX-B: the pages picked with "#" (at most PICKED ids) as passages led like an attached
    file's (`_page_passages`, up to askfile.CHUNK_MAX characters each); an id Chat may not
    read now (`_may_read`) is dropped before any page is read."""
    ids = [*dict.fromkeys(i for i in ids if isinstance(i, str))][:PICKED] \
        if isinstance(ids, builtins.list) else []  # chat.list shadows the builtin
    if not ids:
        return []
    with _INDEX_LOCK:
        idx = _index(vault)
        paths = {i: p for p, i in idx._ids.items()}
        ok = [i for i in ids if _may_read(idx, i, paths, sources)]
    return _page_passages(vault, ok, askfile.CHUNK_MAX)


def _page_passages(vault: Vault, ids: list, cap: int = ask.PASSAGE) -> list[dict]:
    """The start of each page in `ids` (up to `cap` characters) as a passage
    (`ask._passages`' shape), for a compare's top pages and the pages picked with "#". The
    callers named them under Chat's switches, so a private one (only there under `private`)
    is read as `ask.private_pages` reads it."""
    with _INDEX_LOCK:
        idx = _index(vault)
        paths = {i: p for p, i in idx._ids.items()}
        private = set(idx._private)
    out = []
    for i in dict.fromkeys(ids):
        if i not in paths or (n := ask._page(vault, paths[i], i)) is None:
            continue
        p = ask._note(vault, i, n, paths[i].relative_to(vault.root).as_posix(),
                      semantic._chunk_text(n, 0)[:cap],
                      "chat" if semantic.is_chat(n) else "note",
                      **({"private": True} if i in private else {}))
        p["at"], p["len"] = ask._span(p.pop("_body"), p["text"], p["title"])
        out.append(p)
    return out


def _clean(text: str) -> str:
    """Untrusted page text, line by line, without links, images, URLs or HTML."""
    return "\n".join(render.plain(ln, 10_000) for ln in text.splitlines()).strip()


def passage(vault: Vault, path, at, size, sources: dict) -> dict:
    """The passage an answer cited (`ask._passages`' path, at and len): {path, title, kind,
    heading (the last heading above it, "" at the top), text}, at most ask.PASSAGE characters
    from `at` of the page's body, links and images stripped. Under Chat's switches of now, as an
    answer reads (`ask._switch`): a page whose switch is off, a private one with `private` off,
    raises PermissionError, never shows. A path outside the vault or not a page: ValueError."""
    from keepfold.browse import vectors

    if not isinstance(path, str) or not path.endswith(".md") or "\\" in path \
            or path.startswith("/") or ".." in path.split("/"):
        raise ValueError("Not a page of this vault.")
    try:
        at, size = max(0, int(at)), int(size)
    except (TypeError, ValueError):
        raise ValueError("at and len must be whole numbers.") from None
    size = min(size, ask.PASSAGE) if size > 0 else ask.PASSAGE
    try:
        full = vault._contained(path)
    except VaultError as e:
        raise ValueError(str(e)) from None
    with _INDEX_LOCK:
        idx = _index(vault)
        id_ = idx._ids.get(full)
        private = id_ in idx._private
        n = idx._notes.get(id_) if id_ is not None and not private else None
    if id_ is not None and n is None:
        n = ask._page(vault, full, id_)
    if path.startswith("browse/") and n is None:  # a browse page the private search reads
        try:
            title, body = vectors._body(full.read_bytes().decode("utf-8", "replace"))
        except OSError:
            raise ValueError("That page is gone.") from None
        n = {"title": title or full.stem, "body": body, "kind": "browse"}
    if n is None:
        raise ValueError("That page is gone.")
    kind = "browse" if path.startswith("browse/") else "chat" if semantic.is_chat(n) else "note"
    if not sources.get(ask._switch({"path": path, "kind": kind, "private": private})):
        raise PermissionError("That page is under a source switch that is off in Chat now.")
    body = n["body"]
    heads = [m for m in _HEADING.finditer(body) if m.start() <= at]
    return {"path": path, "title": chat_study._title(n["title"]), "kind": kind,
            "heading": chat_study._title(heads[-1][1]) if heads else "",
            "text": _clean(body[at:at + size])}


def _inert(text: str) -> str:
    """An attached file's passage shown as an answer: untrusted text (`_clean`) whose brackets,
    stars and backticks never become citations or markup in the app's renderer."""
    return _clean(text).translate({ord("["): "(", ord("]"): ")", ord("*"): None, ord("`"): "'"})


class Route(NamedTuple):
    """A question Keepfold answers from its own data: the step line, and either `data` (its
    words, no model) or `fq` (a chat_facts query: counted by code, a model may word it)."""
    step: str
    data: Callable[[], str] | None = None
    fq: dict | None = None


def _route(vault: Vault, text: str, own, today: date, first: bool, s: dict,
           leans: bool = False) -> Route | None:
    """The data answer for `text`, or None (the passages answer it): a site list, a digest, a
    study answer, then chat_facts' typed answers (not for a follow-up). `own`: chat._when of
    `text`'s own date phrase; `first`: the chat's first question."""
    span = f" from {_span(own[0], own[1], today)}" if own else ""
    if asks_sites(text, today):
        return Route("Counting the sites you visited" + span,
                     lambda: site_list(vault, text, own, s["sources"], today))
    if (asked := chat_study.kind(text, own, today, first)) == "digest":
        lo, hi, unit = chat_study.digest_days(text, own, today)
        return Route("Putting together your " + ("day" if (unit, lo) == ("day", today) else
                                                 f"{unit} {'on' if lo == hi else 'from'} "
                                                 f"{_span(lo, hi, today)}"),
                     lambda: digest(vault, lo, hi, s["sources"], today, unit))
    if asked is not None:
        return Route(chat_study.STEP[asked] + (span if asked == "range" else ""),
                     lambda: chat_study.study_answer(vault, text, own, today, first,
                                                     bool(s["sources"].get("private")))
                     if s["sources"].get("notes") else NOTES_OFF)
    if not leans and (fq := chat_facts.route(text, own, today)) is not None:
        return Route(chat_facts.step(fq, today), fq=fq)  # a model, if any, only words it
    return None


def _by_code(vault: Vault, r: Route, sources: dict, today: date) -> str:
    """Route `r`'s answer in Keepfold's own words, no model."""
    return r.data() if r.data else chat_facts.say(chat_facts.facts(vault, r.fq, sources), today)


_SPLIT = re.compile(r",?\s+and\s+|\s*;\s+", re.I)
_WH = re.compile(r"\b(?:what|which|who|whom|whose|when|where|why|how)\b", re.I)
_AUX = re.compile(r"\s*(?:is|are|was|were|do|does|did|can|could|should|would|will|have|has"
                  r"|had)\b", re.I)
_NO_SPLIT = re.compile(r"^\s*compare\b|\bvs\.?(?!\w)|\bversus\b", re.I)
HALF_WORDS = 3  # "where and when ...?" is one question


def _asks(half: str) -> bool:
    return len(re.findall(r"[\w'.-]+", half)) >= HALF_WORDS and bool(
        _WH.search(half) or _AUX.match(half))


def halves(text: str, routes) -> list[str]:
    """`text` as its two questions ("what does X use and how many times did I visit Y?"), split
    at the first " and " or "; " where both halves read as questions (a question word, or the
    second half `routes`), else []. "compare X and Y", "X vs Y" and an "and" inside one noun
    phrase ("the pros and cons of B-trees") are never split."""
    if _NO_SPLIT.search(text):
        return []
    for m in _SPLIT.finditer(text):
        a, b = text[:m.start()].strip(" ,"), text[m.end():].strip()
        if _asks(a) and (_asks(b) or routes(b)):
            return [a, b]
    return []


# a data answer that found nothing: site_list, digest and `_nothing`, the study answers
_EMPTY = ("No sites are stored", "Browsing is switched off", "Keepfold has nothing", NOTES_OFF,
          chat_study.NO_STUDY, "No concepts yet")


def certainty(*, data: bool = False, empty: bool = False, read: int = 0, cited: int = 0,
              quoted: bool = False, unquoted: bool = False, unanswered: bool = False,
              stopped: bool = False) -> str | None:
    """How far an answer can be trusted, from what `reply` already knows (no model call):

    | answer                                                         | certainty  |
    |----------------------------------------------------------------|------------|
    | stopped                                                        | None       |
    | nothing found, an empty data/facts answer, a switch off        | "none"     |
    | a data or facts answer counted by code, with a result          | "exact"    |
    | model: unanswered, cites nothing, or a quote no passage holds  | "thin"     |
    | model: cites, quotes at least one span, every quote found      | "exact"    |
    | model: cites, no quote, read 2+ passages                       | "grounded" |
    | model: cites, no quote, read 1 passage                         | "thin"     |

    `data`: counted by code (site list, digest, study answer, chat_facts); `empty`: it (or the
    search) found nothing; `read`/`cited`: passages; `quoted`/`unquoted`: any quoted span /
    any one no passage holds (`ask.unquoted`)."""
    if stopped:
        return None
    if empty:
        return "none"
    if data:
        return "exact"
    if unanswered or not cited or unquoted:
        return "thin"
    if quoted:
        return "exact"
    return "grounded" if read >= 2 else "thin"


def _conflicts(vault: Vault, ps: list[dict]) -> list[tuple[dict, dict]]:
    """[(old passage, new passage)] for each two of `ps` whose me/ pages hold a recorded pair
    (claims.recorded_pairs: `update_of`, `supersedes`), once per two pages: the newer is the
    later day, a tie the recorded direction."""
    from pathlib import PurePosixPath

    me = {PurePosixPath(p["path"]).stem: p for p in ps if p["path"].startswith("me/")}
    if len(me) < 2:  # the event log is read only when a pair could be there
        return []
    from keepfold import claims

    pairs, _ = claims.recorded_pairs(events.EventLog(vault).iter_events())
    out, seen = [], set()
    for old, new, _ in pairs.values():
        a, b = me.get(old[0]), me.get(new[0])
        if a is None or b is None or a is b or (two := frozenset((a["path"], b["path"]))) in seen:
            continue
        seen.add(two)
        out.append((b, a) if (a.get("day") or "") > (b.get("day") or "") else (a, b))
    return out


def reply(vault: Vault, convo_id: str | None, text: str, runner, emit, cancel,
          settings: dict | None = None, today: date | None = None,
          files: list | None = None, pages: list | None = None) -> str:
    """One turn of chat `convo_id` (None: a new chat). Saves the question, retrieves, streams
    the answer through `emit`: {"t": "start", id, model}, {"t": "step", "s"} as it searches and
    as the model starts reading (the app shows each with a live count), {"t": "think"} while a
    reasoning model only thinks, {"t": "facts", facts} before any words of a typed data answer
    (`chat_facts`: count, first, last, timeline, about, compare, gaps; the model then only
    words them (a compare also reads each side's top page), or
    `chat_facts.say` does with no model; saved on the answer as `facts`), {"t": "tok", "s"}
    per piece, {"t": "src", sources: [{n, path, title, kind, at, len}] (the passages cited;
    `passage` shows one), searched (the same without n, when none is cited), coverage: {read,
    matched, more (true when a search was full: "matched" is a floor), also (names
    `relatives` added, when any)} when passages were searched (how many
    the model read of how many the search found; saved on the answer too), unquoted (quoted
    spans no cited passage holds, `ask.unquoted`; saved too) when any, certainty (`certainty`:
    exact, grounded, thin or none; saved too) unless stopped, horizon {first, last, months}
    ("YYYY-MM": `horizon`, what the search could read; saved as {first, last}) on an answer
    from the passage search, conflicts [{old, new}: {path, title, day}] when two pages it cited
    (read, when none cited) hold a recorded pair (`_conflicts`; saved too)}; {"t": "text", "s"}
    replaces the words so far (a loop cut, leaked tokens dropped, a retry). A model that says
    nothing is asked once more; still nothing, the answer is SAID_NOTHING with the pages it
    searched (saved with `unanswered`). A past date range (the question's or a follow-up's)
    that holds no page the search could read is answered at once by Keepfold (`_nothing`), with
    no search model or runner. Then {"t": "done", id, model, seconds, stopped,
    remembers (`remembers` of the chat now)}; or {"t": "err", code (not_set_up,
    no_local_model, no_answer), message}. A set `cancel` stops the stream; the partial answer
    is saved with `stopped: true`. Returns the chat id. `runner` is a local `localllm.Runner`
    or None; never a cloud one.

    `files` (C1, askfile): files attached in the app. Then every question searches them (no
    data answer): their matching chunks lead (at most askfile.LEAD) and the vault's passages
    fill the rest (none without the search model). A file is read only by a local runner: with
    none (or a refused setting) the answer is the askfile.SHOWN best passages under
    askfile.NO_MODEL (with askfile.LOCAL_ONLY first, once, when Chat's model is a cloud one),
    saved as their labels only, never the file's text.

    `pages` (UX-B): ids picked with "#", read as `picked_pages` reads them (an id Chat may not
    read now is dropped unread); they lead the passages as a file's do and, like a file,
    mean no data answer and no need of the search model."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise ValueError(f"Type a message (up to {MAX_TEXT:,} characters).")
    s = settings or get_settings()
    read = picked_pages(vault, pages, s["sources"]) if pages else []
    convo_id = convo_id or new()
    msgs = load(vault, convo_id)  # validates the id
    t0, today = time.monotonic(), today or _today()
    t = SimpleNamespace(vault=vault, id=convo_id, text=text, runner=runner, emit=emit,
                        cancel=cancel, s=s, today=today, files=files, read=read, msgs=msgs)
    if not _route_stage(t):
        return convo_id
    t.model = t.runner.model if t.passages and t.runner is not None else "Keepfold"
    msgs.append({"role": "user", "text": text.strip(), "ts": _now()})
    _save(vault, convo_id, msgs, s["history"])
    emit({"t": "start", "id": convo_id, "model": t.model})
    _retrieve(t)
    _prompt(t)
    _stream(t)
    _finish(t, t0)
    return convo_id


# `reply`'s stages share one turn, `t` (a SimpleNamespace: `reply` fills the inputs, each stage
# adds what the next reads). Every module name tests patch is looked up when a stage runs.

def _route_stage(t: SimpleNamespace) -> bool:
    """Turn `t`'s path, decided before `start` is sent: a data answer by code (`codes`,
    `listed`), a typed facts answer (`route`, `fq`) or the passage search (`passages`: `query`,
    `got`, `said`, two `searches`, a routed `front` half), and the runner it needs. Sends the
    err and returns False when the search model or a local runner is missing."""
    vault, text, s, msgs, today = t.vault, t.text, t.s, t.msgs, t.today
    files, read, runner, emit, convo_id = t.files, t.read, t.runner, t.emit, t.id
    prev = next((m["text"] for m in reversed(msgs) if m["role"] == "user"), "")
    # the last answer as the model would still see it (`history`): its list, if any, is what
    # "the second one" points at
    last = next((m["text"] for m in reversed(msgs) if m["role"] == "assistant"
                 and not m.get("unanswered") and not _made_with_off(m, s["sources"])), "")
    got, said = _when(text, today), text
    item = picked(text, got, list_items(last), prev)
    leans = item is not None or follow_up(text, got)
    if leans and not got and prev and (got_prev := _when(prev, today)):
        got, said = got_prev, prev  # "which of those was hardest?": the last dates still hold
    query = f"{text.strip()}\n{item or prev}".strip() if leans else text.strip()
    # a site list or a study answer is read from Keepfold's own data with no model, so it needs
    # neither the search model nor a local runner; it keeps to its own dates (a follow-up's
    # borrowed ones are for passages only), and so does its step line
    own = got if said is text else None
    # which data answer, if any, is decided first and cheaply; it is read after `start` and its
    # step are sent, so the app shows the step (and Stop) while a heavy vault is counted
    # (an attached file is asked about: passages, never a data answer)
    # a two-part question ("what does X use and how many times did I visit Y?"; not one that
    # leans on the last turn): each half routed on its own, both by code, one in front of the
    # other half's passages, or two searches. A half that routes wins over the whole question
    # routing ("how many times did I visit X and how many times did I visit Y?")
    def half(h: str) -> Route | None:
        return _route(vault, h, _when(h, today), today, not prev, s)
    split = [(h, half(h)) for h in halves(text, lambda h: half(h) is not None)] \
        if not files and not read and not (leans and prev) else []
    route = None if files or read or any(r for _, r in split) else \
        _route(vault, text, own, today, not prev, s, leans)
    split = split if route is None else []
    codes = [route] if route and route.fq is None else \
        [r for _, r in split] if split and all(r for _, r in split) else []
    front = next((r for _, r in split if r), None) if split and not codes else None
    if front:  # the other half is searched alone, with its own dates
        query = said = next(h for h, r in split if r is None)
        got = _when(said, today)
    searches = [(h, _when(h, today), n) for (h, _), n in zip(
        split, ((s["passages"] + 1) // 2, s["passages"] // 2), strict=True)] \
        if split and not codes and not front else None
    fq = route.fq if route else None
    listed = "" if codes else None  # a data answer: filled in after its step
    passages = listed is None and fq is None  # the search model and a runner are needed
    searched_pages = passages  # the answer the horizon is for; sent with its sources
    days = None
    # a past range (pages are dated by when they were written: a future one keeps no page)
    # that holds no page the search could return is answered at once: no model, no embedder
    if passages and not files and not read and not split and got and got[0] <= today:
        days = _horizon_days(vault, s["sources"])
        end = min(got[1], today)
        if none := _nothing(got[0], end, days, today):
            codes, listed, passages = [Route(
                f"Searching your notes and browsing from {_span(got[0], end, today)}",
                lambda: none)], "", False
    blocked = None # with a routed half: why its other half gets no model answer
    if passages and not files and not read and semantic.load_embedder() is None:
        if front is None:
            emit({"t": "err", "code": "not_set_up", "message": NO_SEARCH_MODEL,
                  "id": convo_id})
            return False
        blocked = NO_SEARCH_MODEL
    if passages and not blocked and callable(runner) \
            and not isinstance(runner, localllm.Runner):
        try:  # the app passes localllm.find: it may start LM Studio, so only when needed
            runner = runner()
        except ValueError as e:  # a refused localllm setting (cloud model, non-loopback url)
            if not files and front is None:
                emit({"t": "err", "code": "no_local_model", "message": str(e), "id": convo_id})
                return False
            runner, blocked = None, None if files else str(e)
    if passages and not blocked and not _local(runner):
        if not files and front is None:
            emit({"t": "err", "code": "no_local_model", "message": ask.NO_MODEL,
                  "id": convo_id})
            return False
        # a file never reaches any other runner: its passages are shown instead
        runner, blocked = None, None if files else ask.NO_MODEL
    if blocked:  # the routed half by code, and why the rest has no answer
        codes, listed, passages = [front], "", False
    vars(t).update(got=got, said=said, query=query, split=split, route=route, codes=codes,
                   front=front, searches=searches, fq=fq, listed=listed, passages=passages,
                   searched_pages=searched_pages, days=days, blocked=blocked, runner=runner)
    return True


def _retrieve(t: SimpleNamespace) -> None:
    """Turn `t`'s data, after `start`: each code answer, sent as it is counted (`listed`,
    `parts`); a facts answer's `facts` and the pages read to word them (`word`: a model words
    them); or a routed half's `front_text` and the passages searched (`found`, with `stats`
    and `also` for the coverage line)."""
    vault, s, today, files, read, emit = t.vault, t.s, t.today, t.files, t.read, t.emit
    cancel, runner, route, fq, listed, codes = t.cancel, t.runner, t.route, t.fq, t.listed, t.codes
    blocked, front, got, said, query, split = t.blocked, t.front, t.got, t.said, t.query, t.split
    searches, model = t.searches, t.model
    found: list = []
    also: list = []  # people's names `relatives` added to the search
    stats: dict = {}  # ask._passages' match count, for the coverage line
    parts: list = []
    facts = None
    word = False  # a facts answer the model words (`_prompt`)
    max_tokens = LENGTH[s["length"]]
    front_text = ""  # a routed half's answer, in front of the other half's passages
    if listed is not None:
        for r in codes:
            emit({"t": "step", "s": r.step})
            listed = _by_code(vault, r, s["sources"], today)
            if cancel is not None and cancel.is_set():  # Stop while counting: nothing shown
                break
            piece = ("\n\n" if parts else "") + listed
            parts.append(piece)
            emit({"t": "tok", "s": piece})
        if blocked and parts:
            parts.append("\n\n" + blocked)
            emit({"t": "tok", "s": parts[-1]})
    elif fq is not None:
        emit({"t": "step", "s": route.step})
        facts = chat_facts.facts(vault, fq, s["sources"])
        if cancel is not None and cancel.is_set():
            pass
        elif chat_facts.empty(facts) or (runner := _facts_runner(runner)) is None:
            emit({"t": "facts", "facts": facts})  # before the words, for the facts box
            parts.append(chat_facts.say(facts, today))
            emit({"t": "tok", "s": parts[0]})
        else:
            emit({"t": "facts", "facts": facts})
            if fq["kind"] == "about" and semantic.load_embedder() is not None:
                found = ask._passages(vault, fq["term"], 4, sources=s["sources"], stats=stats,
                                      between=None if fq["lo"] == chat_facts.FIRST_DAY
                                      else (fq["lo"], fq["hi"]),  # the period the facts cover
                                      topic=fq["term"])
            elif fq["kind"] == "compare":  # each side's top page, read from its start
                found = _page_passages(vault, [r["page"]["id"] for r in facts["rows"]
                                               if r["page"] and r["page"]["id"]])
            elif fq["kind"] in chat_facts.PAGE_KINDS:  # the pages counted, read to be cited
                found = _page_passages(vault, [*dict.fromkeys(
                    r["id"] for r in facts["rows"])][:PAGES_READ])
            model, max_tokens = runner.model, LENGTH["short"]  # 1 to 3 sentences
            emit({"t": "step", "s": f"Wording the facts with {runner.model}" + (
                f" and reading {len(found)} page{'s' * (len(found) != 1)}" if found else "")})
            word = True
    else:
        if front:
            emit({"t": "step", "s": front.step})
            if front.fq:
                facts = chat_facts.facts(vault, front.fq, s["sources"])
                emit({"t": "facts", "facts": facts})
                front_text = "\n".join(chat_facts._lines(facts, today))
            else:
                front_text = front.data()[:FRONT_MAX]
        # pages are dated by when they were written: a future range ("tomorrow's exam") keeps
        # no page, so it does not narrow the search; a range running past today stops at today
        past = got and got[0] <= today
        names = ", ".join([*(f.name for f in files or []), *(p["title"] for p in read)])
        emit({"t": "step", "s": "Searching " + (f"{names} and " if names else "")
              + "your notes and browsing" + (
            f" from {_span(got[0], min(got[1], today), today)}" if past else "")})
        hits = askfile.rank(files, query) if files else []
        if files and runner is None:  # no local model: the file's best passages, shown as found
            found = [askfile.passage(f, n, t) for _, _, f, n, t in hits[:askfile.SHOWN]]
        else:
            found = read + [askfile.passage(f, n, t) for _, ok, f, n, t in hits
                            if ok][:askfile.LEAD]
            k = s["passages"] - len(found)
            if k > 0 and (not (files or read) or semantic.load_embedder() is not None):
                left = ask.BUDGET - sum(len(p["text"]) for p in found)
                seen = {p["path"] for p in found}
                # two halves: half the passages each, merged; a half leaning on the other
                # ("... and what course does he teach?") is searched with it
                for q, g, n in searches or [(query, got, k)]:
                    if searches and follow_up(q, g):
                        q = f"{q}\n{next(h for h, _ in split if h != q)}"
                    more = relatives(vault, q)  # "my sister": her name is searched too
                    also += [a for a in more if a not in also]
                    pg, st = g and g[0] <= today, {}
                    for p in ask._passages(vault, "\n".join([q, *more]), n,
                                           between=(g[0].isoformat(),
                                                    min(g[1], today).isoformat())
                                           if pg else None, sources=s["sources"],
                                           topic=topic(said if g is got else q, g[2])
                                           if g else "", stats=st):
                        if p["path"] in seen:
                            continue
                        if len(p["text"]) > left:  # the file's passages took their share
                            break
                        seen.add(p["path"])
                        left -= len(p["text"])
                        found.append(p)
                    if "matched" in st:
                        stats["matched"] = stats.get("matched", 0) + st["matched"]
                    if st.get("more"):  # C14: a full search makes the whole count a floor
                        stats["more"] = True
            if files:
                stats["matched"] = stats.get("matched", 0) + sum(h[1] for h in hits)
    vars(t).update(found=found, also=also, stats=stats, parts=parts, facts=facts, word=word,
                   max_tokens=max_tokens, front_text=front_text, model=model, runner=runner,
                   listed=listed)


def _prompt(t: SimpleNamespace) -> None:
    """Turn `t`'s `messages` for the model (facts to word, or the passages), or its answer
    when no model reads: a file's passages shown as found (saved as `kept`: their labels
    only), or NO_ANSWER."""
    vault, text, s, today, msgs, emit = t.vault, t.text, t.s, t.today, t.msgs, t.emit
    files, runner, found, parts, facts = t.files, t.runner, t.found, t.parts, t.facts
    t.messages = t.kept = None
    if t.listed is not None:
        pass
    elif t.fq is not None:
        if t.word:
            t.messages = [{"role": "system", "content": chat_facts.SYSTEM.format(
                today=f"{today:%A}, {today.isoformat()}")},
                          {"role": "user", "content": chat_facts.prompt(facts, found, text,
                                                                        today)}]
    elif files and runner is None:
        said_once = askfile.cloud_set() and not all(f.told for f in files)
        for f in files if said_once else []:
            f.told = True
        head = [askfile.LOCAL_ONLY] * said_once + [askfile.NO_MODEL]
        parts.append("\n\n".join(head + ([f"**{p['title']}** [{n}]\n{_inert(p['text'])}"
                                          for n, p in enumerate(found, 1)]
                                         or [askfile.NO_MATCH])))
        emit({"t": "tok", "s": parts[0]})
        # the saved chat names the file's passages, never their text
        t.kept = "\n\n".join(head + (["\n".join(f"- {p['title']} [{n}]" for n, p in
                                                enumerate(found, 1))] if found
                                     else [askfile.NO_MATCH]))
    elif not found and not t.front_text:
        parts.append(NO_ANSWER)
        emit({"t": "tok", "s": NO_ANSWER})
    else:
        emit({"t": "step", "s": f"Reading {len(found)} page{'s' * (len(found) != 1)} with "
                                f"{runner.model}"})
        me = about(vault, s["sources"], today)
        # a routed half's answer goes first, as data the model words with the passages
        head = _wrap("facts", "Facts computed by Keepfold:\n" + t.front_text) + "\n\n" \
            if t.front_text else ""
        t.messages = [{"role": "system",
                       "content": SYSTEM.format(today=f"{today:%A}, {today.isoformat()}") + me},
                      *history(msgs[:-1], s["sources"], len(me)),
                      {"role": "user", "content": head + ask.prompt(found, text) + CITE}]


def _stream(t: SimpleNamespace) -> None:
    """Streams turn `t`'s `messages` through its runner into `parts`: a loop cut to each line
    once, leaked tokens dropped, and asked once more when the model says nothing (or only
    [n], D20 M3)."""
    messages, runner, parts, emit, cancel = t.messages, t.runner, t.parts, t.emit, t.cancel
    if messages:
        thought = []

        def thinking():
            if not thought and not parts:
                thought.append(1)
                emit({"t": "think"})
        for attempt in range(2):  # a small model now and then says nothing: ask once more
            if attempt:
                if parts:  # only whitespace or leaked tokens came
                    parts.clear()
                    emit({"t": "text", "s": ""})
                emit({"t": "step", "s": f"{runner.model} said nothing; asking once more"})
            try:
                for piece in runner.stream(messages, temperature=t.s["temperature"],
                                           max_tokens=t.max_tokens, cancel=cancel,
                                           on_thinking=thinking):
                    parts.append(piece)
                    emit({"t": "tok", "s": piece})
                    if "\n" in piece and _looping("".join(parts)):  # stop a loop, each line once
                        parts[:] = [_once("".join(parts))]
                        emit({"t": "text", "s": parts[0]})
                        break
            except (OSError, ValueError):  # no answer, a timeout, a refused url
                pass
            said_raw = "".join(parts)
            if (said := localllm._clean(said_raw)) != said_raw.strip():  # a leaked </think>
                parts[:] = [said]
                emit({"t": "text", "s": said})
            # D20 M3: an answer of only [n] ("[3]") is no answer either
            if re.search(r"\w", ask._CITE.sub("", said)) or (cancel is not None
                                                             and cancel.is_set()):
                break


def _finish(t: SimpleNamespace, t0: float) -> None:
    """Turn `t`'s answer, its fallbacks (the facts in plain words, SAID_NOTHING) or the
    no_answer err; a model answer's [n] checked (`ask.check_cites`; changed words sent as a
    text event); then its sources, coverage, quote proof, certainty, conflicts and horizon,
    saved on the answer and sent as `src`, then `done`."""
    vault, text, s, today, msgs, emit = t.vault, t.text, t.s, t.today, t.msgs, t.emit
    cancel, convo_id, runner, found, facts = t.cancel, t.id, t.runner, t.found, t.facts
    listed, stats, also, front_text, days = t.listed, t.stats, t.also, t.front_text, t.days
    answer = "".join(t.parts).strip()
    stopped = cancel is not None and cancel.is_set()
    # only the model's words: Keepfold's own (a digest, a site list, its fallbacks) quote titles
    by_model = bool(answer) and t.messages is not None
    if not answer and not stopped and facts is not None:  # the model said nothing: plain words
        answer = chat_facts.say(facts, today)
        emit({"t": "tok", "s": answer})
    unanswered = not answer and not stopped and bool(found)
    if unanswered:  # still nothing: the pages it searched, listed below, instead of an error
        answer = SAID_NOTHING.format(model=runner.model)
        emit({"t": "tok", "s": answer})
    if not answer and not stopped:
        emit({"t": "err", "code": "no_answer", "id": convo_id,
              "message": f"{runner.describe()} did not answer; try again."})
        return
    cited = ask.cites(answer)
    if by_model:  # D20 C1-C3: the model's [n] checked against what it read, in code
        fixed, cited = ask.check_cites(answer, found, text, pages=t.fq is not None and t.fq[
            "kind"] in chat_facts.PAGE_KINDS)
        if fixed != answer:
            answer = fixed
            emit({"t": "text", "s": answer})

    def pill(p):  # titles are untrusted page text: no links, images or markup
        return {"path": p["path"], "title": chat_study._title(p["title"]) or p["path"],
                "kind": p["kind"], "at": p.get("at", 0), "len": p.get("len", 0)}
    sources = [{"n": n, **pill(p)} for n, p in enumerate(found, 1) if n in cited]
    # a small model often cites nothing: then show what it read, marked as not cited
    searched = [] if sources else [pill(p) for p in found[:SEARCHED]]
    coverage = {"read": len(found), "matched": stats["matched"],
                **({"more": True} if stats.get("more") else {}),
                **({"also": also} if also else {})} if "matched" in stats else None
    # quote proof: a quoted span must be in a cited passage (any read one when none is cited)
    unquoted = ask.unquoted(answer, [text, *(f"{p['title']}\n{p['text']}" for n, p in
                                             enumerate(found, 1) if n in cited or not sources),
                                     *(chat_facts._lines(facts, today) if facts else []),
                                     front_text]
                            ) if by_model else []
    sure = certainty(data=listed is not None or facts is not None,
                     empty=listed.startswith(_EMPTY) if listed is not None else
                     chat_facts.empty(facts) if facts is not None else not found,
                     read=len(found), cited=len(sources),
                     quoted=bool(ask.unquoted(answer, [])),  # every quote, none to match
                     unquoted=bool(unquoted), unanswered=unanswered, stopped=stopped)

    def side(p):
        return {"path": p["path"], "title": pill(p)["title"], "day": p.get("day") or ""}
    clash = [{"old": side(a), "new": side(b)} for a, b in _conflicts(
        vault, [p for n, p in enumerate(found, 1) if n in cited or not sources])]
    hz = _horizon(days if days is not None else _horizon_days(vault, s["sources"])) \
        if t.searched_pages else None
    extra = {**({"certainty": sure} if sure else {}),
             **({"horizon": {"first": hz["first"], "last": hz["last"]}} if hz else {}),
             **({"conflicts": clash} if clash else {})}
    msgs.append({"role": "assistant", "text": t.kept or answer, "ts": _now(), "sources": sources,
                 **({"searched": searched} if searched else {}),
                 **({"unquoted": unquoted} if unquoted else {}),
                 **({"facts": facts} if facts is not None else {}),
                 **({"coverage": coverage} if coverage else {}),
                 "model": t.model, "switches": dict(s["sources"]),
                 **({"unanswered": True} if unanswered else {}),
                 **({"stopped": True} if stopped else {}), **extra})
    _save(vault, convo_id, msgs, s["history"])
    emit({"t": "src", "sources": sources, "searched": searched,
          **({"unquoted": unquoted} if unquoted else {}),
          **({"coverage": coverage} if coverage else {}), **extra,
          **({"horizon": hz} if hz else {})})
    emit({"t": "done", "id": convo_id, "model": t.model, "stopped": stopped,
          "seconds": round(time.monotonic() - t0, 1),
          "remembers": remembers(msgs, s["sources"])})
