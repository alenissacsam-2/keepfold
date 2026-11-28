"""Plan 5a: the Keepfold app's Review view, read-only. What waits for the user's own click:

- pending: the lines under `## About me (unconfirmed)` in inbox.md (about-me lines and
  teaching-contract rules), each with Approve and Dismiss;
- expired: live claim lines past their own `(until …)` day (Retire);
- stale: confirmed lines not reconfirmed or rewritten in STALE_DAYS (Still true or Retire),
  at most STALE_PER_MONTH a month, oldest first;
- conflicts (plan 5b, D2): pairs of live lines that clash (Keep both, Keep first, Keep second):
  two lines that say opposite things (`opposed`), both the owner's or both inferred; two lines
  on one page that may clash (close, exactly one negated, not `opposed`; W1b); a guessed
  update pair `settle` left alone (W1b). Everything else `settle` resolves itself: lines that
  say the same facts (D1, W1 `same_facts`), recorded `possible_update_of` / `supersedes`
  pairs and an owner's line against an inferred one (D2); mcp_server.Keepfold._tidy retires the
  loser through the retire path;
- rules (plan 5b, N2): a correction the user typed in two or more imported chats, offered as a
  me/core line (keepfold.corrections; Approve or Dismiss);
- decisions (RR-9): decision-like commit subjects of imported git repos
  (more_sources.decision_proposals); Approve makes a decision page, Dismiss drops it;
- forget (R3-5): a `//forget:` the user typed that matched no line or several, with its
  candidate lines (keepfold.tags.forget_cards; Retire a line, or Dismiss);
- imports (RR-18): an imported page edited by hand whose file changed, so the re-import wrote
  a newer page beside it (keepfold.sources.edited_pages; Dismiss);
- unsafe (G6): a live claim line that reads as an instruction to an AI or holds invisible
  characters (`unsafe_lines`); it is not served until Keep it (Keep it out retires it);
- outside (R5a): a line on me/core, me/setup or the teaching contract that Keepfold did not write
  and that was not there at the baseline (`outside_lines`); not served until Keep (Remove
  retires a claim line, deletes any other);
- crowded (G7): lines the X-ray shows cut for space in most of the latest primes that offered
  them (`crowded`; Keep pins it, Retire, Skip), and unused playbooks (G7): no X-ray call served
  them and their file is UNUSED_DAYS old (`unused`; Keep asks again in UNUSED_DAYS, Open);
  together at most STALE_PER_MONTH a month (`_spare_left`);
- promote (R28a): a preferences line that keeps coming back (`reinforced`) or that prime
  serves near the top, offered for me/core (`promote`; Pin to Core, Not now), one a month.

Similarity (`scorer`): cosine with the bge embedder when this process has it loaded already
(never loaded or downloaded here), else word overlap: the Jaccard of the crudely stemmed words,
stop words and negation words left out.

The app's handlers (app/server.py) write through `mcp_server.Keepfold` with `app_clicks` set,
so every write is keepfold_patch's or keepfold_log's own code path."""

from __future__ import annotations

import datetime as dt
import functools
import hashlib
import itertools
import json
import re
import sys
import unicodedata

from keepfold import claims
from keepfold.reflect import _PROPOSED, UNCONFIRMED, UNCONFIRMED_DAYS
from keepfold.render import plain
from keepfold.vault import VaultError, is_private

STALE_DAYS = {"preferences": 180, "personality": 180, "profile": 365}
STALE_PER_MONTH = 3
QUOTED_DAYS = 30  # A21: mcp_server.GUESS_DAYS; a quoted guess this old asks Still true?
# the pages a proposal may wait for: mcp_server.CONFIRM_PAGES and, while harvesting, every
# claim page; the order is the guess for an old line with no `me/<page>` tag
PAGES = (*claims.PAGES, "portrait", "teaching-contract")
SHOWN = 300  # characters of a line shown
_CONFIRMED = re.compile(r"\(confirmed (\d{4}-\d\d-\d\d)\)")
COS = 0.72  # cosine (warm embedder) from which two lines say about the same thing
JACCARD = 0.6  # the same for word overlap, when the embedder is not loaded
SHOWN_PAIRS = 3  # conflict pairs shown at a time (D2)
INFERRED = ("agent", "exposed")  # D1/D2: the tiers of a line a chat inferred (claims.tiers)
TIDY_MAX = 200  # D1/D2: automatic retires in one sweep
TODAY_MAX = 5  # R3-4: Today's 5, cards a day
TODAY_CARDS = ("pending", "conflict", "rule")  # the kinds of card Today's 5 offers
SIGHTINGS = 2  # chats that must propose the same line before it is offered in Today's 5
SINGLE_DAYS = 14  # a line fewer chats proposed leaves the view this many days after its stamp
# RR-10: what makes Review cards, as the Status line names them
PRODUCERS = {"chats": "Lines chats suggest", "imports": "Lines from imported chats",
             "corrections": "Corrections you repeat", "decisions": "Decisions in your commits",
             "forget": "//forget cards"}
PAUSE_AFTER = 20  # decided cards before a producer whose rejects exceed its accepts pauses
LEGACY_DISMISS = ("corrections", "decisions", "forget")  # a dismiss change's id: its producer
PRIMES = ("keepfold_prime", "prime-hook")  # the X-ray rows that offer the Preferences lines
WINDOW = 40  # G7: the latest primes a crowded line is judged over
CROWD_MIN = 5  # primes that must have offered a line before it counts as crowded out
UNUSED_DAYS = 60  # G7: a playbook's file this old and served by no X-ray call; Keep's quiet days
SPARE_CARDS = ("crowded", "unused")  # the card stamps that spend the G7 monthly cap
PLAYBOOK_TAG = "playbook"  # mcp_server.PLAYBOOK_TAG (it imports this module)
REINFORCE = "reinforce"  # R13b: a change's note when a new line repeated a live one
PROMOTE = "promote_core"  # R28a: the "Pin it to Core?" card's stamp and its Not now's id
PROMOTE_PROOFS, PROMOTE_APPS = 4, 2  # sessions and AI apps a line came back in, to be offered
PROMOTE_TOP, PROMOTE_SERVED = 5, 10  # or: primes that served it in their top 5 preferences
PROMOTE_QUIET = 183  # days Not now keeps a line from being offered again
_STOP = frozenset("a an the i me my mine to of and or in on at for with is are be am it its "
                  "this that so as by from you your".split())
# a line "says no" with one of these, a `_NEG_PHRASE` or any word ending n't: never/always,
# don't/do, avoid/prefer, stopped using/uses
_NEG = frozenset("no not never none nothing nor neither without avoid avoids avoiding stop "
                 "stops stopped stopping quit quits quitting refuse refuses refused "
                 "hate hates dislike dislikes dont cannot".split())
_NEG_PHRASE = re.compile(r"\b(?:no longer|g(?:ive|ives|ave|iving|iven) up)\b")
# W1: a line that says something ended ("used to", "ex-") is never the same fact as one that
# does not; past verb forms the stemmer merges ("lived") are `_past`'s
_ENDED = re.compile(r"\b(?:used to|formerly|previously|no longer|back in \d{4})\b|\bex-")
_NUM = re.compile(r"(\d+(?:[.,]\d+)*)\s*([^\s\d]\S*|)")  # a number and the word after it
_CMP = re.compile(r"\b(rather than|instead of|over|than|before|after)\b")  # comparative order
_COMPARE = re.compile(r"(\S+)\s+(rather than|instead of|over|than|before|after)\s+"
                      r"(\S+)")  # a comparative with the word on each side (of `_words`)
_SUFFIX = re.compile(r"(?:(?<=..)ing|(?<!e)ed|es|s)$")
_VECS: dict = {}  # normalised line -> unit vector, from the embedder in _VECS_OF[0]
_VECS_OF: list = [None]


@functools.cache
def _word() -> re.Pattern:
    """R30c: a word is Unicode letters and digits (not `_`) with their combining marks (a Hindi
    matra or virama, which Python's word class leaves out) and apostrophes, so "München",
    "दिल्ली" and "東京" are words. Built on first use (about 30 ms), so importing this module
    stays cheap.
    shortcut: marks of the Basic Multilingual Plane only; all planes if a script needs it."""
    spans: list[list[int]] = []
    for c in range(0x300, 0x10000):
        if unicodedata.category(chr(c))[0] == "M":
            if spans and spans[-1][1] == c - 1:
                spans[-1][1] = c
            else:
                spans.append([c, c])
    marks = "".join(chr(a) if a == b else f"{chr(a)}-{chr(b)}" for a, b in spans)
    return re.compile(rf"(?:[^\W_]|['{marks}])+")


def _words(text: str) -> list[str]:
    return _word().findall(unicodedata.normalize("NFC", claims.norm(text)).replace("’", "'"))


def negated(text: str) -> bool:
    """Does the line say no (a `_NEG` word or phrase, or a word ending n't)?"""
    return (any(w in _NEG or w.endswith("n't") for w in _words(text))
            or bool(_NEG_PHRASE.search(" ".join(_words(text)))))


def _stem(w: str) -> str:
    """shortcut: a crude suffix strip (use, uses, using, used -> us; study, studies, studying,
    studied -> study); it misses doubled consonants (running) and irregular forms. Tense is
    `same_facts`' job, not the stemmer's. A real stemmer if pairs are missed."""
    if len(w) > 4 and w.endswith(("ies", "ied")):
        return w[:-3] + "y"
    if len(w) > 3:
        w = _SUFFIX.sub("", w)
    return w.rstrip("e") if len(w) > 2 else w


def _bag(text: str) -> frozenset:
    return frozenset(_stem(w) for w in _NEG_PHRASE.sub(" ", " ".join(_words(text))).split()
                     if w not in _STOP and w not in _NEG and not w.endswith("n't"))


def ended(text: str) -> bool:
    """W1: does the line say something ended (`_ENDED`: used to, formerly, no longer, ex-)?"""
    return bool(_ENDED.search(" ".join(_words(text))))


def _past(text: str) -> frozenset:
    """W1: the stems of the line's past verb forms, any word ending -ed (lived, used, studied).
    shortcut: an adjective (based, interested) counts too, so such lines stay two (the safe
    side); a part-of-speech tagger if that keeps real copies apart."""
    return frozenset(_stem(w) for w in _words(text) if len(w) > 3 and w.endswith("ed"))


@functools.lru_cache(maxsize=8192)  # settle compares every pair: each line is parsed once
def _facts(text: str) -> tuple:
    """W1: what `same_facts` compares: the bag, polarity, tense (`ended`, `_past`), the
    numbers with their units (a multiset), the raw `(until …)` stamp (claims.norm drops it),
    and the content words on each side of every comparative (`_CMP`)."""
    norm = " ".join(_words(text))
    m = claims._UNTIL.search(text)
    order = tuple(s if k % 2 else _bag(s) for k, s in enumerate(_CMP.split(norm)))
    return (_bag(text), negated(text), ended(text), _past(text), tuple(sorted(_NUM.findall(norm))),
            m and m.group(1), order)


def same_facts(a: str, b: str) -> bool:
    """W1: the two lines say the same thing, so Keepfold may retire one by itself: equal bags and
    the same polarity, tense, numbers, `(until …)` stamp and comparative order (`_facts`). A
    strict subset ("…peanuts" / "…peanuts and shellfish") never is."""
    return _facts(a) == _facts(b)


@functools.lru_cache(maxsize=8192)
def _swapped(text: str) -> tuple:
    """The (word before, comparative, word after) of each comparative in the line."""
    return tuple(_COMPARE.findall(" ".join(_words(text))))


# the words a `_NEG` word answers (never/always, avoid/prefer, dislike/like, hate/love): left
# out of the bags `opposed` compares, so "Always uses tabs" / "Never uses tabs" are opposites
_POLE = frozenset(map(_stem, ("always", "prefer", "like", "love")))


def opposed(a: str, b: str) -> bool:
    """W1: the two lines say opposite things: equal bags (negation words and `_POLE` left
    out) and exactly one negated, or equal bags and a comparative reversed ("Rust over Go" /
    "Go over Rust")."""
    fa, fb = _facts(a), _facts(b)
    if fa[0] - _POLE != fb[0] - _POLE:
        return False
    x, y = _swapped(a), _swapped(b)
    return fa[1] != fb[1] or (bool(x) and x != y and y == tuple((r, c, w) for w, c, r in x))


def since_for(old: dict, new: dict, day: str, newer: bool | None = None) -> str | None:
    """R13a: the `(since …)` day line `new` gets when it replaces `old` (rows with `text`, and
    `day` when `newer` is None), or None. A line saying the same facts keeps the old line's own
    since day; a newer line saying something else gets `day`; an older one (the owner's line
    kept over a later guess) none. `newer` None: `new`'s tier day is after `old`'s."""
    if same_facts(old["text"], new["text"]):
        return claims.since(old["text"])
    if newer is None:
        newer = str(new.get("day") or "") > str(old.get("day") or "")
    return day if newer else None


def apart(a: str, b: str) -> bool:
    """Each line names something the other does not ("reads news" / "reads papers", "uses
    Python" / "uses Rust"): two facts however close they score, so Keepfold never retires one
    for the other by itself (22 Nov QA: the sweep retired "Reads news every morning")."""
    x, y = _bag(a), _bag(b)
    return bool(x - y) and bool(y - x)


def _warm(texts: list[str]) -> list | None:
    """Unit vectors of `texts` from the bge embedder, only while this process has it in memory
    (`semantic.loaded_embedder`): never imports keepfold.semantic, loads a model or restarts its
    idle clock (M3). None when not in memory or it fails."""
    sem = sys.modules.get("keepfold.semantic")
    embed = sem.loaded_embedder() if sem is not None else None
    if embed is None:
        return None
    try:
        if _VECS_OF[0] is not embed or len(_VECS) > 20000:
            _VECS.clear()
            _VECS_OF[0] = embed
        if new := [t for t in dict.fromkeys(texts) if t not in _VECS]:
            _VECS.update(zip(new, sem._embed(embed, new), strict=True))
        return [_VECS[t] for t in texts]
    except Exception:  # a model error must never cost the write or the view: words serve
        return None


def scorer(texts: list[str], loose: bool = False):
    """(score(i, j), cut) over `texts`: cosine and COS with a warm embedder, else the Jaccard
    of `_bag`s and JACCARD. A pair scoring at least `cut` says about the same thing; an
    `apart` pair scores 0 unless `loose` (`collision`'s guess, which the agent settles)."""
    bags = [_bag(t) for t in texts]

    def off(i: int, j: int) -> bool:
        return not loose and bool(bags[i] - bags[j]) and bool(bags[j] - bags[i])

    vecs = _warm([claims.norm(t) for t in texts])
    if vecs is not None:
        return (lambda i, j: 0.0 if off(i, j) else float(vecs[i] @ vecs[j])), COS
    return (lambda i, j: 0.0 if off(i, j)
            else len(bags[i] & bags[j]) / (len(bags[i] | bags[j]) or 1)), JACCARD


def collision(vault, page: str, text: str, today: str) -> dict | None:
    """The live line of a non-private claim page (on a tie, `page`'s own) that `text`, a line
    about to be appended to me/<page>, most likely updates: {page, id, text (raw note text),
    apart}, or None under the cut (`scorer`, loose). An `apart` guess is left to the agent:
    `settle` never retires either line for it."""
    hidden = _private(vault)
    live = [r for r in claims.rows(vault) if claims.is_live(r, today) and r["page"] not in hidden]
    if not live or not claims.norm(text):
        return None
    score, cut = scorer([text, *(r["text"] for r in live)], loose=True)
    k = max(range(len(live)), key=lambda k: (score(0, k + 1), live[k]["page"] == page))
    if score(0, k + 1) < cut:
        return None
    return {"page": live[k]["page"], "id": live[k]["id"], "text": live[k]["text"],
            "apart": apart(text, live[k]["text"])}


def pair_key(a: dict, b: dict) -> str:
    """A conflict pair's key: both `page/id`, sorted, so either order is the same pair."""
    return claims.pair_key((a["page"], a["id"]), (b["page"], b["id"]))


def owned(r: dict) -> bool:
    """D1/D2: the owner's own line (typed, legacy, or confirmed/approved), not a chat's
    inference (`INFERRED`)."""
    return r.get("tier") not in INFERRED


def rank(r: dict) -> tuple:
    """D1: of near-duplicate lines the highest stays: the owner's over an inferred one, then
    the newer (its tier's day), then the longer, then the later line on its page."""
    return (owned(r), str(r.get("day") or ""), len(claims.norm(r["text"])), r.get("line_no", 0))


def known_texts(vault) -> dict:
    """(page, id) -> text of every claim line, any status: what `kept_cover` compares with."""
    return {(r["page"], r["id"]): r["text"] for r in claims.rows(vault)}


def kept_cover(rows: list[dict], events, known: dict | None = None):
    """D5: (covered(i, j), score, cut, no) over `rows`: covered when the user kept both of
    that pair (a `keep_both` change) or of a pair whose two lines are each near-duplicates of
    the two kept ones (`known`: `known_texts`)."""
    _, kept = claims.recorded_pairs(events)
    known = known or {}
    sides = [[tuple(s.split("/", 1)) for s in k.split("|")] for k in kept]
    sides = [s for s in sides if len(s) == 2 and all(x in known for x in s)]
    texts = [r["text"] for r in rows] + [known[x] for s in sides for x in s]
    score, cut = scorer(texts)
    no = [negated(t) for t in texts]
    n = len(rows)

    def near(i: int, k: int) -> bool:  # W1: only a line saying the same facts
        return score(i, k) >= cut and same_facts(texts[i], texts[k])

    def covered(i: int, j: int) -> bool:
        if pair_key(rows[i], rows[j]) in kept:
            return True
        return any((near(i, a) and near(j, a + 1)) or (near(i, a + 1) and near(j, a))
                   for a in range(n, len(texts), 2))

    return covered, score, cut, no


def settle(rows: list[dict], events, known: dict | None = None, skip=frozenset()) -> dict:
    """D1, D2, D5 over `rows` (tiered live claim rows, none private): {retire: [{retire, keep,
    why, newer}] with raw rows, cards: [{key, why, left, right}]} (why `opposite`, `maybe_update`
    or `maybe_opposite`). `newer` (R13a, `since_for`): whether `keep` is a recorded pair's newer
    line; None when no pair says.
    - a recorded pair (claims.recorded_pairs) whose lines agree: the newer stays, unless the
      older is the owner's and the newer inferred (why `update` or `supersedes`); a guessed
      `update` pair is applied only when `same_facts` or `opposed` (W1), else it is a card
      (why `maybe_update`, the older line left), so prime.held's hold can end;
    - lines on one page that say the same facts (`scorer` cut and `same_facts`): the one
      `rank` puts first stays (why `duplicate`), so n copies are one line;
    - two of the lines left that are `opposed` (or a recorded pair exactly one of which is
      negated): the owner's stays over an inferred one (why `opposite`); two of the owner's or two
      inferred are a card, one per pair of near-copy groups;
    - W1b: two lines left on one page, at least the `scorer` cut, exactly one negated but not
      `opposed` ("Likes spicy food" / "Doesn't like very spicy Thai food"): a card (why
      `maybe_opposite`) whoever wrote them, never retired by itself.
    `conflicts` shows SHOWN_PAIRS cards at a time. A pair the user kept both of (`kept_cover`)
    or whose key is in `skip` is left alone, and so is a line whose `page/id` is in `skip`
    (an automatic retire the user undid)."""
    events = list(events)
    covered, score, cut, no = kept_cover(rows, events, known)
    recorded, _ = claims.recorded_pairs(events)
    at = {(r["page"], r["id"]): i for i, r in enumerate(rows)}
    out, gone, forced = [], set(), []

    def free(i: int, j: int) -> bool:
        return pair_key(rows[i], rows[j]) not in skip and not covered(i, j)

    def kept(i: int) -> bool:  # the user undid Keepfold retiring this line: never again
        return f"{rows[i]['page']}/{rows[i]['id']}" in skip

    def drop(i: int, k: int, why: str, newer: bool | None = None) -> None:
        out.append({"retire": rows[i], "keep": rows[k], "why": why, "newer": newer})
        gone.add(i)

    asks = []  # W1b: guessed updates left alone: a card, so prime.held's hold on the older ends
    for old, new, why in recorded.values():
        o, n = at.get(old), at.get(new)
        if o is None or n is None or o in gone or n in gone or not free(o, n):
            continue
        a, b = rows[o]["text"], rows[n]["text"]
        if why == "update" and not (same_facts(a, b) or opposed(a, b)):
            asks.append((o, n))  # W1: a guessed pair that would lose a fact: both stay
            continue
        if no[o] != no[n]:
            forced.append((o, n))
        elif owned(rows[o]) and not owned(rows[n]):
            if not kept(n):
                drop(n, o, why, False)
        elif not kept(o):
            drop(o, n, why, True)
    rep: dict[int, int] = {}  # a line -> the kept line it near-duplicates
    keep: list[int] = []
    for i in sorted((i for i in range(len(rows)) if i not in gone),
                    key=lambda i: rank(rows[i]), reverse=True):
        k = next((k for k in keep if rows[k]["page"] == rows[i]["page"] and score(i, k) >= cut
                  and same_facts(rows[i]["text"], rows[k]["text"]) and free(i, k)), None)
        if k is None or kept(i):
            keep.append(i)
        else:
            rep[i] = k
            drop(i, k, "duplicate")
    keep.sort()
    pairs = [(rep.get(a, a), rep.get(b, b)) for a, b in forced]
    by: dict[frozenset, list[int]] = {}  # opposed lines share their bag less `_POLE`
    for a in keep:
        by.setdefault(_facts(rows[a]["text"])[0] - _POLE, []).append(a)
    pairs += sorted((a, b) for g in by.values() for x, a in enumerate(g) for b in g[x + 1:]
                    if opposed(rows[a]["text"], rows[b]["text"]))
    cards, seen = [], set()
    for a, b in pairs:
        if a == b or a in gone or b in gone or frozenset((a, b)) in seen or not free(a, b):
            continue
        seen.add(frozenset((a, b)))
        if owned(rows[a]) != owned(rows[b]):
            lose, win = (b, a) if owned(rows[a]) else (a, b)
            if not kept(lose):
                drop(lose, win, "opposite")
        else:
            cards.append((a, b, "opposite"))
    for a, b in asks:  # older left, newer right: the app's Y keeps the right line
        if frozenset((a, b)) not in seen:
            seen.add(frozenset((a, b)))
            cards.append((a, b, "maybe_update"))
    # W1b: lines on one page as close as `scorer`'s cut, exactly one negated, not `opposed`:
    # they may clash, so a card whoever wrote them, never a retire
    sides: dict[str, tuple] = {}
    for a in keep:
        if a not in gone:
            sides.setdefault(rows[a]["page"], ([], []))[no[a]].append(a)
    for yes, nay in sides.values():
        for a, b in sorted(tuple(sorted(p)) for p in itertools.product(yes, nay)):
            if (frozenset((a, b)) not in seen and score(a, b) >= cut and free(a, b)
                    and not opposed(rows[a]["text"], rows[b]["text"])):
                seen.add(frozenset((a, b)))
                cards.append((a, b, "maybe_opposite"))
    return {"retire": out, "cards": [
        {"key": pair_key(rows[a], rows[b]), "why": why, "left": rows[a], "right": rows[b]}
        for a, b, why in cards if a not in gone and b not in gone]}


def conflicts(rows: list[dict], events: list[dict], known: dict | None = None) -> list[dict]:
    """D2: up to SHOWN_PAIRS pairs of `rows` (live, shown claim rows) that clash and Keepfold
    does not settle itself (`settle`'s cards: an opposite pair of two of the owner's lines or
    two inferred ones, a guessed update left alone, a pair that may clash), as {key, why,
    left, right} with raw rows; Keep first or second retires the other line, Keep both
    covers near-copies of the pair too (`kept_cover`)."""
    return settle(rows, events, known)["cards"][:SHOWN_PAIRS]


def copies(rows: list[dict], r: dict) -> list[dict]:
    """D5: `r` and the `rows` on its page that say the same (the `scorer` cut, W1
    `same_facts`), `r` first: what Keep first or second on a conflict card retires."""
    same = [x for x in rows if x["page"] == r["page"] and x["id"] != r["id"]
            and same_facts(x["text"], r["text"])]  # W1: never a line holding another fact
    score, cut = scorer([r["text"], *(x["text"] for x in same)])
    return [r, *(x for i, x in enumerate(same, 1) if score(0, i) >= cut)]


def collapse(items: list[dict], text=lambda x: x["text"]) -> list[dict]:
    """D5: near-identical `items` (the `scorer` cut, W1 `same_facts`) as one: the first of each
    group, with `members` (the group, itself first). A click on it covers every member."""
    if not items:
        return []
    texts = [text(x) for x in items]
    score, cut = scorer(texts)
    groups: list[list[int]] = []
    for i in range(len(items)):
        g = next((g for g in groups if score(g[0], i) >= cut
                  and same_facts(texts[g[0]], texts[i])), None)  # W1: the same facts only
        if g is None:
            groups.append([i])
        else:
            g.append(i)
    return [{**items[g[0]], "members": [items[i] for i in g]} for g in groups]


def group(items: list[dict], want, field: str = "key", text=lambda x: x["text"]) -> list:
    """D5: the `field` values of the `collapse` group of `items` holding the item whose `field`
    is `want`, that one first; [want] when none holds it."""
    for g in collapse(items, text):
        got = [m[field] for m in g["members"]]
        if want in got:
            return [want, *(x for x in got if x != want)]
    return [want]


def _spare_left(events, today: str) -> int:
    """G7: how many crowded and unused cards this month still has: STALE_PER_MONTH minus the
    clicks this month stamped with a SPARE_CARDS card (one per click, its session)."""
    voided, said = _voided(events), set()
    for e in events:
        c = _payload(e).get("change") if e.get("kind") == "change" else None
        if (isinstance(c, dict) and c.get("card") in SPARE_CARDS and e.get("id") not in voided
                and str(e.get("day") or "")[:7] == today[:7]):
            said.add(e.get("session"))
    return max(0, STALE_PER_MONTH - len(said))


def _ids(blocks) -> set[str]:
    """The ids of an X-ray row's `served` or `trimmed` {block: [ids]}, whatever its shape."""
    return {i for v in (blocks.values() if isinstance(blocks, dict) else ())
            for i in (v if isinstance(v, list) else ()) if isinstance(i, str)}


def crowded(vault, rows: list[dict]) -> tuple[list[dict], int]:
    """G7: (the `rows` (live, shown claim rows) the X-ray shows cut for space in most of the
    latest WINDOW primes that offered them, at least CROWD_MIN, as {page, id, text (raw), cut,
    seen} most cut first; how many primes the window holds). A pinned line (Keep pins it) or a
    muted one is left out."""
    from collections import Counter

    from keepfold import xray
    primes = [r for r in xray.rows(vault) if r.get("tool") in PRIMES][-WINDOW:]
    seen, cut = Counter(), Counter()
    for r in primes:
        s, t = _ids(r.get("served")), _ids(r.get("trimmed"))
        seen.update(s | t)
        cut.update(t - s)
    xs = xray.settings(vault)
    flagged = xs["pin"] | xs["mute"]
    out = [{"page": r["page"], "id": r["id"], "text": r["text"], "cut": cut[c], "seen": seen[c]}
           for r in rows if (c := xray.claim(r["id"])) not in flagged
           and seen[c] >= CROWD_MIN and 2 * cut[c] > seen[c]]
    out.sort(key=lambda x: (-x["cut"] / x["seen"], x["page"], x["id"]))
    return out, len(primes)


def unused(vault, events, today: str) -> tuple[list[dict], int, str | None]:
    """G7: (the playbooks (`note` pages tagged PLAYBOOK_TAG, none private or muted) that no
    X-ray call served and whose file is over UNUSED_DAYS old, not kept (a `dismiss` change on
    id `playbooks` holding the id's hash) in the last UNUSED_DAYS, oldest first, as {id, title,
    path (vault-relative), changed}; the X-ray calls seen; the first one's day). Nothing while
    the X-ray holds no call. R27b: a keepfold_find, keepfold_recall or keepfold_read that returned a
    playbook counts as serving it too, which only ever keeps a playbook off this list."""
    from keepfold import xray
    calls = xray.rows(vault)
    if not calls:
        return [], 0, None
    served = {i for r in calls for i in _ids(r.get("served"))}
    since = (dt.date.fromisoformat(today) - dt.timedelta(days=UNUSED_DAYS)).isoformat()
    voided, kept = _voided(events), set()
    for e in events:
        c = _payload(e).get("change") if e.get("kind") == "change" else None
        if (isinstance(c, dict) and c.get("tool") == "dismiss" and c.get("id") == "playbooks"
                and e.get("id") not in voided and str(e.get("day") or "") > since):
            kept.add(c.get("hash"))
    mute = xray.settings(vault)["mute"]
    out = []
    for n in vault.iter_notes("note"):
        tags = n.fields.get("tags")
        if (PLAYBOOK_TAG not in (tags if isinstance(tags, list) else [tags]) or is_private(n)
                or n.id in mute or n.id in served or text_hash(n.id) in kept):
            continue
        changed = dt.date.fromtimestamp(n.path.stat().st_mtime).isoformat()
        if changed < since:
            out.append({"id": n.id, "title": plain(str(n.fields.get("title") or n.id), SHOWN),
                        "path": n.path.relative_to(vault.root).as_posix(), "changed": changed})
    out.sort(key=lambda x: (x["changed"], x["id"]))
    try:  # the oldest call's Keepfold day
        from keepfold.events import local_day
        first = local_day(dt.datetime.fromisoformat(str(calls[0].get("ts"))))
    except (TypeError, ValueError):
        first = None
    return out, len(calls), first


def text_hash(text: str) -> str:
    """A dismissed line's mark in its `dismiss` change event: never the text itself."""
    return hashlib.sha256(claims.norm(text).encode()).hexdigest()[:16]


def _voided(events: list[dict]) -> set:
    return {e.get("ref") for e in events if e.get("kind") == "void"}


def _payload(e: dict) -> dict:
    return e["payload"] if isinstance(e.get("payload"), dict) else {}


def quoted(events) -> set[str]:
    """A21: the claim ids the fact harvest wrote (a change stamped with its `imports` producer
    and the `chat` it read): each line's quote is the user's own words, verbatim in a turn of
    theirs (fact_harvest.said). Not undone ones."""
    events = list(events)
    voided, out = _voided(events), set()
    for e in events:
        c = _payload(e).get("change") if e.get("kind") == "change" else None
        if (isinstance(c, dict) and e.get("id") not in voided and c.get("producer") == "imports"
                and c.get("chat") and c.get("claim")):
            out.update(c["claim"] if isinstance(c["claim"], list) else [c["claim"]])
    return out


def dismissed(events, page: str, text: str) -> bool:
    """True when Dismiss in the app dropped `text` for me/<page> (a `dismiss` change event)."""
    events, h = list(events), text_hash(text)
    voided = _voided(events)
    for e in events:
        c = _payload(e).get("change") if e.get("kind") == "change" else None
        if (isinstance(c, dict) and c.get("tool") == "dismiss" and c.get("id") == page
                and c.get("hash") == h and e.get("id") not in voided):
            return True
    return False


def unsafe_lines(vault, events) -> list[dict]:
    """G6: the live claim lines (private pages too) whose text or section claims.unsafe flags
    and Keep has not kept, as {key, page, id, section, text (raw), what}. Keep is a `dismiss`
    change on id `unsafe` holding the key's hash (`dismissed`); the key hashes the page and the
    exact line, so a line edited since is checked again.
    G5b-6: also a line whose text or section claims.command flags, when an AI app wrote it
    (tier agent or exposed, or confirmed by a yes other than the owner's click or tag); its row
    has `command: True`. The tier fold runs only when such a line is found."""
    events = list(events)
    voided, kept = _voided(events), set()
    for e in events:
        c = _payload(e).get("change") if e.get("kind") == "change" else None
        if (isinstance(c, dict) and c.get("tool") == "dismiss" and c.get("id") == "unsafe"
                and e.get("id") not in voided):
            kept.add(c.get("hash"))
    out, sections = [], {}
    for r in claims.rows(vault):
        if r["status"] != "live":
            continue
        what = claims.unsafe(r["text"], r["page"]) or claims.unsafe(r["section"], r["page"])
        if not what and r["section"] not in sections:
            sections[r["section"]] = claims.command(r["section"])
        cmd = None if what else claims.command(r["text"]) or sections[r["section"]]
        if not (what or cmd):
            continue
        key = hashlib.sha256(f"{r['page']}\n{r['section']}\n{r['text']}".encode()).hexdigest()[:16]
        if text_hash(key) not in kept:
            out.append({"key": key, "page": r["page"], "id": r["id"], "section": r["section"],
                        "text": r["text"], "what": what or cmd, **({"command": True} if cmd
                                                                   else {})})
    if any(x.get("command") for x in out):
        by = {(t["page"], t["id"]): t for t in claims.tiers(vault, events)}
        out = [x for x in out if not x.get("command") or _by_app(by.get((x["page"], x["id"])))]
    return out


def _by_app(t: dict | None) -> bool:
    """G5b-6: a claims.tiers row an AI app wrote: not the owner's (a click or a //pref or
    //fact tag), nor a typed or legacy line."""
    return bool(t) and (t["tier"] in INFERRED or (
        t["tier"] == "confirmed" and t["confirm"] not in ("clicked", "tagged")))


# R5a: the pages every AI app takes as standing orders. A line on them that Keepfold did not write
# and the owner did not keep is "changed outside Keepfold" and is not served until Keep.
GUARDED = ("core", "setup", "teaching-contract")
OUTSIDE = "outside"  # the card's type and its Keep's `dismiss` id
GUARD_FILE = ".keepfold/guard-baseline.json"  # the guarded lines present at the upgrade


def line_hash(line: str) -> str:
    """R5a: a guarded line's mark: the line without its claim id, whitespace collapsed; case
    and `(confirmed …)`-style markers count, so any edit makes a new line."""
    return hashlib.sha256(" ".join(claims.strip_id(line).split()).encode()).hexdigest()[:16]


def line_key(page: str, line: str) -> str:
    """R5a: a guarded line's card key, also its mark in prime.held's set."""
    return f"{page}:{line_hash(line)}"


def guarded_lines(vault) -> list[dict]:
    """R5a: every line an AI app may be served from the GUARDED pages, as {page, section,
    line, id (a claim row's, else None), key}: on me/core and me/setup the non-blank lines of
    each `## ` section but Past and Not me, its heading included (keepfold_context serves a
    section's other lines too); on the contract every non-blank line, front matter too."""
    out = []
    for page in GUARDED:
        p = vault.root / "me" / f"{page}.md"
        text = p.read_bytes().decode("utf-8") if p.is_file() else ""
        claim = page in claims.PAGES
        ids = {r["line_no"]: r["id"] for r in claims.parse(page, text)} if claim else {}
        section = None
        for no, ln in enumerate(text.splitlines()):
            if claim and (m := claims._H2.match(ln)):
                section = m.group(1)
            if not ln.strip() or claim and (section is None
                                            or section.lower() in claims.RESERVED):
                continue
            out.append({"page": page, "section": section, "line": ln.rstrip(),
                        "id": ids.get(no), "key": line_key(page, ln)})
    return out


def _baseline(vault) -> dict | None:
    """R5a: GUARD_FILE's {page: line hashes}, None when it is missing or unreadable."""
    try:
        got = json.loads((vault.root / GUARD_FILE).read_bytes())
    except (OSError, ValueError):
        return None
    return got.get("lines") if isinstance(got, dict) and isinstance(got.get("lines"),
                                                                   dict) else None


def guard(vault, events) -> tuple[dict | None, set]:
    """R5a: ({page: line hashes Keepfold wrote or found at the baseline}, None before the
    baseline is taken; the hashes of the kept card keys). Written: an unvoided change on a
    GUARDED page listing its `lines` (keepfold_patch), or a retire's `kept` line it stamped with
    `(since …)` (R13a); kept: a `dismiss` change on id OUTSIDE."""
    base = _baseline(vault)
    events = list(events)
    voided, ok, kept = _voided(events), {p: set() for p in GUARDED}, set()
    for e in events:
        c = _payload(e).get("change") if e.get("kind") == "change" else None
        if not isinstance(c, dict) or e.get("id") in voided:
            continue
        if c.get("id") in GUARDED and isinstance(c.get("lines"), list):
            ok[c["id"]].update(h for h in c["lines"] if isinstance(h, str))
        elif c.get("tool") == "dismiss" and c.get("id") == OUTSIDE:
            kept.add(c.get("hash"))
        k = c.get("kept")  # R13a: the line a retire stamped `(since …)`, as Keepfold wrote it
        if isinstance(k, dict) and k.get("page") in GUARDED and isinstance(k.get("line"), str):
            ok[k["page"]].add(k["line"])
    if base is None:
        return None, kept
    for p in GUARDED:
        if isinstance(got := base.get(p), list):
            ok[p].update(h for h in got if isinstance(h, str))
    return ok, kept


def outside_lines(vault, events) -> list[dict]:
    """R5a: the `guarded_lines` changed outside Keepfold: neither written by Keepfold, present at
    the baseline nor kept; [] before the baseline is taken. A removed line needs no guard."""
    ok, kept = guard(vault, events)
    if ok is None:
        return []
    return [x for x in guarded_lines(vault) if line_hash(x["line"]) not in ok[x["page"]]
            and text_hash(x["key"]) not in kept]


def record_baseline(vault) -> bool:
    """R5a, first run: with no GUARD_FILE yet, every guarded line present now is written there
    as Keepfold's own (through keepfold.vault, under its lock), so nothing the owner has on upgrade
    is withheld. A cache file, as the X-ray's, never a note; True when it wrote one.
    shortcut: a deleted file is taken again from the pages as they are then; an event in the
    log if a program that deletes it ever matters (it could edit the log as well)."""
    path = vault.root / GUARD_FILE
    if path.is_file():
        return False
    with vault.lock():
        if path.is_file():  # another process took it meanwhile
            return False
        lines: dict[str, set[str]] = {p: set() for p in GUARDED}
        for x in guarded_lines(vault):
            lines[x["page"]].add(line_hash(x["line"]))
        vault.write_text(path, json.dumps({"lines": {p: sorted(h) for p, h in lines.items()}}))
    return True


def _private(vault) -> set[str]:
    return {n.id for n in vault.iter_notes("me") if is_private(n)}


def _page_of(vault, section: str) -> str:
    """An old line with no `me/<page>` tag: the first page with its `## section`, a YYYY-MM
    section the portrait, else preferences."""
    if re.fullmatch(r"\d{4}-\d\d", section):
        return "portrait"
    want = f"## {section}".casefold()
    for page in PAGES:
        p = vault.root / "me" / f"{page}.md"
        if p.is_file() and want in (ln.strip().casefold()
                                    for ln in p.read_bytes().decode("utf-8").splitlines()):
            return page
    return "preferences"


def pending(vault, today: str) -> list[dict]:
    """The waiting lines as {key, page, section, text, since} with raw note text, one per
    (page, section, text) with its latest stamp. Left out as reflect.unconfirmed leaves them
    (stamped over UNCONFIRMED_DAYS ago), and lines with no `<section>: ` or for a private page."""
    p = vault.root / "inbox.md"
    cut = (dt.date.fromisoformat(today) - dt.timedelta(days=UNCONFIRMED_DAYS)).isoformat()
    hidden, out, on = _private(vault), {}, False
    for ln in p.read_bytes().decode("utf-8").splitlines() if p.is_file() else []:
        if ln.startswith("#"):
            on = ln.strip() == UNCONFIRMED
            continue
        if not on or not ln.startswith("- "):
            continue
        m = _PROPOSED.search(ln)
        section, sep, text = _PROPOSED.sub("", ln)[2:].strip().partition(": ")
        section, text, since = section.strip(), text.strip(), m.group(1) if m else None
        if not (sep and section and text) or (since and since < cut):
            continue
        page = (m and m.group(2)) or _page_of(vault, section)
        if page not in PAGES or page in hidden:
            continue
        key = hashlib.sha256(f"{page}\n{claims.norm(section)}\n{claims.norm(text)}".encode()
                             ).hexdigest()[:16]
        if key not in out or (since or "") > (out[key]["since"] or ""):
            out[key] = {"key": key, "page": page, "section": section, "text": text,
                        "since": since}
    return list(out.values())


def _shown(text: str) -> str:
    """Note text for the page: no `(confirmed …)`-style markers, links, images or HTML."""
    return plain(claims._MARK.sub("", text), SHOWN)


def reinforce(log, session: str, page: str, claim: str, client: str | None = None) -> None:
    """R13b: a new line repeated live line `claim` of me/<page> (a near-copy dropped at write
    time): a `change` event {note: reinforce, page, claim, client} in `session`. `client` is
    the AI app as the server knows it (the MCP handshake), never tool input; id, day and
    session are stamped by the log. History only (`reinforced` folds it); best effort."""
    try:
        log.append(session=session, kind="change", source="tutor", fields={"payload": {
            "note": REINFORCE, "page": page, "claim": claim,
            **({"client": client[:100]} if client else {})}})
    except VaultError:
        pass  # a sighting is never worth failing the write it rides on


def reinforced(events, known: dict | None = None) -> dict[str, dict]:
    """R13b: {claim id: {sessions, apps, last}} for each line that came back: a `reinforce`
    change names it, or Keepfold retired a near-copy of it (an `auto` retire whose `kept` it is:
    why `duplicate`, or why `update` when `known` (`known_texts`) shows the two lines say the
    same facts, as a repeat at write time is recorded; not a swapped comparative). A sighting
    is the session that said the line again (for a retired copy, the session that wrote the
    copy; a copy no change wrote is not counted), its AI app (the event's `client`, else its
    session's `open` one) and its day. A kept line takes over its retired copy's sightings, so
    a line said again and again keeps adding up; once a line has one, its own write counts
    too. `len(sessions)` is its proofs, `len(apps)` its apps, `days` {session: its first day}
    (for `use`)."""
    events = list(events)
    voided, apps, wrote, out = _voided(events), {}, {}, {}

    def saw(cid, by) -> dict | None:
        if not isinstance(cid, str) or by is None or not by[1]:
            return None
        x = out.setdefault(cid, {"sessions": set(), "apps": set(), "last": "", "days": {}})
        sid, day, app = by
        x["sessions"].add(sid)
        x["days"][sid] = min(day, x["days"].get(sid, day))
        if app := app or apps.get(sid):
            x["apps"].add(app)
        x["last"] = max(x["last"], day)
        return x

    for e in events:
        if e.get("id") in voided:
            continue
        pl, sid, day = _payload(e), e.get("session"), str(e.get("day") or "")
        if e.get("kind") == "open" and isinstance(pl.get("client"), str):
            apps[sid] = pl["client"]
        if e.get("kind") != "change":
            continue
        if pl.get("note") == REINFORCE:
            saw(pl.get("claim"), (sid, day, pl.get("client")))
        c = pl.get("change")
        if not isinstance(c, dict):
            continue
        if c.get("tier") and c.get("claim"):
            for cid in c["claim"] if isinstance(c["claim"], list) else [c["claim"]]:
                wrote[cid] = (sid, day, None)
        if c.get("auto") and isinstance(k := c.get("kept"), dict) and (
                c.get("why") == "duplicate" or c.get("why") == "update" and _same(
                    known, (c.get("id"), c.get("claim")), (k.get("page"), k.get("claim")))):
            copy = c.get("claim")
            x = saw(c["kept"].get("claim"), wrote.get(copy) if isinstance(copy, str) else None)
            if x is not None and (y := out.pop(copy, None)) is not None:
                x["sessions"] |= y["sessions"]
                for s, d in y["days"].items():
                    x["days"][s] = min(d, x["days"].get(s, d))
                x["apps"] |= y["apps"]
                x["last"] = max(x["last"], y["last"])
    for cid in list(out):
        saw(cid, wrote.get(cid))
    return out


def use(vault, events, before: str | None = None, known: dict | None = None,
        back: dict | None = None) -> dict[str, dict]:
    """G5b-4: {claim id: {n, last}}: how often a claim line was used and the last day it was:
    each X-ray call that served it to an AI app, each yes reconfirm and each time it came back
    (`back`: `reinforced` over `events`, folded here when not given; `known`: `known_texts`,
    read when not given). With `before` (an ISO day) only days before it, so prime's order from
    it stays the same all day. A fold over the X-ray ring and the events; nothing stored.
    shortcut: the ring keeps xray.CAP calls, and a tidy's retire hands a copy's past sightings
    to the kept line, so either during the day can move a line across a bucket edge
    (prime.use_order); snapshot the fold per day if caches miss."""
    from keepfold import xray
    from keepfold.events import local_day

    out: dict[str, dict] = {}
    events = list(events)
    if back is None:
        back = reinforced(events, known_texts(vault) if known is None else known)

    def add(cid, day: str) -> None:
        if isinstance(cid, str) and day and (before is None or day < before):
            x = out.setdefault(cid, {"n": 0, "last": ""})
            x["n"] += 1
            x["last"] = max(x["last"], day)

    for r in xray.rows(vault):
        try:
            day = local_day(dt.datetime.fromisoformat(str(r.get("ts"))))
        except (TypeError, ValueError):
            continue
        for i in _ids(r.get("served")):
            if i.startswith("^"):
                add(i[1:], day)
    events = [e for e in events if before is None or str(e.get("day") or "") < before]
    voided = _voided(events)
    for e in events:
        pl = _payload(e)
        if (e.get("kind") == "log" and pl.get("kind") == "reconfirm" and pl.get("verdict") == "yes"
                and e.get("id") not in voided):
            add(pl.get("claim"), str(e.get("day") or ""))
    for cid, x in back.items():
        for day in x["days"].values():
            add(cid, day)
    return out


def _same(known: dict | None, a: tuple, b: tuple) -> bool:
    """Do lines `a` and `b` ((page, id) in `known`) say the same facts, markers aside (a
    retired line carries its `(until …)`)?"""
    x, y = (known or {}).get(a), (known or {}).get(b)
    return x is not None and y is not None and same_facts(claims._MARK.sub("", x),
                                                          claims._MARK.sub("", y))


def _clicked_this_month(events, card: str, today: str) -> bool:
    """A click this calendar month whose change the app stamped with `card`."""
    voided = _voided(events)
    return any(isinstance(c := _payload(e).get("change"), dict) and c.get("card") == card
               and e.get("kind") == "change" and e.get("id") not in voided
               and str(e.get("day") or "")[:7] == today[:7] for e in events)


def promote(vault, rows: list[dict], events, today: str) -> list[dict]:
    """R28a: the "Pin it to Core?" card, at most one a calendar month (none once a click this
    month was stamped PROMOTE): the owner's live preferences line (confirmed, typed or legacy;
    not on me/core, not X-ray muted, not put off with Not now in the last PROMOTE_QUIET days: a
    `dismiss` change on id PROMOTE holding its id's hash) that came back in PROMOTE_PROOFS
    sessions from PROMOTE_APPS AI apps (`reinforced`), or that prime served in its top
    PROMOTE_TOP Preferences lines PROMOTE_SERVED times (the X-ray's rows). Most proofs first,
    then most served. As [{page, id, text (raw), proofs, apps, served}]."""
    from keepfold import xray
    events = list(events)
    if _clicked_this_month(events, PROMOTE, today):
        return []
    since = (dt.date.fromisoformat(today) - dt.timedelta(days=PROMOTE_QUIET)).isoformat()
    voided, later = _voided(events), set()
    for e in events:
        c = _payload(e).get("change") if e.get("kind") == "change" else None
        if (isinstance(c, dict) and c.get("tool") == "dismiss" and c.get("id") == PROMOTE
                and e.get("id") not in voided and str(e.get("day") or "") > since):
            later.add(c.get("hash"))
    core = {claims.norm(r["text"]) for r in rows if r["page"] == "core"}
    top: dict[str, int] = {}
    for r in xray.rows(vault):
        got = r.get("served")
        prefs = got.get("preferences") if isinstance(got, dict) else None
        if r.get("tool") in PRIMES and isinstance(prefs, list):
            for i in prefs[:PROMOTE_TOP]:
                top[i] = top.get(i, 0) + 1
    back, mute = reinforced(events, known_texts(vault)), xray.settings(vault)["mute"]
    out = []
    for r in rows:
        x = back.get(r["id"], {})
        proofs, apps, served = len(x.get("sessions", ())), len(x.get("apps", ())), top.get(
            xray.claim(r["id"]), 0)
        if (r["page"] == "preferences" and r.get("tier") in ("confirmed", "legacy", "typed")
                and claims.norm(r["text"]) not in core and xray.claim(r["id"]) not in mute
                and text_hash(r["id"]) not in later
                and ((proofs >= PROMOTE_PROOFS and apps >= PROMOTE_APPS)
                     or served >= PROMOTE_SERVED)):
            out.append({"page": r["page"], "id": r["id"], "text": r["text"], "proofs": proofs,
                        "apps": apps, "served": served})
    out.sort(key=lambda x: (-x["proofs"], -x["served"], x["id"]))
    return out[:1]


def stale(rows: list[dict], events: list[dict], today: str, known: dict | None = None
          ) -> list[dict]:
    """Live confirmed lines on the STALE_DAYS pages whose last confirm (a `(confirmed DAY)`
    marker, the confirming change event, a yes reconfirm or, R13b, the line coming back
    (`reinforced`'s last day, `known`: `known_texts`)) is older than the page's days, oldest
    first; at most STALE_PER_MONTH minus this month's Review answers (clicked
    reconfirms and retires; shortcut: an expired line retired in the Review counts too). D5:
    near-identical lines are one card, its `ids` every member's (Still true or Retire covers
    them all). A21: also each live guess the fact harvest quoted (`quoted`) written over
    QUOTED_DAYS ago, `since` its day and `guess` true (never confirmed), in place of G5b-2's
    silent retire."""
    voided, said, answered = _voided(events), {}, 0
    for e in events:
        if e.get("id") in voided:
            continue
        pl, day = _payload(e), str(e.get("day") or "")
        this_month = day[:7] == today[:7]
        if e.get("kind") == "log" and pl.get("kind") == "reconfirm":
            if pl.get("verdict") == "yes":
                said[pl.get("claim")] = max(said.get(pl.get("claim"), ""), day)
            answered += this_month and pl.get("confirm") == "clicked"
        c = pl.get("change") if e.get("kind") == "change" else None
        if isinstance(c, dict) and c.get("op") == "retire" and c.get("confirm") == "clicked":
            answered += this_month
    out, back = [], reinforced(events, known)
    asked = quoted(events)
    guess_cut = (dt.date.fromisoformat(today) - dt.timedelta(days=QUOTED_DAYS)).isoformat()
    for r in rows:
        days = STALE_DAYS.get(r["page"])
        if days is None or claims.expired(r["text"], today):
            continue
        if r["tier"] in INFERRED:  # A21: a quoted guess G5b-2 no longer retires
            if r["id"] in asked and str(r["day"] or "~") < guess_cut:
                out.append({"page": r["page"], "id": r["id"], "text": _shown(r["text"]),
                            "since": str(r["day"]), "guess": True})
            continue
        if r["tier"] not in ("confirmed", "legacy", "typed"):
            continue
        last = max([*_CONFIRMED.findall(r["text"]), said.get(r["id"], ""),
                    str(r["day"] or "") if r["tier"] == "confirmed" else "",
                    back.get(r["id"], {}).get("last", "")])
        if last and last < (dt.date.fromisoformat(today) - dt.timedelta(days=days)).isoformat():
            out.append({"page": r["page"], "id": r["id"], "text": _shown(r["text"]),
                        "since": last})
    out.sort(key=lambda x: (x["since"], x["page"], x["id"]))
    out = [{**{k: v for k, v in g.items() if k != "members"},
            "ids": [m["id"] for m in g["members"]]} for g in collapse(out)]
    return out[:max(0, STALE_PER_MONTH - answered)]


def live_rows(vault, events, today: str) -> list[dict]:
    """The tiered live claim rows the Review works on: none private, none past its until."""
    hidden = _private(vault)
    return [r for r in claims.tiers(vault, events, today=today) if r["page"] not in hidden]


def _proposals(events) -> dict:
    """{(page, text_hash): {sessions, producer}} of the unvoided `propose` changes that hold
    their line's hash; `producer` (RR-10) is the latest one's, `chats` when it names none."""
    events = list(events)
    voided, out = _voided(events), {}
    for e in events:
        c = _payload(e).get("change") if e.get("kind") == "change" else None
        if (isinstance(c, dict) and c.get("tool") == "propose" and isinstance(c.get("hash"), str)
                and e.get("id") not in voided):
            x = out.setdefault((c.get("id"), c["hash"]), {"sessions": set()})
            x["sessions"].add(e.get("session"))
            x["producer"] = c.get("producer") if c.get("producer") in PRODUCERS else "chats"
    return out


def sightings(events) -> dict:
    """R3-4: {(page, text_hash): sessions}: the chats (one session each) that proposed the
    same line."""
    return {k: v["sessions"] for k, v in _proposals(events).items()}


def producer_of(proposals: dict, page: str, text: str) -> str:
    """RR-10: which producer proposed a waiting line (`_proposals`); an old line: `chats`."""
    return proposals.get((page, text_hash(text)), {}).get("producer", "chats")


def ledger(events) -> dict:
    """RR-10: {producer: {accept, reject, paused}} over the Review clicks on each producer's
    cards since its latest `resume` change: a click's changes carry the `producer` the app
    stamped (one verdict per click, its session); before that stamp, a commit decision's
    `decide` and a `dismiss` of corrections, decisions or forget still count. A producer whose
    rejects exceed its accepts over PAUSE_AFTER or more decided cards is `paused`."""
    events = list(events)
    voided, said = _voided(events), {p: {} for p in PRODUCERS}
    for e in events:
        c = _payload(e).get("change") if e.get("kind") == "change" else None
        if not isinstance(c, dict) or e.get("id") in voided or c.get("confirm") != "clicked":
            continue
        if c.get("tool") == "resume":
            if c.get("producer") in PRODUCERS:
                said[c["producer"]] = {}  # counting starts again
            continue
        p = c.get("producer")
        if p not in PRODUCERS:  # a click from before the stamp
            p = ("decisions" if c.get("tool") == "decide" else c.get("id")
                 if c.get("tool") == "dismiss" and c.get("id") in LEGACY_DISMISS else None)
        if p is None:
            continue
        no = c.get("tool") == "dismiss" or c.get("op") == "reject"
        said[p][e.get("session") or e.get("id")] = "reject" if no else "accept"
    out = {}
    for p, votes in said.items():
        n = {v: list(votes.values()).count(v) for v in ("accept", "reject")}
        out[p] = {**n, "paused": n["accept"] + n["reject"] >= PAUSE_AFTER
                  and n["reject"] > n["accept"]}
    return out


def answered_today(events, today: str) -> int:
    """R3-4: the Today's 5 kinds of card (TODAY_CARDS) the user answered on `today`: one per
    app click, each its own session stamped with the card's kind (app/server.py `_click`)."""
    voided, said = _voided(events), set()
    for e in events:
        c = _payload(e).get("change") if e.get("kind") == "change" else None
        if (isinstance(c, dict) and c.get("card") in TODAY_CARDS and e.get("day") == today
                and e.get("id") not in voided and not c.get("auto")):  # D1/D2: not a click
            said.add(e.get("session"))
    return len(said)


def view(vault, events, today: str) -> dict:
    """The Review view: every text is shown text (`_shown`). D4: the badge `count` is the
    Today's 5 cards still unanswered; `more` counts every board's cards. R3-4: a
    single inference (a waiting line other than a teaching rule that fewer than SIGHTINGS
    chats proposed) leaves the view SINGLE_DAYS after its stamp (inbox.md keeps it), and
    `today` orders at most TODAY_MAX cards a day (`_today`). RR-10: a `paused` producer's
    cards (`ledger`) are not offered; `paused` lists them for the Status line. G5b-4: a claim
    line on a card that chats said again carries `said` {times, last} (`reinforced`)."""
    events = list(events)
    led = ledger(events)
    off = {p for p, x in led.items() if x["paused"]}
    hidden = _private(vault)
    rows = [r for r in claims.tiers(vault, events) if r["page"] not in hidden]  # writes nothing
    expired = [{"page": r["page"], "id": r["id"], "text": _shown(r["text"]), "until": until}
               for r in rows if (until := claims.expired(r["text"], today))]
    seen, cut = _proposals(events), (dt.date.fromisoformat(today)
                                     - dt.timedelta(days=SINGLE_DAYS)).isoformat()

    def single(p: dict) -> bool:
        return p["page"] != "teaching-contract" and len(
            seen.get((p["page"], text_hash(p["text"])), {}).get("sessions", ())) < SIGHTINGS

    waiting = [{**p, "section": plain(p["section"], 80), "text": _shown(p["text"]),
                "single": single(p)}
               for p in pending(vault, today) if not (single(p) and (p["since"] or "~") < cut)
               and producer_of(seen, p["page"], p["text"]) not in off]
    waiting = [{**{k: v for k, v in g.items() if k != "members"},
                "keys": [m["key"] for m in g["members"]]}  # D5: one card per near-copy group
               for page in dict.fromkeys(p["page"] for p in waiting)
               for g in collapse([p for p in waiting if p["page"] == page])]
    known = known_texts(vault)
    back = reinforced(events, known)

    def said(cid: str) -> dict:  # G5b-4: how often chats said the line, a fold (`reinforced`)
        x = back.get(cid)
        return {"said": {"times": len(x["sessions"]), "last": x["last"]}} if x else {}

    old = [{**x, **said(x["id"])} for x in stale(rows, events, today, known)]

    def side(r: dict) -> dict:
        return {"page": r["page"], "id": r["id"], "text": _shown(r["text"]), **said(r["id"])}

    current = [r for r in rows if not claims.expired(r["text"], today)]  # = live_rows
    pairs = [{"key": c["key"], "why": c["why"], "left": side(c["left"]),
              "right": side(c["right"])} for c in conflicts(current, events, known)]
    from keepfold import corrections, more_sources  # read imported pages: only the Review asks
    rules = [{"key": x["key"], "text": _shown(x["text"]), "chats": x["chats"],
              "page": x["page"]}
             for x in ([] if "corrections" in off else corrections.proposals(vault, events,
                                                                              current))]
    decisions = [{**x, "project": plain(x["project"], 80), "subject": plain(x["subject"], SHOWN)}
                 for x in ([] if "decisions" in off
                           else more_sources.decision_proposals(vault, events))]
    from keepfold import tags  # it imports this module
    forget = [{"key": g["key"], "keys": [m["key"] for m in g["members"]],
               "text": plain(g["text"], SHOWN),
               "candidates": list({c["id"]: side(c) for m in g["members"]
                                   for c in m["candidates"]}.values())}
              for g in collapse([] if "forget" in off else tags.forget_cards(vault, events,
                                                                             today))]
    from keepfold import sources
    imports = [{**x, "title": plain(x["title"], SHOWN)}
               for x in sources.edited_pages(vault, events)]
    unsafe = [{"key": g["key"], "page": g["page"], "id": g["id"], "what": g["what"],
               "keys": [m["key"] for m in g["members"]], "ids": [m["id"] for m in g["members"]],
               "invisible": claims.invisible(g["what"]), "command": bool(g.get("command")),
               "text": _shown("".join(c for c in g["text"] if c.isprintable()))}
              for g in collapse([x for x in unsafe_lines(vault, events)
                                 if x["page"] not in hidden])]
    outside = [{"key": x["key"], "page": x["page"], "section": plain(x["section"] or "", 80),
                "id": x["id"], "text": _shown("".join(
                    c for c in claims.strip_id(x["line"]).removeprefix("- ") if c.isprintable()))}
               for x in outside_lines(vault, events) if x["page"] not in hidden]
    spare = _spare_left(events, today)  # G7: crowded first, then unused, within the month's cap
    lines, window = crowded(vault, current) if spare else ([], 0)
    crowd = [{**side(x), "cut": x["cut"], "seen": x["seen"], "window": window}
             for x in lines[:spare]]
    books, calls, since = unused(vault, events, today) if spare > len(crowd) else ([], 0, None)
    books = [{**x, "calls": calls, "since": since} for x in books[:spare - len(crowd)]]
    pin = [{**side(x), "proofs": x["proofs"], "apps": x["apps"], "served": x["served"]}
           for x in promote(vault, current, events, today)]  # R28a: one a month at most
    left = max(0, TODAY_MAX - answered_today(events, today))
    first = ([{"type": "unsafe", "key": x["key"]} for x in unsafe]  # G6: always asked, first
             + [{"type": OUTSIDE, "key": x["key"]} for x in outside])  # R5a: likewise
    today_ = first + _today(waiting, pairs, rules)[:max(0, left - len(first))]
    return {"pending": waiting, "expired": expired, "stale": old, "conflicts": pairs,
            "rules": rules, "decisions": decisions, "forget": forget, "imports": imports,
            "unsafe": unsafe, OUTSIDE: outside, "crowded": crowd, "unused": books, "promote": pin,
            "today": today_, "today_left": left,
            "paused": [{"producer": p, "label": PRODUCERS[p], **led[p]} for p in sorted(off)],
            "count": len(today_),  # D4: the badge counts only Today's 5 still unanswered
            "more": len(waiting) + len(expired) + len(old) + len(pairs) + len(rules)
            + len(decisions) + len(forget) + len(imports) + len(unsafe) + len(outside)
            + len(crowd)
            + len(books) + len(pin)}


def _today(waiting: list[dict], pairs: list[dict], rules: list[dict]) -> list[dict]:
    """R3-4's order, as {type, key} of cards in the boards (after G6's unsafe lines, which
    `view` puts first): teaching-rule proposals, then contradiction pairs, repeated
    corrections, and last the other waiting lines that 2+ chats proposed (a single inference
    never)."""
    rule = [p for p in waiting if p["page"] == "teaching-contract"]
    return ([{"type": "pending", "key": p["key"]} for p in rule]
            + [{"type": "conflict", "key": c["key"]} for c in pairs]
            + [{"type": "rule", "key": x["key"]} for x in rules]
            + [{"type": "pending", "key": p["key"]} for p in waiting
               if p not in rule and not p["single"]])
