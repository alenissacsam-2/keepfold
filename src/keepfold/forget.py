"""R10: one honest forget. `forget(vault, id)` takes a page (`people/<slug>`, a chat page, any
other page), or a claim line (`me/<page>^c-…`), out of every store Keepfold owns; `browse` does
the same around `keepfold.browse.forget.purge`. Each returns a receipt: what went from each
store, and what Keepfold cannot remove (`left`), named plainly.

Where a page's or line's text can sit, and how a forget clears it:
- the page or line: deleted through keepfold.vault (a claim line with the `## Past` lines that
  led to it; a people page with the voice samples and drafts addressed to it);
- the event log (events/**.jsonl): every event is rewritten in place through keepfold.vault's
  atomic write. A redacted event keeps its id, key shape, ts, day, session, kind, mode, source,
  schema, predicted_p, grader and ref, and the fold fields (object, also, problem, mistakes,
  targets_mistakes), so levels, voids and `keepfold undo` fold as before; in every other string
  each copy of the text becomes "[forgotten]", a dict entry keyed by it goes, and a forgotten
  claim line's hashes go too (the guard's line hashes, the dismiss/patch text hash);
- Keepfold's own pages (sessions/, rollups/, log.md, Today.md, index.md, inbox.md: an inbox
  line holding the text goes whole) and the chat pages (archive/chats/, the chat_saved
  ledger's pages): each copy replaced;
- `.keepfold/**.json(l)` caches (recall log, X-ray, cloud log, the local Chat's history, the
  fact-harvest and safety-net ledgers, the guard baseline, ...): each copy replaced;
- chat transcripts Keepfold keeps (chats.export_root(): imported exports and the live logs):
  swept, or deleted when the page forgotten is that chat's page; the chat import's state
  (.keepfold/chat-import.sqlite) loses the chat's record (a forgotten chat is marked so a
  re-import skips it);
- the search index: in memory only (rebuilt per process); its snapshot
  (.keepfold/index-snapshot.json) is written again from the pages as they are now;
- vectors (.keepfold/vectors.db): the rows of pages that went or changed (`semantic.prune`);
  the old .npz the store was read from is deleted when it holds such a row;
- the SQLite stores (vectors.db, chat-import.sqlite; with a browse forget also the browse
  store and its vectors): secure_delete, WAL checkpoint(TRUNCATE), VACUUM;
- damaged stores moved aside (`keepfold.quarantine`, `<store>.damaged-<stamp>` and sidecars):
  deleted, not pruned: a damaged file cannot be pruned reliably, and the live store is
  rebuilt from the pages (vectors) or the transcripts (chat import), so nothing else is lost;
- the AI apps' rules files whose Keepfold block carries the pinned core (keepfold.app.targets:
  Cursor, VS Code, Codex's AGENTS.md): rewritten through `targets.refresh_rules`, and the
  backup that rewrite makes is deleted (it holds only the old block beyond what the file
  keeps); older backups are named in `left` while they hold the text.
A page's id is never swept as a substring: only a string that equals it, or its link forms
([[id]], [[id|...]], <folder>/<id>). A title, a line's text or a //forget's words are swept
only when specific (`_texts`: MIN_CHAT characters, or MIN_TEXT and two words or more; from
chats, MIN_CHAT); a person's name from MIN_TEXT everywhere, one word too, and the receipt
says so. The receipt names what was left unswept. Copies in other words are not found.
Each file is checked outside the vault lock and replaced under it, one at a time, so AI
apps' writes (which wait 5 s for the lock) never wait for the whole sweep.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from keepfold.vault import CHAT_ARCHIVE, ID_RE, Vault, VaultError, invalid

log = logging.getLogger(__name__)

MARK = "[forgotten]"
MIN_TEXT, MIN_CHAT = 4, 12  # characters; a page title like "Math" is never swept from chats
# event fields kept as written: the ids levels, voids and the review fold over, at any depth
# (a payload's `concept` too) ...
FOLD = frozenset({"kind", "ref", "object", "also", "problem", "mistakes", "targets_mistakes",
                  "concept", "assessment", "claim"})
# ... and at the top the server-stamped fields and the event's place
KEEP = FOLD | {"id", "ts", "day", "session", "mode", "source", "schema", "predicted_p",
               "grader"}
OWN_DIRS = ("sessions", "rollups")  # pages Keepfold writes itself (chat pages: from the ledger)
OWN_FILES = ("log.md", "Today.md", "index.md", "inbox.md")
SKIP_CACHE = ("backups", "tmp")  # .keepfold folders not swept (backups are named in `left`)
# the snapshot is written again; watch-files.json keys each watched file by its path, and a
# dropped key would read the file as new and bring a forgotten page back (`left` says so)
UNSWEPT = ("index-snapshot.json", "watch-files.json")
STATE = ".keepfold/chat-import.sqlite"  # chat_import.STATE (not imported: it loads a lot)
DBS = (".keepfold/vectors.db", STATE)
BROWSE_DBS = (".keepfold/browse/store.db", ".keepfold/browse/vectors-private.db")
NOT_PAGES = {"concept": "its entry lives in catalog.yaml; edit or remove it there",
             "area": "its entry lives in catalog.yaml; edit or remove it there",
             "domain": "its entry lives in catalog.yaml; edit or remove it there",
             "me": "forget one line with its id, me/<page>^c-xxxxx",
             "browse": "use `keepfold browse forget --site <domain>`"}
SYNCED = ("OneDrive", "Dropbox", "Google Drive", "GoogleDrive", "My Drive", "iCloudDrive",
          "iCloud Drive", "Box", "pCloud", "MEGA", "Nextcloud", "Syncthing")
CLAIM_RE = re.compile(r"^(?:me/(?P<page>[a-z-]+))?\s*#?\^?(?P<id>c-[a-z2-7]{5})$")


# ---- entry points ---------------------------------------------------------------------------

def check(vault: Vault, id: str) -> str:
    """What `forget(vault, id)` would forget, in words; VaultError when it cannot."""
    if m := CLAIM_RE.match(id.strip()):
        rows = _claim_rows(vault, m["page"], m["id"])
        return f"the line \"{rows[0]['text']}\" on me/{rows[0]['page']}"
    kind, path = _page(vault, id)
    return f"{kind} page {path.relative_to(vault.root).as_posix()}"


def forget(vault: Vault, id: str, also: tuple[str, ...] = ()) -> dict:
    """Forget a claim line (`me/<page>^c-…` or `^c-…`) or a page (`people/<slug>`, a page id,
    `<folder>/<id>`). `also`: more text to sweep (a //forget's own words). The receipt."""
    if m := CLAIM_RE.match(id.strip()):
        return claim(vault, m["page"], m["id"], also)
    return page(vault, id, also)


def page(vault: Vault, id: str, also: tuple[str, ...] = ()) -> dict:
    from keepfold import people, relink

    kind, path = _page(vault, id)
    note = vault.read_path(path)
    rel = path.relative_to(vault.root).as_posix()
    title = str(note.fields.get("title") or "")
    texts, names = {title, *also}, {title} if kind == "person" else set()
    refs = {rel.removesuffix(".md"): kind}  # people/<slug> for a person
    removed: dict = {}
    chats = _chat_ledger(vault, note.id)
    if kind == "person":
        removed.update(people.drop_voice(vault, people.PREFIX + note.id))
    removed["page"] = int(vault.remove(path))
    if kind == "person":
        relink.apply(vault, relink.people_fixes(vault))  # drop it from the hub
    if chats:
        removed["chats"] = _drop_chats(vault, chats)
    removed.update(_sweep(vault, texts, refs=refs, names=names))
    removed.update(_finish(vault))
    raw = str(note.fields.get("source") or "")
    return _receipt(f"{kind} page {rel}", removed, vault, [rel], texts,
                    [raw] if raw.startswith("raw/") else [], chats, rules=_refresh_rules(vault),
                    refs=refs, names=names)


def claim(vault: Vault, page_: str | None, cid: str, also: tuple[str, ...] = ()) -> dict:
    from keepfold import claims, review

    rows = _claim_rows(vault, page_, cid)
    pg = rows[0]["page"]
    path = vault.root / "me" / f"{pg}.md"
    gone: list[dict] = []

    def change(text: str) -> str:
        new, got = claims.drop(text, pg, cid)
        gone[:] = got
        return new

    vault.update_text(path, change)
    texts = {claims._MARK.sub("", r["text"]).strip() for r in gone} | set(also)
    hashes = {review.text_hash(r["text"]) for r in gone} | {
        review.line_hash(r["line"]) for r in gone}
    removed = {"lines": len(gone), **_sweep(vault, texts, hashes)}
    removed.update(_finish(vault))
    return _receipt(f"a line on me/{pg}", removed, vault, [f"me/{pg}.md"], texts,
                    rules=_refresh_rules(vault))


def due(events) -> list[dict]:
    """The //forget lines tags retired and queued (`forget_due` log events), oldest first, but
    those a `forget_done` names (its `ref`) or a void voided."""
    events = list(events)

    def kind(e):
        return e.get("kind") == "log" and isinstance(p := e.get("payload"), dict) and p.get("kind")
    closed = {e["payload"].get("ref") for e in events if kind(e) == "forget_done"} | {
        e.get("ref") for e in events if e.get("kind") == "void"}
    return [e for e in events if kind(e) == "forget_due" and e["id"] not in closed]


def run_due(vault: Vault) -> list[dict]:
    """Each `due` //forget swept from every store (`claim`; the line is in `## Past` by now),
    then a `forget_done` log event with the receipt's counts only (its `left` quotes the
    text). A line gone meanwhile (a CLI forget) has only the //forget's words swept. Only the
    Keepfold app's periodic pass runs this. The receipts."""
    from keepfold.events import EventLog

    evlog, out = EventLog(vault), []
    for e in due(evlog.iter_events()):
        p = e["payload"]
        text = str(p.get("text") or "")
        try:
            out.append(claim(vault, p.get("page"), str(p.get("claim")), also=(text,)))
            removed = out[-1]["removed"]
        except VaultError as err:
            if err.code != "unknown_id":
                raise
            removed = _sweep(vault, {text})
        evlog.append(session="cli", kind="log", source="self", fields={"payload": {
            "kind": "forget_done", "ref": e["id"], "removed": removed}})
    return out


def browse(vault: Vault, rule, what: str) -> dict:
    """`keepfold browse forget`: `purge` (every browse store), then the shared finish and the
    browse stores compacted. The receipt; `removed` holds purge's counts too."""
    from keepfold.browse import forget as bf
    from keepfold.browse import sites

    removed = Counter(bf.purge(vault, rule))
    removed.update({k: n for k, n in _finish(vault).items() if k != "index"})  # adds
    with sites.busy(vault, bf.FORGET_WAIT_S):
        removed["compacted"] += sum(_compact(vault.root / d) for d in BROWSE_DBS)
    left = ["Your browsers still hold their recent history: add it to browse.deny_domains (or "
            "set the category to skip) so the next import leaves it out."]
    return _receipt(what, removed, vault, ["browse"], set(), extra=left)


# ---- finding what to forget ---------------------------------------------------------------

def _page(vault: Vault, id: str) -> tuple[str, Path]:
    """(kind, path) of page `id`: a bare id, `<folder>/<id>` or `people/<slug>`."""
    ref = id.strip().removesuffix(".md")
    folder, _, stem = ref.rpartition("/")
    if ID_RE.fullmatch(stem):
        for kind in vault.KIND_DIRS:
            for d in vault.dirs(kind):
                if folder in ("", d) and (p := vault.root / d / f"{stem}.md").is_file():
                    if kind in NOT_PAGES:
                        raise invalid(f"{ref} is a {kind} page: {NOT_PAGES[kind]}.")
                    return kind, p
    raise VaultError("unknown_id", f"No page '{ref}'; use its id (as keepfold_find shows it), "
                     "people/<slug> or me/<page>^c-xxxxx.")


def _claim_rows(vault: Vault, page_: str | None, cid: str) -> list[dict]:
    from keepfold import claims

    rows = [r for r in claims.rows(vault) if r["id"] == cid and page_ in (None, r["page"])]
    if not rows:
        raise VaultError("unknown_id", f"No claim line ^{cid}" + (f" on me/{page_}" if page_
                         else "") + "; read the me/ page and use a line's ^c-xxxxx id.")
    return rows


def _chat_ledger(vault: Vault, pid: str) -> list[dict]:
    """The chat_saved payloads (any, voided too) that list page `pid`: its chats."""
    from keepfold.events import EventLog

    return [p for e in EventLog(vault).iter_events()
            if e.get("kind") == "log" and isinstance(p := e.get("payload"), dict)
            and p.get("kind") == "chat_saved" and pid in (p.get("pages") or [])]


# ---- sweeping ---------------------------------------------------------------------------------

def _texts(texts, names=()) -> tuple[set[str], set[str]]:
    """(texts swept from Keepfold's own files, from chats). A free text is swept only when it is
    specific: MIN_CHAT characters, or MIN_TEXT and two words or more ("Python" alone would
    hit years of chats); from chats, MIN_CHAT. A person's name (`names`) is swept everywhere
    from MIN_TEXT, one word too: forgetting a person means every mention of them."""
    def norm(t):
        return " ".join(str(t).split())
    names = {n for n in map(norm, names) if len(n) >= MIN_TEXT}
    own = {t for t in map(norm, texts) if len(t) >= MIN_CHAT or (len(t) >= MIN_TEXT and " " in t)}
    return own | names, {t for t in own if len(t) >= MIN_CHAT} | names


def _pattern(texts, refs=None, exact: bool = True) -> re.Pattern | None:
    """Each of `texts` whole (not inside a longer word or id), any case, any run of whitespace
    between its words; each page in `refs` ({`<folder>/<id>`: kind}) never as a substring, only
    in its link forms ([[id]], [[id|...]], [[id#...]], <folder>/<id>, and `<kind> <id>` as
    session notes name it) and, with `exact`, as a whole string, case as written."""
    alts = [(r"(?<![\w-])" if re.match(r"[\w-]", t) else "") + r"\s+".join(map(
        re.escape, t.split(" "))) + (r"(?![\w-])" if re.search(r"[\w-]$", t) else "")
        for t in sorted(filter(None, texts), key=len, reverse=True)]
    for ref, kind in (refs or {}).items():
        folder, _, pid = ref.rpartition("/")
        f, i, k = re.escape(folder), re.escape(pid), re.escape(kind)
        link = (rf"\[\[(?:{f}/)?{i}(?:[#|][^\]\n]*)?\]\]|(?<![\w.-]){f}/{i}(?:\.md)?(?![\w-])"
                rf"|(?<![\w-]){k} {i}(?![\w-])")
        alts.append(f"(?-i:{link}" + (rf"|\A{i}\Z" if exact else "") + ")")
    return re.compile("|".join(alts), re.I) if alts else None


def _needles(texts, refs=(), hashes=()) -> list[str] | None:
    """Casefolded words one of which every match holds as written in a file, so a file without
    any is skipped unread (JSON escapes only quotes, backslashes, control and, from some
    writers, non-ASCII characters); None (read every file) when a text has no such word."""
    out = [*(r.rpartition("/")[2] for r in refs), *hashes]
    for t in texts:
        w = max(t.split(), key=len)
        if not (w.isascii() and w.isprintable()) or '"' in w or "\\" in w:
            return None
        out.append(w)
    return [n.casefold() for n in out]


def _scrub(v, pat: re.Pattern | None, hashes=frozenset(), keep=frozenset()):
    """`v` with each match of `pat` in a string replaced by MARK, a string that is one of
    `hashes` replaced (in a list: dropped) and a dict entry keyed by a match dropped; the
    values of dict keys in `keep` stay as they are."""
    if isinstance(v, str):
        return MARK if v in hashes else (pat.sub(MARK, v) if pat else v)
    if isinstance(v, list):
        return [_scrub(x, pat, hashes, keep) for x in v
                if not (isinstance(x, str) and x in hashes)]
    if isinstance(v, dict):
        return {k: x if k in keep else _scrub(x, pat, hashes, keep) for k, x in v.items()
                if k not in hashes and not (pat and pat.search(k))}
    return v


def _lines(path: Path, fix, needles=None) -> tuple[str, int] | None:
    """Each line of a JSONL file through `fix` (a parsed object -> new object; an unparsable
    line is passed as its text); (new text, lines changed), None when unreadable or when it
    holds none of `needles` (`_needles`)."""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if needles is not None and not any(n in text.casefold() for n in needles):
        return None
    out, n = [], 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        try:
            obj = json.loads(body)
        except ValueError:
            obj = body
        new = fix(obj)
        if new != obj:
            n += 1
            line = (new if isinstance(new, str) else json.dumps(
                new, ensure_ascii=False, separators=(",", ":"))) + line[len(body):]
        out.append(line)
    return "".join(out), n


def _write(vault: Vault, path: Path, text: str) -> None:
    vault._atomic_write(path, text)  # a vault file or a chat transcript: temp file, os.replace


def _rewrite(vault: Vault, p: Path, fix, needles) -> int:
    """A JSONL file of the vault through `_lines`: checked outside the vault lock (most files
    hold nothing), then read again and replaced under it, so AI apps' writes wait for one
    file at most, never the whole sweep. Lines changed."""
    if not ((got := _lines(p, fix, needles)) and got[1]):
        return 0
    with vault.lock():
        if (got := _lines(p, fix)) and got[1]:  # again: it may have grown meanwhile
            _write(vault, p, got[0])
            return got[1]
    return 0


def _sweep(vault: Vault, texts, hashes=frozenset(), refs=(), names=()) -> dict:
    """Every store's copies of `texts` (those `_texts` keeps), `hashes` and the pages `refs`,
    one file at a time; counts."""
    root, out = vault.root, {}
    own, chat = _texts(texts, names)
    pat, chat_pat = _pattern(own, refs), _pattern(chat, refs, exact=False)
    if pat is None and not hashes:
        return out
    needles = _needles(own, refs, hashes)

    def event(ev):
        if not isinstance(ev, dict):
            return pat.sub(MARK, ev) if pat else ev
        return {k: v if k in KEEP else _scrub(v, pat, hashes, FOLD) for k, v in ev.items()}

    ledger = _ledger_pages(vault)  # before the events are rewritten: a warm parse, not a re-read
    for p in sorted((root / "events").glob("*/*/*.jsonl")):
        if n := _rewrite(vault, p, event, needles):
            out["events"] = out.get("events", 0) + n
    chat_needles = _needles(chat, refs)
    mine = [*(root / f for f in OWN_FILES),
            *(p for d in OWN_DIRS for p in (root / d).rglob("*.md"))]
    chat_pages = [*(root / CHAT_ARCHIVE).glob("*.md"), *ledger]
    for p, use in [*((p, pat) for p in mine), *((p, chat_pat) for p in dict.fromkeys(chat_pages))]:
        if use is not None and p.is_file() and _sweep_page(vault, p, use):  # locks per page
            out["pages"] = out.get("pages", 0) + 1
    cache = root / ".keepfold"
    for p in sorted(cache.rglob("*.json*")) if cache.is_dir() else []:
        rel = p.relative_to(cache).parts
        if (p.suffix not in (".json", ".jsonl") or rel[0] in SKIP_CACHE
                or p.name in UNSWEPT):
            continue
        if rel[:2] == ("browse", "chat"):  # the local Chat's history: a chat
            n = _sweep_json(vault, p, chat_pat, (), chat_needles)
        else:
            n = _sweep_json(vault, p, pat, hashes, needles)
        if n:
            out["caches"] = out.get("caches", 0) + 1
    if chat_pat is not None:
        swept = _sweep_transcripts(vault, chat_pat, chat_needles)
        if swept:
            out["chats"] = len(swept)
            out["chat_state"] = _drop_state(vault, swept, mark=False)
        out["chat_state"] = out.get("chat_state", 0) + _scrub_state(vault, chat_pat)
    return {k: n for k, n in out.items() if n}


def _sweep_page(vault: Vault, p: Path, pat: re.Pattern) -> bool:
    """One page Keepfold wrote: each copy replaced; an inbox line holding one goes whole."""
    def change(text: str) -> str:
        if p.name == "inbox.md":
            return "".join(ln for ln in text.splitlines(keepends=True) if not pat.search(ln))
        return pat.sub(MARK, text)
    try:
        text = p.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if change(text) == text:
        return False
    return vault.update_text(p, change)


def _sweep_json(vault: Vault, p: Path, pat, hashes, needles) -> bool:
    """One .keepfold cache, as `_rewrite` does (a .json file whole)."""
    if pat is None and not hashes:
        return False
    if p.suffix == ".jsonl":
        return bool(_rewrite(vault, p, lambda o: _scrub(o, pat, hashes) if not isinstance(
            o, str) else (pat.sub(MARK, o) if pat else o), needles))

    def scrubbed():  # the new content, None when there is nothing to change
        try:
            raw = p.read_bytes().decode("utf-8")
            if needles is not None and not any(n in raw.casefold() for n in needles):
                return None
            data = json.loads(raw)
        except (OSError, ValueError):
            return None
        return None if (out := _scrub(data, pat, hashes)) == data else out

    if scrubbed() is None:  # outside the lock: most caches hold nothing
        return False
    with vault.lock():
        if (out := scrubbed()) is None:
            return False
        _write(vault, p, json.dumps(out, ensure_ascii=False))
    return True


# ---- chats ------------------------------------------------------------------------------------

def _ledger_pages(vault: Vault) -> list[Path]:
    """The chat pages the chat_saved ledger names (archive/chats/ or resources/)."""
    from keepfold.events import EventLog

    out = []
    for e in EventLog(vault).iter_events():
        p = e.get("payload")
        if e.get("kind") == "log" and isinstance(p, dict) and p.get("kind") == "chat_saved":
            for pid in p.get("pages") or []:
                if isinstance(pid, str) and ID_RE.fullmatch(pid):
                    out += [f for d in vault.dirs("resource")
                            if (f := vault.root / d / f"{pid}.md").is_file()]
    return out


def _transcripts() -> list[tuple[str, Path]]:
    """(chat_import's state key, path) of every transcript Keepfold keeps: the imported exports
    and the live logs under chats.export_root(); Claude Code's own are not Keepfold's."""
    from keepfold import chats

    out = []
    for d, source in [*chats.EXPORT_DIRS.values(), (chats.LIVE, chats.LIVE)]:
        out += [(f"{source}:{d}/{p.stem}", p) for p in sorted((chats.export_root() / d).glob(
            "*.jsonl"))]
    return out


def _chat_lock(vault: Vault):
    return vault.job_lock(".keepfold/chat-import.lock", 10, "A chat import is running; try the "
                          "forget again in a minute.")


def _sweep_transcripts(vault: Vault, pat: re.Pattern, needles) -> list[str]:
    """Each copy of a text in the transcripts Keepfold keeps, replaced; their state keys."""
    swept = []
    with _chat_lock(vault):
        for key, p in _transcripts():
            got = _lines(p, lambda o: _scrub(o, pat) if not isinstance(o, str) else pat.sub(
                MARK, o), needles)
            if got and got[1]:
                # shortcut: a live log appended to during this rewrite loses that turn; a forget
                # is rare and the next turn logs again
                _write(vault, p, got[0])
                swept.append(key)
    return swept


def _drop_chats(vault: Vault, chats_: list[dict]) -> int:
    """A forgotten chat page's chats: the transcript Keepfold keeps deleted, and its state record
    replaced by a mark (no text) that makes chat_import skip that chat for good, so neither a
    later turn nor importing the export again brings it back. How many transcripts went."""
    from keepfold import chats

    n = 0
    with _chat_lock(vault):
        for c in chats_:
            cid = str(c.get("chat_id") or "")
            if c.get("source") != "claude-code" and re.fullmatch(r"[\w.-]+/[\w.-]+", cid):
                p = chats.export_root() / f"{cid}.jsonl"
                if p.is_file():
                    p.unlink()
                    n += 1
    _drop_state(vault, [f"{c.get('source')}:{c.get('chat_id')}" for c in chats_], mark=True)
    return n


def _db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=10, isolation_level=None)
    con.execute("PRAGMA secure_delete=ON")
    return con


def _drop_state(vault: Vault, keys: list[str], mark: bool) -> int:
    """chat_import's records of `keys`: deleted (the swept transcript is read again from the
    start), or with `mark` replaced by {"adopted": "forgotten"}. Rows changed."""
    p = vault.root / STATE
    if not keys or not p.is_file():
        return 0
    n = 0
    with _chat_lock(vault), closing(_db(p)) as con:
        for k in keys:
            if mark:  # the sig stays: unchanged it is not read; changed, `adopted` skips it
                n += con.execute("UPDATE files SET data = ? WHERE key = ?",
                                 (json.dumps({"adopted": "forgotten"}), k)).rowcount
                n += con.execute("INSERT OR IGNORE INTO files VALUES (?, 0, 0, ?)",
                                 (k, json.dumps({"adopted": "forgotten"}))).rowcount
            else:
                n += con.execute("DELETE FROM files WHERE key = ?", (k,)).rowcount
    return n


def _scrub_state(vault: Vault, pat: re.Pattern) -> int:
    """Copies left in chat_import's records (a Claude Code chat's summary): replaced."""
    p = vault.root / STATE
    if not p.is_file():
        return 0
    n = 0
    with _chat_lock(vault), closing(_db(p)) as con:
        for key, data in con.execute("SELECT key, data FROM files").fetchall():
            try:
                rec = json.loads(data)
            except (TypeError, ValueError):
                continue
            if (new := _scrub(rec, pat)) != rec:
                con.execute("UPDATE files SET data = ? WHERE key = ?", (json.dumps(new), key))
                n += 1
    return n


# ---- finishing ------------------------------------------------------------------------------

def _finish(vault: Vault) -> dict:
    """The index snapshot written again from the pages as they are, the vectors of pages that
    went or changed deleted, the SQLite stores compacted, damaged copies of them deleted."""
    from keepfold import quarantine, semantic
    from keepfold.catalog import Catalog
    from keepfold.index import Index

    idx = Index(vault, Catalog.load(vault))
    idx.save_snapshot()
    return {"snapshot": 1, "vectors": semantic.prune(vault),  # VACUUM: its own DB lock only
            "compacted": sum(_compact(vault.root / d) for d in DBS),
            "damaged": sum(vault.remove(p) for p in quarantine.found(vault.root)), "index": idx}


def _compact(path: Path) -> bool:
    """Deleted rows overwritten, the WAL emptied into the file, the file rebuilt (VACUUM), so no
    free page or WAL frame keeps a forgotten row. False when absent or busy."""
    if not path.is_file():
        return False
    try:
        with closing(_db(path)) as con:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.execute("VACUUM")
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return True
    except sqlite3.Error as e:
        log.warning("forget: %s not compacted: %s", path.name, e)
        return False


# ---- the receipt ------------------------------------------------------------------------------

REMOVED = (("page", "the page"), ("lines", "claim lines"), ("samples", "voice samples"),
           ("drafts", "voice drafts"), ("events", "events redacted (their ids, kinds, days "
                                                  "and grades kept)"),
           ("pages", "of Keepfold's own pages swept (sessions, logs, chat pages)"),
           ("caches", "cache files swept"), ("chats", "chat transcripts swept or deleted"),
           ("chat_state", "chat import records"), ("vectors", "search vectors"),
           ("snapshot", "search snapshot written again"), ("compacted", "databases compacted"),
           ("damaged", "files of damaged database copies deleted"),
           ("rules", "AI apps' rules files rewritten (the Keepfold block)"))


def _receipt(what: str, removed: dict, vault: Vault, paths: list[str], texts,
             raw: list[str] = (), chats_: list[dict] = (), extra: list[str] = (),
             rules: list[str] = (), refs=(), names=()) -> dict:
    root = vault.root
    idx = removed.pop("index", None)
    removed = {**removed, "rules": len(rules)}
    own, chat = _texts(texts, names)
    pat = _pattern(own, refs)
    notes = [f'Every mention of "{n}" was redacted, including other people with that name.'
             for n in sorted(_texts((), names)[0])]
    left = list(extra)
    if (root / ".git").exists():
        spec = " ".join(f'--path "{p}"' for p in paths)
        left.append(f"The vault's git history still holds the old versions. Keepfold never "
                    f"rewrites it; to remove them for good, run git filter-repo yourself: "
                    f'git -C "{root}" filter-repo --invert-paths {spec} for a page, or '
                    "--replace-text <file> (one `literal:<text>==>[forgotten]` line each) for "
                    "a line, then delete any remote or cloned copy too.")
    from keepfold import backup
    try:
        bdir = backup.folder(root)
    except VaultError:  # backup.folder is inside the vault, where no backup is made
        bdir = None
    dirs = list(dict.fromkeys(filter(None, (bdir, root / ".keepfold" / "backups"))))  # + 0.x's
    for d in dirs:
        if (n := len(backup.zips(d))) or d == dirs[0]:
            left.append(f"Backups: {n} zip{'s' * (n != 1)} in {d} {'hold' if n != 1 else 'holds'} "
                        "the vault as it was, as does any zip made with `keepfold backup --to`; "
                        "delete them to remove it.")
    left += [f"{r}: the raw file this page was made from; delete it yourself." for r in raw]
    left.append("Raw exports under raw/ and the export files you imported (in Downloads or "
                "wherever you saved them) still hold what they held.")
    from keepfold import watch
    if watch.watched(vault):
        left.append("Watched folders and Zotero keep their files, and Keepfold keeps their paths "
                    "(.keepfold/watch-files.json) so an unchanged file is not read again; a file "
                    "that changes comes in again: delete it there, or stop watching its folder.")
    for c in chats_:
        if c.get("source") == "claude-code":
            left.append(f"Claude Code keeps its own transcript of chat {c.get('chat_id')} in "
                        "~/.claude/projects; Keepfold will not import it again.")
    left.append("AI apps keep what they were already sent: chats in Claude, ChatGPT and other "
                "apps, and any memory they keep of their own.")
    if held := _rules_left(pat):
        left.append("AI apps' rules files, or the backups Keepfold keeps beside each one it "
                    "changes (*.keepfold-<date>.bak), that still hold it; delete the backups (and "
                    f"check the rules files) yourself: {', '.join(held)}.")
    if sync := next((s for s in SYNCED if s.lower() in str(root).lower()), None):
        left.append(f"The vault is in a {sync} folder: its version history and recycle bin may "
                    "keep old copies.")

    def quoted(ts):  # case variants once
        return ", ".join(f'"{t}"' for t in sorted({t.casefold(): t for t in sorted(
            ts, reverse=True)}.values()))
    free = {" ".join(t.split()) for t in texts} - {""}
    if short := free - own:
        left.append(f"Too short or common to sweep safely: {quoted(short)}.")
    if short := own - chat:
        left.append(f"Too short to sweep from chats safely: {quoted(short)}.")
    if mine := _mentions(vault, pat, idx):
        left.append("Your own pages that still mention it (Keepfold leaves them as you wrote "
                    f"them): {', '.join(mine[:5])}" + (f" and {len(mine) - 5} more"
                                                       if len(mine) > 5 else "") + ".")
    return {"what": what, "removed": {k: n for k, n in removed.items() if n}, "notes": notes,
            "left": left, "rules": list(rules)}


def _rule_files() -> list[Path]:
    """Each AI app's rules file Keepfold keeps its block in (keepfold.app.targets), then the
    backups Keepfold made beside it."""
    from keepfold.app import targets

    out = []
    for t in targets.targets():
        if isinstance(t, targets.Rules):
            try:
                p = t.rule_path()
                out += [p, *sorted(p.parent.glob(f"{p.name}.keepfold-*.bak"))]
            except (OSError, ValueError):
                continue
    return out


def _refresh_rules(vault: Vault) -> list[str]:
    """The AI apps' rules files whose Keepfold block (the pinned core) the forget changed,
    rewritten now through targets.refresh_rules: their paths."""
    from keepfold import clients
    from keepfold.app import targets

    before = set(_rule_files())
    try:
        rows = targets.refresh_rules(clients.server_entry(clients.mcp_command(), vault.root))
    except Exception as e:  # noqa: BLE001  (the app's own pass tries again; the receipt says)
        log.warning("forget: rules files not refreshed: %s", e)
        return []
    for b in set(_rule_files()) - before:  # this rewrite's backup: the old block, the rest is
        if b.suffix == ".bak":              # still in the file, so it would only keep the text
            b.unlink(missing_ok=True)
    return [r["file"] for r in rows]


def _rules_left(pat) -> list[str]:
    """The AI apps' rules files, and older backups Keepfold made beside them, still holding a
    match of `pat`."""
    if pat is None:
        return []
    out = []
    for f in _rule_files():
        try:
            if f.is_file() and pat.search(f.read_text(encoding="utf-8", errors="replace")):
                out.append(str(f))
        except OSError:
            continue
    return out


def _mentions(vault: Vault, pat, idx) -> list[str]:
    """Pages Keepfold does not write that still hold a match of `pat`, from the index `_finish`
    built (no second parse of every page)."""
    if pat is None or idx is None:
        return []
    own = [vault.root / d for d in (*OWN_DIRS, CHAT_ARCHIVE, "browse")]
    out = []
    for p, i in idx._ids.items():
        if (n := idx._notes.get(i)) and not any(p.is_relative_to(d) for d in own) and (
                pat.search(n["body"]) or pat.search(json.dumps(n["fields"], default=str))):
            out.append(p.relative_to(vault.root).as_posix())
    return sorted(out)


def lines(receipt: dict) -> list[str]:
    """The receipt as the CLI prints it."""
    got = [f"{n if n != 1 or k not in ('page', 'snapshot') else ''} {label}".strip()
           for k, label in REMOVED if (n := receipt["removed"].get(k))]
    out = [f"forgot {receipt['what']}: removed " + ("; ".join(got) if got else "nothing")]
    out += [*(receipt.get("notes") or []), *(f"rewritten: {p}" for p in receipt.get("rules") or [])]
    return out + ["Keepfold cannot remove:"] + [f"- {x}" for x in receipt["left"]]
