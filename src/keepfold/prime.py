"""The light part of keepfold_prime: the pinned Core, Profile, claim legend, Preferences and the
teaching-contract core, built from a Vault and the event log alone. It never imports the `mcp`
SDK, numpy or `keepfold.semantic` and never builds the Index, so the `keepfold prime-hook` process
stays fast on a large vault; the MCP server adds the Related block on top
(mcp_server.Keepfold.keepfold_prime). Preferences rank by meaning only with an embedder that
process already loaded.
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime

from keepfold import access, claims, render, review, xray
from keepfold.contract import MAX_CHARS as CONTRACT_MAX
from keepfold.contract import scope_contract
from keepfold.events import EventLog, local_day
from keepfold.index import STOPWORDS, TRUNCATED, _edge_targets, _neutralise, _wrap
from keepfold.vault import ID_RE, Vault, VaultError, is_private

CONTEXT_MAX = 2400  # characters, about 600 tokens
# keepfold_prime: one cheap first call per chat, about 500 tokens
PRIME_MAX, PRIME_PROMPT = 2000, 2000
PRIME_CAPS = {"profile": 250, "preferences": 800, "contract": 400, "related": 450,
              "mistakes": 250, "learned": 250}
# A21 (D2 L1): the newest unconfirmed guesses that match the prompt, outside the preference cap
LEARNED_HEAD, LEARNED_MAX, LEARNED_PAGES = "Learned lately (ask)", 2, ("profile", "preferences")
# me/core.md: the user's pinned lines, first in every prime, whole lines within this many chars
CORE_MAX, CORE_HEAD = 500, "Core"
VEC_CACHE = 5000  # line vectors kept for a long-running server; past it the cache starts over
# PHASE3 H3: served claim labels and ranking
LEGEND = "(ask) = check with the user before relying on it."
ASK_WEIGHT = 0.3
# phase4-wiki §7.2: the me/personality.md sections keepfold_context serves, by scope
PERSONALITY_HEADINGS = {"all": ("communication", "how i think"),
                        "communication": ("communication",), "learning": ("how i think",)}
NOT_RECORDED = "not recorded yet; ask the user and save it with keepfold_patch."
# Sections that say how to work with the user in any chat; when no line matches the prompt,
# these lead, and situational ones (Projects, Coursework, …) wait for a match.
GENERAL_SECTIONS = ("communication", "working with ai", "learning", "how i think")
# Words that say what to do, not what about: "revise" must not pull a line that says
# "revised a dissertation". Prime-only; index.STOPWORDS stays as search needs it.
TASK_WORDS = frozenset(("revise", "revision", "help", "make", "write", "solve",
                        "explain", "learn", "study", "practice", "practise", "today",
                        "tomorrow", "please", "want", "need", "can", "how", "what",
                        "why", "show", "give", "tell", "let", "lets", "get", "use"))
# Study abbreviations in a prompt -> the long words preference lines use. The match is a
# substring test, so only expansions of 4+ letters, never the short forms ("os" is in "most").
ABBREVIATIONS = {
    "dbms": ("database",), "sql": ("database",), "os": ("operating",),
    "oop": ("object", "oriented"), "dsa": ("data structure", "algorithm"),
    "ml": ("machine learning",), "dl": ("deep learning",), "ai": ("artificial intelligence",),
    "cn": ("network",), "dp": ("dynamic programming",), "llm": ("language model",),
    "genai": ("generative",), "nlp": ("language processing",), "js": ("javascript",),
    "py": ("python",)}
PROFILE_TEMPLATE = "Who I am"  # the me/profile.md summary `keepfold init` writes (cli.ME_PAGES)
# a non-owner server leaves out me/ pages not shared with work (mcp_server.Keepfold._withheld)
WITHHELD = "withheld: {n} me/ page(s) not shared with this server's role."

# ideas-r2 #2: one line, never a gate (forced work before AI help did worse, IZA DP 18338).
# Its block comes out of the preferences cap, so the prime budget stays the same.
ATTEMPT_HEAD = "Attempt first"
ATTEMPT = ("[[{id}]] is {status}: before solving, ask once for their approach (pattern and "
           "complexity) in a sentence or two and grade it with keepfold_grade(item_type="
           "'application', problem='{id}'{concept}); if they say just show me, solve it.")
_LC_ID = re.compile(r"\blc-\d+\b")
_LC_URL = re.compile(r"leetcode\.(?:com|cn)/problems/([a-z0-9-]+)")
_FRONT_FIELD = re.compile(r"""^(title|url):[ \t]*["']?(.*?)["']?[ \t\r]*$""", re.M)

# relink's navigation lines (`Up: [[x]]`, `People: [[people]]`, `Topics: [[a]], [[b]]`)
_NAV_LINE = re.compile(r"\s*\w+:\s*\[\[[^\]]*\]\](?:\s*,\s*\[\[[^\]]*\]\])*\s*")


def _first_paragraph(body: str) -> str:
    """The first paragraph of a note body that is not only headings or navigation links."""
    for para in body.strip().split("\n\n"):
        text = "\n".join(ln for ln in para.splitlines()
                         if not ln.lstrip().startswith("#") and not _NAV_LINE.fullmatch(ln))
        if text.strip():
            return text.strip()
    return ""


def _sections(body: str) -> list[str]:
    """Each `## ` section of a note body, heading line included."""
    return [f"## {c}".strip() for c in ("\n" + body).split("\n## ")[1:]]


def me(vault: Vault, id: str):
    """A me/ note, or None if the file does not exist or cannot be parsed."""
    try:
        return vault.read("me", id)
    except VaultError as e:
        if e.code in ("unknown_id", "invalid_argument"):  # unparsable: absent, not a failed prime
            return None
        raise


def terms(prompt: str) -> set[str]:
    """The prompt's content words prime matches lines by: no stop or task words, study
    abbreviations expanded (a line matches a term it holds as a substring)."""
    words = re.findall(r"\w+", prompt.lower())
    return ({t for t in words if len(t) > 2 and t not in STOPWORDS and t not in TASK_WORDS}
            | {x for w in words for x in ABBREVIATIONS.get(w, ())})


def owner_first(items: list, section, guess, fixed=lambda x: False) -> list:
    """A21 (3): `items` with, in each `section(x)`, the user's own lines before the guesses
    (`guess(x)`) in the places that section's items hold; `fixed` items (X-ray pins) stay."""
    out = list(items)
    for sec in dict.fromkeys(section(x) for x in items):
        at = [i for i, x in enumerate(out) if section(x) == sec and not fixed(x)]
        mine = [out[i] for i in at]
        for i, x in zip(at, [x for x in mine if not guess(x)] + [x for x in mine if guess(x)],
                        strict=True):
            out[i] = x
    return out


def profile_summary(vault: Vault, today: str, served: dict | None = None,
                    held_ids: set[str] = frozenset(), prompt: str | None = None) -> str:
    """The `summary:` of me/profile.md, else its first paragraph, without lines past their
    `(until …)` day on local day `today` (agenda 4); "" when absent. The init template's
    summary carries no fact, so its live claim lines stand in for it, less those whose id is
    in `held_ids` (R3-3, `held`); with a `prompt` (A21) best match first (pref_rows' word
    match), newest first on a tie, never a guess before the user's own line of its section.
    A claim line gets its `served` label (B3: an agent or exposed line is ` (ask)`, as in
    claim_items)."""
    profile = me(vault, "profile")
    summary = str(profile.fields.get("summary") or "").strip() if profile else ""
    labels = {t: lab for (p, _, t), (lab, _) in (served or {}).items() if p == "profile" and lab}
    if summary == PROFILE_TEMPLATE:
        ids = claim_ids(vault) if held_ids else {}
        lines = [(head, claims.strip_id(ln)) for sec in _sections(profile.body)
                 if (head := sec.partition("\n")[0][3:].strip()).lower() not in claims.RESERVED
                 for ln in sec.splitlines()[1:] if ln.startswith("- ") and ids.get(
                     ("profile", head, claims.strip_id(ln)[2:].strip())) not in held_ids]
        if prompt is not None:
            want = terms(prompt)
            lines = sorted(reversed(lines), key=lambda x: -sum(
                t in f"{x[0]} {x[1][2:]}".lower() for t in want))  # stable: newest first
            lines = owner_first(lines, lambda x: x[0], lambda x: x[1][2:].strip() in labels)
        text = "\n".join(ln for _, ln in lines)
    else:
        text = summary or (_first_paragraph(profile.body) if profile else "")

    def label(ln: str) -> str:
        lab = labels.get(claims.strip_id(ln)[2:].strip()) if ln.startswith("- ") else None
        return claims.strip_since(claims.strip_id(ln) + lab if lab else ln)

    # G6: template claim lines are held (`held_ids`) until Keep; free text has no Keep
    return "\n".join(label(ln) for ln in text.splitlines()
                     if not claims.expired(ln, today) and (
                         summary == PROFILE_TEMPLATE or not claims.unsafe(ln, "profile"))).strip()


def contract(vault: Vault, domains: set, events=None) -> str:
    """me/teaching-contract.md with the overrides of `domains`, cut at CONTRACT_MAX, without
    the lines changed outside Keepfold (R5a, review.outside_lines over `events`, else the log)
    until the owner keeps them."""
    p = vault.root / "me/teaching-contract.md"
    raw = p.read_bytes().decode("utf-8") if p.is_file() else ""
    if raw:
        evs = EventLog(vault).iter_events() if events is None else events
        out = {x["key"] for x in review.outside_lines(vault, evs)
               if x["page"] == "teaching-contract"}
        if out:
            raw = "".join(ln for ln in raw.splitlines(keepends=True)
                          if review.line_key("teaching-contract", ln) not in out)
    text = scope_contract(raw, domains)
    if len(text) > CONTRACT_MAX:
        text = text[:CONTRACT_MAX - len(TRUNCATED)].rstrip() + TRUNCATED
    return text


_CORE_HEAD = re.compile(r"##\s+core(\s+rules)?\s*", re.I)
_RULE = re.compile(r"(\d+[.)]|[-*+])\s+(.*)")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def contract_core(text: str) -> list[str]:
    """CONSENSUS item 3: the contract's rules, not its title or preamble. The lines of a
    `## Core rules` (or `## Core`) section if there is one, else each top-level numbered or
    bulleted rule's lead (its leading bold text, else its first sentence), in order."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":  # frontmatter
        end = next((i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---"), 0)
        lines = lines[end + 1:]
    at = next((i for i, ln in enumerate(lines) if _CORE_HEAD.fullmatch(ln.rstrip())), None)
    if at is not None:
        out = []
        for ln in lines[at + 1:]:
            if ln.startswith("#"):
                break
            if ln.strip():
                out.append(ln.rstrip())
        return out
    out = []
    for ln in lines:
        if not (m := _RULE.fullmatch(ln.rstrip())):  # indented lines are sub-points
            continue
        body = m.group(2)
        bold = re.match(r"\*\*(.+?)\*\*", body)
        lead = bold.group(0) if bold else _SENTENCE_END.split(body, maxsplit=1)[0]
        out.append(f"{m.group(1)} {lead}")
    return out


def served_claims(vault: Vault, events: list[dict], rows: list[dict] | None = None) -> dict:
    """PHASE3 H3: {(page, section, text): (label, score)} for each live claim, by tier alone.
    The user's own lines (typed, legacy, confirmed) are served plain; (ask) for agent or
    exposed, or a yes the model relayed from claims.RELAYED_FROM, ranked below plain lines.
    `rows`: claims.tiers' result when the caller has it."""
    out = {}
    for r in claims.tiers(vault, events) if rows is None else rows:
        ask = claims.late_relay(r) or r["tier"] in ("agent", "exposed")
        out[r["page"], r["section"], r["text"]] = (" (ask)" if ask else "",
                                                   0.5 * (ASK_WEIGHT if ask else 1))
    return out


def held(vault: Vault, events, today: str) -> set[str]:
    """The `^c-…` ids of lines held back while a Review card waits. R3-3: the older line of
    each recorded pair (claims.recorded_pairs: `update_of`, `supersedes`), both lines live on
    non-private pages; prime and keepfold_context serve only the newer line until the user keeps
    both or one. G6: every line review.unsafe_lines flags, until Keep. R5a: every line
    review.outside_lines finds (changed outside Keepfold) until Keep, by its claim id and by its
    card key (`me/setup` lines that are no claim, `claim_items`). Every serving path (prime,
    keepfold_context, the delta, packs and rules files, Chat's About block) mutes these.
    Derived from events at read time; nothing stored."""
    events = list(events)
    out = {xray.claim(x["id"]) for x in review.unsafe_lines(vault, events)}
    for x in review.outside_lines(vault, events):
        out.add(x["key"])
        if x["id"]:
            out.add(xray.claim(x["id"]))
    pairs, _ = claims.recorded_pairs(events)
    if not pairs:
        return out
    hidden = {n.id for n in vault.iter_notes("me") if is_private(n)}
    live = {(r["page"], r["id"]) for r in claims.rows(vault)
            if claims.is_live(r, today) and r["page"] not in hidden}
    return out | {xray.claim(old[1]) for old, new, _ in pairs.values()
                  if old in live and new in live}


def _app(name) -> str:
    """An AI app's name as profiles.norm compares it."""
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def cross_app(vault: Vault, events, client: str | None, rows: list[dict] | None = None
              ) -> set[str]:
    """R5d: the `^c-…` ids of the inferred about-me lines (tier agent or exposed) written in a
    session of another AI app than `client`: the session's `open` event names its app
    (session.py, the MCP handshake). An unknown `client` (None, "") gets none of them; a line
    whose session names no app (an old one, a harvest from imports) is served as before. The
    owner's Still true or Approve (tier confirmed) ends the hold. `rows`: claims.tiers'."""
    events = list(events)
    apps = {e.get("session"): _app(e["payload"].get("client")) for e in events
            if e.get("kind") == "open" and isinstance(e.get("payload"), dict)
            and e["payload"].get("client")}
    if not apps:
        return set()
    me = _app(client)
    return {xray.claim(r["id"]) for r in (claims.tiers(vault, events) if rows is None else rows)
            if r["tier"] in ("agent", "exposed") and r["session"] in apps
            and (not me or apps[r["session"]] != me)}


def claim_ids(vault: Vault) -> dict:
    """{(page, section, text): `^c-…`} for every claim line (xray's claim id form)."""
    return {(r["page"], r["section"], r["text"]): xray.claim(r["id"]) for r in claims.rows(vault)}


def claim_items(vault: Vault, page: str, keep, served: dict, today: str, ids: dict | None = None,
                mute: set[str] = frozenset(), use: dict | None = None) -> list[tuple]:
    """Per kept `## ` section of me/<page> (heading line, other lines, [(score, labelled list
    line, labelled?, `^c-…` id or None)] best first, ties by `use` (`use_order`), then newest
    first); never `## Past`, `## Not me`, a line whose own `(until …)` day is before `today`
    (agenda 4) or one whose id (`ids`) is muted. Lines lose their `(since …)` stamp (R13a). On
    a review.GUARDED page a heading or line whose card key is muted (R5a) is left out."""
    note = me(vault, page)
    out = []
    guarded = page in review.GUARDED and mute
    for sec in _sections(note.body if note else ""):
        head, _, rest = sec.partition("\n")
        name = head[3:].strip().lower()
        lines = [ln.rstrip() for ln in rest.splitlines() if ln.strip()]
        if guarded:
            if review.line_key(page, head) in mute:
                continue
            lines = [ln for ln in lines if review.line_key(page, ln) not in mute]
        if lines and name not in claims.RESERVED and keep(name):
            items = []
            for ln in reversed([ln for ln in lines if ln.startswith("- ")
                                and not claims.expired(claims.strip_id(ln), today)]):
                key = (page, head[3:].strip(), claims.strip_id(ln)[2:].strip())
                cid = (ids or {}).get(key)
                if cid in mute:
                    continue
                label, score = served.get(key, ("", 0.5))
                items.append((score, claims.strip_since(claims.strip_id(ln)) + label,
                              bool(label), cid))
            # stable: ties stay newest first
            items.sort(key=lambda x: (-x[0], -(use or {}).get(x[3], 0)))
            out.append((head, [ln for ln in lines if not ln.startswith("- ")], items))
    return out


def putter(parts: list[str], left: list[int]):
    """A `put(head, text, id, cap, tail)` that appends a wrapped block to `parts`, within `cap`
    and the remaining budget `left[0]`; `tail` (a `more` line) follows the wrapped text,
    outside the note, since it is Keepfold's own word."""
    def put(head: str, text: str, id: str | None = None, cap: int = CONTEXT_MAX,
            tail: str = "") -> None:
        while True:  # trim until the wrapped block fits (escaping can lengthen the text)
            block = f"## {head}\n" + (_wrap(id, text) if id else text) + (
                f"\n{tail}" if tail else "")
            over = len(block) - min(cap, left[0])
            if over <= 0:
                break
            if len(text) - over < 40:
                return
            text = text[: len(text) - over - 1].rstrip() + "…"
        parts.append(block)
        left[0] -= len(block) + 2
    return put


def fit(lines: list[str], id: str, cap: int, left: int, what: str = "") -> list[str]:
    """`lines` minus the last ones until their wrapped block fits `cap` and `left`. With
    `what` (a MORE key) and lines cut, room is kept for the `more` line saying so (RR-13)."""
    keep, room = list(lines), 0
    while keep and len(_wrap(id, "\n".join(keep))) + 40 + room > min(cap, left):
        keep.pop()  # 40: room for the "## <head>\n" line
        if what and not room:
            room = len(more(len(lines), what)) + 1  # n has at most this many digits
    return keep


# RR-13: the line after a block fit() cut, naming the call that reads the rest
MORE = {"preference": "keepfold_context(scope='all')",
        "contract": "keepfold_read(id='me/teaching-contract')",
        "related": "keepfold_recall(query=<the message>)",
        "mistake": "keepfold_context(topic=<the topic>)"}


def more(n: int, what: str) -> str:
    """`(+N more <what> lines: <call> reads them)`, or "" when nothing was cut."""
    return f"(+{n} more {what} lines: {MORE[what]} reads them)" if n > 0 else ""


# G4: playbooks (notes tagged `playbook`) the prompt matches, out of the Related budget
PRIME_PLAYBOOKS, PLAYBOOK_TAG, PLAYBOOK_SUMMARY = 2, "playbook", 60
PLAYBOOK_SAYS = ("Follow it for this task (keepfold_read it first); if the user corrects how, "
                 "keepfold_patch that playbook.")
# a playbook file changed this long after the last write Keepfold logged on it: the owner edited it
OWNER_EDIT_SLACK = 2


def playbooks(vault: Vault, prompt: str, hits: list[dict], notes: dict, mute: set[str],
              scopes, evs: list[dict]) -> list[tuple[str, str]]:
    """G4: [(id, "- [[id]] title: summary")] of the playbooks (`note` pages tagged
    `playbook`) the prompt matches, best first: recall `hits`, then by the content words
    (pref_rows' terms) their title and summary share. `notes` ({id: {kind, title, fields}},
    as the index holds them) holds no private page; a muted one, or one whose `scope` the
    client's `scopes` (None: all) lack, is out, and so is each `withheld_playbooks` one (R5b:
    an agent's until the owner confirms it). keepfold_prime and the prime hook's later-turn
    delta (keepfold.delta.playbooks) both select with this."""
    words = re.findall(r"\w+", prompt.lower())
    terms = {t for t in words if len(t) > 2 and t not in STOPWORDS
             and t not in TASK_WORDS}
    terms |= {x for w in words for x in ABBREVIATIONS.get(w, ())}
    rank = {h["id"]: n for n, h in enumerate(hits) if h.get("match") != "linked"}
    found = []
    for i, n in notes.items():
        f, tags = n["fields"], n["fields"].get("tags")
        if (n["kind"] != "note" or i in mute or PLAYBOOK_TAG not in (
                tags if isinstance(tags, list) else [tags])):
            continue
        if not access.scope_own(scopes, f.get("scope")):
            continue
        summary = render.plain(str(f.get("summary") or ""), PLAYBOOK_SUMMARY)
        shared = sum(t in f"{n['title']} {summary}".lower() for t in terms)
        if i in rank or shared:
            found.append((rank.get(i, len(rank)), -shared, i, n["title"], summary))
    found.sort()
    out = withheld_playbooks(vault, {x[2] for x in found}, evs)
    return [(i, f"- [[{i}]] {title}" + (f": {summary}" if summary else ""))
            for _, _, i, title, summary in found if i not in out]


def withheld_playbooks(vault: Vault, ids: set[str], evs: list[dict]) -> set[str]:
    """R5b: the playbooks of `ids` prime does not serve: one an agent wrote that the owner has
    not confirmed (`unconfirmed_notes`; served "(ask)" before R5b, but an AI app follows a
    playbook's steps all the same), and one written through Keepfold (a logged write) whose
    title, summary or text claims.unsafe flags, as an about-me line (links pass: a playbook
    may name a docs page). A page with no logged write is the owner's own and is not checked."""
    out = unconfirmed_notes(vault, ids, evs)
    logged = set()
    for e in evs:
        pl = e.get("payload") if e.get("kind") == "change" else None
        c = pl.get("change") if isinstance(pl, dict) else None
        if isinstance(c, dict) and c.get("kind") == "note" and c.get("id") in ids:
            logged.add(c["id"])
    for i in sorted(logged - out):
        try:
            n = vault.read("note", i)
        except VaultError:
            out.add(i)
            continue
        if claims.unsafe(f"{n.fields.get('title') or ''}\n{n.fields.get('summary') or ''}\n"
                         f"{n.body}"):
            out.add(i)
    return out


def unconfirmed_notes(vault: Vault, ids: set[str], evs: list[dict]) -> set[str]:
    """G4: which of the note `ids` an agent wrote and the owner has not confirmed: the last
    write Keepfold logged on the page (its `keepfold_add`, or a `keepfold_patch` with its `tier`
    as for claims) is not the owner's (`confirmed`, or `clicked` in the app), and the file
    has not changed since. A page with no logged write is the owner's own; filing it under
    a topic (`set_topics`) is no write about its content."""
    last, at = {}, {}
    for e in evs:
        pl = e.get("payload") if e.get("kind") == "change" else None
        c = pl.get("change") if isinstance(pl, dict) else None
        if isinstance(c, dict) and c.get("kind") == "note" and c.get("id") in ids:
            at[c["id"]] = e.get("ts")
            if c.get("op") != "set_topics":
                last[c["id"]] = c
    out = set()
    for i, c in last.items():
        if c.get("tier") == "confirmed" or c.get("confirm") == "clicked":
            continue
        ts = at[i]
        try:  # shortcut: any later write Keepfold did not log (a relink) reads as the owner's
            # edit; record a hash with each write if that ever matters
            edited = vault.path_for("note", i).stat().st_mtime > datetime.strptime(
                ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp() + OWNER_EDIT_SLACK
        except (OSError, TypeError, ValueError):
            edited = False
        if not edited:
            out.add(i)
    return out


def named_problem(vault: Vault, prompt: str):
    """ideas-r2 #2: the `todo` or `attempted` problem note the prompt names by id (`lc-<n>`),
    LeetCode URL slug or title (2+ words; the longest match wins), else None. It regex-reads
    each front matter and parses only the match: YAML-parsing ~300 problems takes seconds,
    past the prime hook's budget."""
    low = prompt.lower()
    ids, slugs = set(_LC_ID.findall(low)), set(_LC_URL.findall(low))
    folder = vault.root / Vault.KIND_DIRS["problem"]
    best, rank = None, 0
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        try:
            front = path.read_text(encoding="utf-8").split("\n---", 1)[0]
        except (OSError, UnicodeDecodeError):
            continue
        f = dict(_FRONT_FIELD.findall(front))
        title = f.get("title", "").strip().lower()
        r = (10_000 if path.stem in ids else
             5_000 if set(_LC_URL.findall(f.get("url", "").lower())) & slugs else
             len(title) if len(title.split()) > 1 and re.search(
                 rf"(?<!\w){re.escape(title)}(?!\w)", low) else 0)
        if r > rank:
            best, rank = path.stem, r
    try:
        note = vault.read("problem", best) if best else None
    except VaultError:
        return None
    return note if note and note.fields.get("status") in ("todo", "attempted") \
        and not is_private(note) else None


def attempt_line(note) -> str:
    """The Attempt first line for a named problem note (ids and status only, no note text)."""
    cs = _edge_targets(note.fields.get("concepts") or [])
    concept = f", concept='{cs[0]}'" if cs and ID_RE.fullmatch(cs[0]) else ""
    return ATTEMPT.format(id=note.id, status=note.fields["status"], concept=concept)


def core_lines(vault: Vault, served: dict, today: str,
               withheld: set[str] = frozenset(), ids: dict | None = None,
               mute: set[str] = frozenset()) -> list[str]:
    """me/core.md's live lines the user wrote or confirmed (the ones `served` labels plain),
    verbatim (claim id and `(since …)` stripped) in page order, as many whole lines as fit
    CORE_MAX; [] with no
    page, a withheld or private page. Agent, exposed or relayed lines are never pinned, and a
    line whose id (`ids`) is muted in the X-ray is left out."""
    note = None if "core" in withheld else me(vault, "core")
    if note is None or is_private(note):
        return []
    out, size = [], 0
    for (page, sec, text), (label, _) in served.items():
        line = f"- {claims.strip_since(text)}"
        if page != "core" or label or claims.expired(text, today) or (
                ids or {}).get((page, sec, text)) in mute:
            continue
        if size + bool(out) + len(line) <= CORE_MAX:  # a line that does not fit is dropped whole
            size += bool(out) + len(line)
            out.append(line)
    return out


_VECS: dict = {}  # line text -> unit vector, from the embedder in _VECS_OF[0]
_VECS_OF: list = [None]


def similarity(prompt: str, texts: list[str]) -> list[float] | None:
    """Cosine of `prompt` to each of `texts` with the bge embedder, only if this process has it
    loaded already (the MCP server's warm-up): it never imports keepfold.semantic, so the prime
    hook stays light, and never loads a model. None when it is not warm or fails."""
    sem = sys.modules.get("keepfold.semantic")
    embed = getattr(sem, "_embedder", None)
    if embed is None:
        return None
    try:
        if _VECS_OF[0] is not embed or len(_VECS) > VEC_CACHE:
            _VECS.clear()
            _VECS_OF[0] = embed
        if new := [t for t in dict.fromkeys(texts) if t not in _VECS]:
            _VECS.update(zip(new, sem._embed(embed, new), strict=True))
        q = sem._embed(embed, [sem.QUERY_PREFIX + prompt])[0]
        return [float(_VECS[t] @ q) for t in texts]
    except Exception:  # a model error must never cost the prime: word overlap serves
        return None


def pref_rows(vault: Vault, prompt: str, served: dict, today: str, pinned: list[str],
              withheld: set[str] = frozenset(), allow=None, ids: dict | None = None,
              xs: dict | None = None, back: dict | None = None,
              use: dict | None = None) -> list[list]:
    """The preference, setup and personality lines as prime ranks them for `prompt`, best first:
    [-match, situational?, -served score, "- [Section] line", labelled?, "Section: text", id].
    Never a `pinned` line again, a `withheld` page, a section `allow(page, heading)` (an
    agent profile, access.scope_section) refuses, or an X-ray muted claim; X-ray pins lead.
    `ids` (claim_ids) and `xs` (xray.settings) are read when not given. R13b: of lines that
    rank equal, the one that came back latest (`back`: `^c-…` id -> review.reinforced's last
    day) goes first, and one that never came back after it; G5b-4: then, inside a section, the
    more used (`use`: `use_order`)."""
    ids = claim_ids(vault) if ids is None else ids
    xs = xray.settings(vault) if xs is None else xs
    want = terms(prompt)
    rows = []  # [-prompt match, situational?, -served score, line, labelled, text, id]; stable
    seen = {claims.norm(ln) for ln in pinned}  # a pinned line is not repeated
    for page, keep in (("preferences", lambda h: True), ("setup", lambda h: True),
                       ("personality", lambda h: h in PERSONALITY_HEADINGS["all"])):
        if page in withheld:
            continue
        ok = keep if allow is None else (lambda h, k=keep, p=page: k(h) and allow(p, h))
        for head, _, items in claim_items(vault, page, ok, served, today, ids, xs["mute"], use):
            name = head[3:].strip()
            for score, line, lab, cid in items:  # a section name match counts (my presentation)
                bare = line[2:].removesuffix(" (ask)")
                if claims.norm(bare) in seen:
                    continue
                text = f"{name} {line}".lower()
                rows.append([-sum(t in text for t in want),
                             name.lower() not in GENERAL_SECTIONS, -score,
                             f"- [{name}] {line[2:]}", lab, f"{name}: {bare}", cid])
    # by meaning when this process's embedder is warm: cosine times the tier weight, a line
    # under semantic.MIN_COS counting as no match; else by the prompt words a line shares
    if rows and (sims := similarity(prompt, [r[5] for r in rows])) is not None:
        floor = sys.modules["keepfold.semantic"].MIN_COS
        for r, s in zip(rows, sims, strict=True):
            r[0] = s * r[2] / 0.5 if s >= floor else 0  # r[2] / 0.5: -1, or -ASK_WEIGHT
    for r in rows:  # the owner's X-ray pins lead, still within the preferences cap
        if r[6] in xs["pin"]:
            r[0] = float("-inf")
    if back:  # stable sorts: ties stay as they were unless a line came back
        rows.sort(key=lambda r: back.get(r[6], ""), reverse=True)
    rows.sort(key=lambda r: r[:3])
    return owner_first(rows, lambda r: r[5].partition(": ")[0], lambda r: r[4],
                       lambda r: r[0] == float("-inf"))


def learned(rows: list[dict], served: dict, prompt: str, today: str, mute: set[str],
            pins: set[str], withheld: set[str] = frozenset(), allow=None) -> list[tuple]:
    """A21 (D2 L1): the "Learned lately" candidates, newest first: [(served line, `^c-…`
    id)] of the live unconfirmed guesses (served ` (ask)`) on LEARNED_PAGES that share a prompt
    word (`terms`, with their section's name as pref_rows matches), never a muted, X-ray
    pinned (Preferences leads with it), withheld or `allow`-refused one. `rows`: claims.tiers'."""
    want, out = terms(prompt), []
    for r in rows:
        key, cid = (r["page"], r["section"], r["text"]), xray.claim(r["id"])
        if (r["page"] in LEARNED_PAGES and r["page"] not in withheld
                and served.get(key, ("",))[0] and cid not in mute and cid not in pins
                and not claims.expired(r["text"], today)
                and (allow is None or r["page"] == "profile" or allow(r["page"], r["section"]))
                and any(t in f"{r['section']} {r['text']}".lower() for t in want)):
            out.append((str(r["day"] or ""), r["line_no"],
                        f"- [{r['section']}] {claims.strip_since(r['text'])} (ask)", cid))
    out.sort(reverse=True)
    return [(line, cid) for _, _, line, cid in out]


def use_order(vault: Vault, events, today: str, known: dict | None = None,
              back: dict | None = None) -> dict[str, int]:
    """G5b-4: {`^c-…` id: use bucket}: the log2 of how often a line was used before `today`
    (review.use; `back`: its `reinforced` fold when the caller has it), so the calls of one day
    leave prime's order as it was that morning."""
    return {xray.claim(c): x["n"].bit_length()
            for c, x in review.use(vault, events, today, known, back).items()}


def core(vault: Vault, prompt: str, events: list[dict], now: datetime,
         withheld: set[str] = frozenset(), xr: dict | None = None, allow=None,
         client: str | None = None) -> tuple[list[str], list[int]]:
    """keepfold_prime's blocks before Related, and the budget left: (parts, [chars left]).
    Order: Core, Profile, Teaching contract (core), the legend, Preferences (the owner's
    standing text), Learned lately (A21: `learned`'s newest LEARNED_MAX lines, left out of
    Profile and Preferences), then the prompt's Attempt first line.
    `withheld`: bare ids of me/ pages left out (a non-owner server or an agent profile);
    `allow(page, heading)`: the preference and personality sections an agent profile allows
    (None: all). The X-ray's pinned claims lead Preferences and muted ones are left out; `xr`,
    if given, gets the `served` and `trimmed` ids by block (keepfold.xray). `client`: the AI app
    asking (R5d, `cross_app`; None: not known)."""
    prompt = prompt[:PRIME_PROMPT]
    parts: list[str] = []
    left = [PRIME_MAX]
    put = putter(parts, left)
    rows = claims.tiers(vault, events)
    served = served_claims(vault, events, rows)
    xs, ids = xray.settings(vault), claim_ids(vault)
    # R3-3: as muted while its pair waits; R5d: another app's guesses until the owner's yes
    waits = held(vault, events, local_day(now)) | cross_app(vault, events, client, rows)
    xs = {**xs, "mute": xs["mute"] | waits}
    if pinned := core_lines(vault, served, local_day(now), withheld, ids, xs["mute"]):
        put(CORE_HEAD, "\n".join(pinned), "me/core", PRIME_MAX)  # first and whole, never shrunk
    # the core is the user's own pick of preferences, so it comes out of their cap
    pref_cap = max(0, PRIME_CAPS["preferences"] - (PRIME_MAX - left[0]))
    # A21: the slot's lines leave Profile and Preferences (one place each); its room is kept
    # from the start, outside the preferences cap
    got = learned(rows, served, prompt, local_day(now), xs["mute"], xs["pin"], withheld,
                  allow)[:LEARNED_MAX]
    new = fit([ln for ln, _ in got], "me", PRIME_CAPS["learned"], left[0])
    slot = [c for _, c in got[:len(new)]]
    room = len(f"## {LEARNED_HEAD}\n{_wrap('me', chr(10).join(new))}") + 2 if new else 0
    left[0] -= room
    summary = "" if "profile" in withheld else profile_summary(
        vault, local_day(now), served, waits | set(slot), prompt)
    if summary:
        put("Profile", summary, "me/profile", PRIME_CAPS["profile"])
    if "teaching-contract" not in withheld and (rules := contract_core(contract(vault, set(),
                                                                                events))):
        # whole rules that fit, else the first one cut by put
        lines = fit(rules, "me/teaching-contract", PRIME_CAPS["contract"], left[0],
                    "contract") or rules[:1]
        put("Teaching contract (core)", "\n".join(lines), "me/teaching-contract",
            PRIME_CAPS["contract"], more(len(rules) - len(lines), "contract"))
    known = review.known_texts(vault)
    again = review.reinforced(events, known)  # once per prompt: R13b's `back` and the use order
    back = {xray.claim(c): x["last"] for c, x in again.items()}
    rows = pref_rows(vault, prompt, served, local_day(now), pinned, withheld, allow, ids,
                     {**xs, "mute": xs["mute"] | set(slot)}, back,
                     use_order(vault, events, local_day(now), known, again))
    # the prompt's Attempt first line follows Preferences (the owner's standing text first, so
    # it stays the same from chat to chat) and still comes out of the preferences cap
    problem = named_problem(vault, prompt)
    attempt = attempt_line(problem) if problem else ""
    cap = pref_cap - (len(f"## {ATTEMPT_HEAD}\n{attempt}") + 2 if attempt else 0)
    # the legend only when an (ask) line can be served: the Preferences fit below has no more
    # room than this one, so it serves a prefix of these lines
    asks = {r[3] for r in rows if r[4]}
    shown = fit([r[3] for r in rows], "me", cap, left[0])
    if asks.intersection(shown) or " (ask)" in summary or new:
        put("How to read the claims", LEGEND)
    if prefs := fit([r[3] for r in rows], "me", cap, left[0], "preference"):
        put("Preferences", "\n".join(prefs), "me", cap,
            more(len(rows) - len(prefs), "preference"))
    elif n := len(withheld & {"preferences", "personality"}):
        put("Preferences", WITHHELD.format(n=n))  # no "not recorded" nag: /keepfold home asks
    left[0] += room
    if new:
        put(LEARNED_HEAD, "\n".join(new), "me", PRIME_CAPS["learned"])
    if attempt:
        put(ATTEMPT_HEAD, attempt)
    if xr is not None:
        heads = {b.split("\n", 1)[0][3:] for b in parts}
        # A21: the profile's claim lines served whole, by id (a free-text summary: the page)
        body = set(next((b for b in parts if b.startswith("## Profile\n")), "").splitlines())
        prof = {f"- {claims.strip_since(t)}": c for (p, _, t), c in ids.items() if p == "profile"}
        lines = [prof[k] for ln in summary.splitlines()
                 if (k := ln.removesuffix(" (ask)")) in prof and _neutralise(ln) in body]
        xr["served"] = {"core": [c for (p, _, t), c in ids.items()
                                 if p == "core" and f"- {claims.strip_since(t)}" in pinned],
                        "profile": lines or ["me/profile"] * ("Profile" in heads),
                        "learned": slot,
                        "preferences": [r[6] for r in rows[:len(prefs)] if r[6]],
                        "contract": ["me/teaching-contract"] * (
                            "Teaching contract (core)" in heads)}
        xr["trimmed"] = {"preferences": [r[6] for r in rows[len(prefs):] if r[6]]}
    return parts, left
