"""Automatic chat import (the app every EVERY_S, `keepfold import-chats --auto`): one chat page
per past conversation, written with no model.

Claude Code transcripts (chats.root(), the desktop app's Code tab included, and its Cowork
sessions: chats.cowork_files()), Codex, Gemini CLI and Copilot CLI sessions (chats.codex_files,
gemini_files, copilot_files; one reader each turns them into the same Chat), imported chat
exports (chats.export_root()) and the chats a Claude app logged live over MCP (chats.LIVE) not
saved yet, or changed since their page was written, become `resource` pages whose `Timeline:`
line names their `Source:` app (semantic.is_chat), new ones in the git-ignored archive/chats/
(keepfold.chat_archive). An ordinary chat keeps the user's own turns only, redacted and capped
(chats.LINE_CHARS a turn, its last TAIL characters kept; chats.PART_CHARS a page, then only
the first-person sentences of later turns, up to LATE more); assistant text is never copied. A
coding session (file edits or shell commands) becomes a short summary: project, dates, the
user's requests (first lines), their first-person sentences ("In my words", `**Me:**` lines the
safety net and the fact harvest read), files touched (relative paths) and commit subjects,
never code, diffs, tool output or file contents.

Each page written gets a `chat_saved` log event (payload `auto: true` and the body's `hash`);
a chat whose latest one was voided (`keepfold undo`) is never imported again.
A page is rewritten only while its body (less a `Topics:` line) is still what was written: an
owner's edit or deletion and a chat saved by hand are left alone. A chat in a category the
owner set to skip (browse privacy settings) is never written; a local-only one is written
private. Sidechains, transcripts with no user text, transcripts touched in the last
chats.LIVE_SECONDS and sessions run in an `exclude:` folder are skipped.

Transcripts are append-only, and some are tens of MB. STATE (SQLite under `.keepfold/`, a
git-ignored cache) keeps per transcript its (mtime, size), the byte offset read so far, a hash of
its first line and the bounded running summary (Chat): a pass with nothing new only lists and
stats files, a grown transcript is read from its offset only, and one that shrank or whose first
line changed is read again from the start. Newest transcripts go first; work runs in short
slices (SLICE_S) with a rest between them so the app stays responsive. Nothing here embeds:
pages are bounded and the index embeds them later.

A Chat-tab chat logged live and the same conversation in a claude.ai export are one chat
(Ctx.partner: the same first message, started within SAME_CHAT, and no other candidate either
way). They share one page and both ledger keys: the export's turns (complete; a model can forget
a chat_turn) plus the live turns after its last message (_merged), whichever came first.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from keepfold import chat_archive, chats, prime_hook, quarantine, render, tags, taint, topics
from keepfold.browse import privacy
from keepfold.catalog import slugify
from keepfold.events import EventLog, local_day
from keepfold.vault import (
    Vault,
    VaultError,
    _dump_fm,
    _load_fm,
    _newline,
    _split,
    is_private,
    truthy,
)

log = logging.getLogger(__name__)

FIRST_S, EVERY_S = 60, 300  # the app's first pass a minute after start, then every 5 minutes
STATE, LOCK = ".keepfold/chat-import.sqlite", ".keepfold/chat-import.lock"
BUSY = "A chat import is already running."
LABELS = {"claude-code": "Claude Code", "claude-chat": "Claude", "chatgpt": "ChatGPT",
          "gemini": "Gemini", "perplexity": "Perplexity", chats.LIVE: "Claude",
          "codex": "Codex", "gemini-cli": "Gemini CLI", "copilot-cli": "Copilot CLI"}
AGENTS = ("claude-code", "codex", "gemini-cli", "copilot-cli")  # coding agents' own sessions
MINE = "My turns only"  # on the Timeline line: harvest.user_turns ends a turn at `### `
CODING = {"Edit", "MultiEdit", "Write", "NotebookEdit", "Bash"}
REQUESTS, FILES, COMMITS, TITLE_CHARS = 12, 30, 20, 80
# what a summary keeps (the page shows fewer; the category check reads the requests)
ASKED, PATHS, SUBJECTS = 100, 1000, 200
MAX_LINE = 4 << 20  # a longer JSON line (a tool's output) is skipped unparsed
HEAD = 64 << 10  # the first line's first bytes, hashed: a change means a new file
SLICE_S, REST_S = 0.2, 0.02  # work at most this long, then rest this long
FEW = 5  # more chats than this: the progress line shows a percent and the time left
TRANSIENT = ("vault_locked", "busy")  # retried next pass; other failures wait for a change
_ESCAPE = re.compile(r"^(?=#|Timeline: |Topics: |\*\*Me:\*\*)", re.M)  # no fake page lines
_COMMIT = re.compile(r"\bgit\b[^\n;&|]*?\bcommit\b")
_MSG = re.compile(r"""\s(?:-[a-zA-Z]*m|--message)(?:=|\s+)"""
                  r"""(?:"((?:[^"\\]|\\.)*)"|'([^']*)'|([^\s;&|]+))""")
_ABS = re.compile(r"^(?:/|~|[A-Za-z]:/)")
_ME = re.compile(r"^\*\*Me:\*\*(.*)$", re.M)
_SOURCE = re.compile(r"Source: ([^·\r\n]+?)\s*(?:·|$)")
_DATE = re.compile(r"Date: (\d{4}-\d{2}-\d{2})")
EXPORT = chats.EXPORT_DIRS["claude"]  # (folder, source) of claude.ai export conversations
# the live log starts at keepfold_prime, seconds after the first message; 15 minutes allows a slow
# first call or a skewed clock, while two chats opening alike that close are rare (and ambiguous)
SAME_CHAT = timedelta(minutes=15)
OPENING = 200  # characters of the first message compared
TAGS = 50  # //tags kept per chat (R3-5)
UNDONE = {"undone": True}  # _ledger's entry for a chat whose latest chat_saved was voided
# R6: a model's thinking trace in a turn (a pasted DeepSeek or Qwen answer): a closed <think> or
# <thinking> block anywhere, or an unclosed one that opens the turn; never indexed or harvested
_THINK = re.compile(r"<(think|thinking)>.*?</\1\s*>|\A\s*<(?:think|thinking)>(?!.*</think).*",
                    re.S | re.I)
# D2 L4: what a page keeps beyond the caps. A long turn keeps its last TAIL characters (where
# "Also, I'm vegetarian" sits); past PART_CHARS a turn's first-person sentences still go on the
# page, up to LATE more characters; a coding page shows OWN turns' first-person sentences (In my
# words), at most OWN_CHARS a turn. Never code: fences, inline code and indented lines are taken
# out first, and a sentence dense in code symbols is left out.
TAIL, LATE, OWN, OWN_CHARS = 300, 3_000, 30, 500
# "I" as a capital only (a lowercase `i` is a loop variable), "i'm" and kin, my, mine, myself
_I = re.compile(r"\bI\b|\bi(?=['’](?:m|ve|d|ll)\b)|\b(?i:my|mine|myself)\b")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_INDENTED = re.compile(r"^(?: {4}|\t).*$", re.M)
_SYMBOLS = set("{}();=<>$\\|&")  # not [ ]: prose and "[redacted]" use them


def _own(text: str) -> str:
    """The first-person sentences of a user turn (each once, in order), code taken out, joined
    on one line and cut to OWN_CHARS; "" when there is none."""
    out = []
    for s in _SENTENCE.split(_INDENTED.sub("", chats._CODE.sub(" ", text))):
        s = " ".join(s.split())
        code = sum(ch in _SYMBOLS for ch in s) > len(s) // 12
        if len(s) >= 8 and _I.search(s) and not code and s not in out:
            out.append(s)
    own = " ".join(out)
    return own if len(own) <= OWN_CHARS else own[:OWN_CHARS - 1] + "…"


def strip_thinking(text: str) -> str:
    """`text` without its model thinking traces (`_THINK`)."""
    return _THINK.sub("", text).strip() if "<think" in text.lower() else text


def enabled() -> bool:
    """`app.chat_import` in ~/.keepfold/config.yaml: on unless set to false, or off while the
    file cannot be read (`taint.config_error`)."""
    cfg, err = taint._read()
    a = cfg.get("app")
    return not err and not (isinstance(a, dict) and a.get("chat_import") is False)


# ---- which transcripts ------------------------------------------------------------------------

def _transcripts():
    """(source, path) of every Claude Code transcript (Cowork's run Claude Code too), Codex,
    Gemini CLI and Copilot CLI session, imported export conversation and live-logged chat."""
    yield from (("claude-code", p) for p in chats.root().glob("*/*.jsonl"))
    yield from (("claude-code", p) for p in chats.cowork_files())
    yield from (("codex", p) for p in chats.codex_files())
    yield from (("gemini-cli", p) for p in chats.gemini_files())
    yield from (("copilot-cli", p) for p in chats.copilot_files())
    for d, s in [*chats.EXPORT_DIRS.values(), (chats.LIVE, chats.LIVE)]:
        yield from ((s, p) for p in (chats.export_root() / d).glob("*.jsonl"))


def _key(source: str, p: Path) -> str:
    return f"{source}:{chats._cid(p)}"


class State:
    """STATE: per transcript key, its (mtime_ns, size) when handled and a JSON record (offset,
    head, chat). WAL with synchronous=NORMAL: a commit per transcript costs no fsync, and a lost
    one only means that transcript is read again. A damaged one (quick_check at the first open
    in a process) is moved aside (keepfold.quarantine) and started again: the next pass reads
    every transcript from its start, the event log's chat_saved ledger still knows each page
    written (kept, edited or deleted ones stay so) and a forgotten chat's ledger page is
    "[forgotten]", which never makes a page; only the time to read them again is lost."""

    def __init__(self, vault: Vault):
        p = vault.root / STATE
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._open(p)
        except sqlite3.DatabaseError as e:  # a damaged cache starts over; a locked one waits
            if not quarantine.torn(e) or not quarantine.heal(
                    p, e, "every chat is read again from its start", vault):
                raise
            self._open(p)

    def _open(self, p: Path) -> None:
        self.db = sqlite3.connect(p, timeout=10, check_same_thread=False)
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.execute("CREATE TABLE IF NOT EXISTS files (key TEXT PRIMARY KEY, "
                            "mtime_ns INTEGER, size INTEGER, data TEXT)")
            quarantine.check(self.db, p, "files", ["key", "mtime_ns", "size", "data"])
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    def sigs(self) -> dict:
        return {k: (m, s) for k, m, s in self.db.execute("SELECT key, mtime_ns, size FROM files")}

    def get(self, key: str) -> dict:
        row = self.db.execute("SELECT data FROM files WHERE key = ?", (key,)).fetchone()
        try:
            d = json.loads(row[0]) if row else {}
        except (TypeError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    def put(self, key: str, sig: tuple, record: dict) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)",
                            (key, sig[0], sig[1], json.dumps(record, separators=(",", ":"))))


def pending(vault: Vault, now: float | None = None, sigs: dict | None = None) -> list:
    """(source, path, (mtime_ns, size)) of each transcript to look at, newest first: not
    handled at this (mtime, size) and not touched in the last chats.LIVE_SECONDS. A directory
    listing and stats only."""
    if sigs is None:
        with closing(State(vault)) as st:
            sigs = st.sigs()
    now = time.time() if now is None else now
    out = []
    for source, p in _transcripts():
        try:
            s = p.stat()
        except OSError:
            continue
        sig = (s.st_mtime_ns, s.st_size)
        if now - s.st_mtime >= chats.LIVE_SECONDS and sigs.get(_key(source, p)) != sig:
            out.append((source, p, sig))
    return sorted(out, key=lambda x: x[2][0], reverse=True)


# ---- reading a transcript ---------------------------------------------------------------------

@dataclass
class Chat:
    """The bounded running summary of one transcript, kept in STATE between passes."""
    n: int = 0  # user turns
    head: str = ""  # the first turn's first line: the title
    kept: list = field(default_factory=list)  # [timestamp, text] of the turns the page shows
    full: bool = False  # kept reached chats.PART_CHARS: later turns keep their _own only
    late: int = 0  # of kept, the turns past PART_CHARS (their first-person sentences)
    own: list = field(default_factory=list)  # [timestamp, _own] of up to OWN turns (coding)
    asked: list = field(default_factory=list)  # each turn's first line, up to ASKED
    n_asked: int = 0
    first: str = ""  # timestamps
    last: str = ""
    cwd: str = ""
    coding: bool = False
    files: dict = field(default_factory=dict)  # each path a tool wrote, in order, up to PATHS
    commits: dict = field(default_factory=dict)  # each commit subject, in order, up to SUBJECTS
    tags: list = field(default_factory=list)  # [tag, text, day] of the user's //tags, up to TAGS
    sub: bool = False  # a Codex sub-agent's or internal session: never a page
    off: bool = False  # A24: a user turn turned memory off (a line-start //off): never a page

    def dump(self) -> dict:
        return {**asdict(self), "files": list(self.files), "commits": list(self.commits)}

    @classmethod
    def load(cls, d: dict) -> Chat:
        c = cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
        c.files, c.commits = dict.fromkeys(c.files), dict.fromkeys(c.commits)
        return c

    def turn(self, ts: str, text: str) -> None:
        r = chats.redact(strip_thinking(text))
        t = r[:chats.LINE_CHARS + 1]  # redact, then cut
        w = _when(ts)
        # R3-5: the owner's own words; `one` acts on them. Only a tag that starts a line: one
        # mid-line in an old chat is usually quoted or pasted text
        typed = "\n".join(x for x in r.splitlines() if x.lstrip().startswith("//"))
        self.off = self.off or tags.off(r)  # the whole turn: a //off line in code is no tag
        for tag, x in tags.find(typed):
            if tag == "off":
                continue
            item = [tag, x, local_day(w) if w else ""]
            if len(self.tags) < TAGS and item not in self.tags:
                self.tags.append(item)
        self.n += 1
        self.first = self.first or ts
        if self.n == 1:
            self.head = _first(t)[:500]
        own = _own(r)
        if own and len(self.own) < OWN:
            self.own.append([ts, own])
        size = sum(len(x) for _, x in self.kept)
        if not self.full:
            # a long turn: its start and its last TAIL characters
            t2 = r if len(r) <= chats.LINE_CHARS else \
                r[:chats.LINE_CHARS - TAIL - 3] + " … " + r[-TAIL:]
            t2 = chats.redact(t2)
            t2 = t2 if len(t2) <= chats.LINE_CHARS else t2[:chats.LINE_CHARS - 1] + "…"
            if self.kept and size + len(t2) > chats.PART_CHARS:
                self.full = True
            else:
                self.kept.append([ts, t2])
        if self.full and own and size + len(own) <= chats.PART_CHARS + LATE:
            self.kept.append([ts, own])
            self.late += 1
        if r := _line(_first(t)):
            self.n_asked += 1
            if len(self.asked) < ASKED:
                self.asked.append(r)


class _Stop(Exception):
    pass


class Pace:
    """Call it often: past SLICE_S of work it rests REST_S (the app's threads get the GIL) and
    raises _Stop once `cancelled()`."""

    def __init__(self, cancelled: Callable[[], bool] | None = None):
        self.cancelled, self.t = cancelled, time.perf_counter()

    def __call__(self) -> None:
        if time.perf_counter() - self.t >= SLICE_S:
            time.sleep(REST_S)
            if self.cancelled is not None and self.cancelled():
                raise _Stop
            self.t = time.perf_counter()


def _lines(f):
    """(line, offset after it) of each line from f's position. A line over MAX_LINE comes back
    as b"" without being kept in memory; an unterminated last line only when it is whole JSON
    (else it is still being written and is read next time)."""
    off = f.tell()
    while b := f.readline(MAX_LINE):
        if b.endswith(b"\n"):
            off += len(b)
            yield b, off
        elif len(b) < MAX_LINE:  # the end of the file, no newline
            try:
                json.loads(b)
            except ValueError:
                return
            yield b, off + len(b)
            return
        else:
            off += len(b)
            while (b := f.readline(MAX_LINE)) and not b.endswith(b"\n"):
                off += len(b)
            off += len(b)
            yield b"", off


def read(path: Path, prev: dict | None = None, pace: Callable[[], None] = lambda: None,
         source: str = "claude-code") -> tuple[Chat, dict]:
    """Stream a transcript from where `prev` (its STATE record) stopped, on top of prev's
    summary; from the start when there is none, or the file shrank or its first line changed.
    Keeps the user's turns (sidechain, meta and compaction-summary lines dropped) and, from
    coding tool calls, the paths written and commit subjects; tool inputs and outputs never.
    Each line goes to `source`'s reader (_READERS; Claude Code's and the exports' format by
    default); a Gemini CLI session is replayed whole (_gemini). Returns the summary and the new
    record."""
    if source == "gemini-cli":
        return _gemini(path, pace), {}
    add = _READERS.get(source, _add)
    prev = prev or {}
    with path.open("rb") as f:
        head = hashlib.sha1(f.readline(HEAD)).hexdigest()[:16]
        off = prev.get("offset")
        c = None
        if prev.get("head") == head and isinstance(off, int) \
                and 0 < off <= os.fstat(f.fileno()).st_size and isinstance(prev.get("chat"), dict):
            try:
                c = Chat.load(prev["chat"])
            except (TypeError, ValueError):
                c = None
        if c is None:
            c, off = Chat(), 0
        f.seek(off)
        for b, off in _lines(f):  # noqa: B007 (the last offset is the record's)
            pace()
            if b:
                try:
                    d = json.loads(b.decode("utf-8", "replace"))
                except ValueError:
                    continue
                add(c, d)
    return c, {"offset": off, "head": head, "chat": c.dump()}


def _add(c: Chat, d) -> None:
    if not isinstance(d, dict) or d.get("isSidechain"):
        return
    kind, ts = d.get("type"), str(d.get("timestamp") or "")
    if not c.cwd and isinstance(d.get("cwd"), str):
        c.cwd = d["cwd"][:1000]
    # a tool's result comes back as a user line (`toolUseResult`): never the user's words
    if kind == "user" and not d.get("isCompactSummary") and "toolUseResult" not in d \
            and (t := chats._text(d)) and not t.startswith("[Request interrupted"):
        c.turn(ts, t)
    if kind in ("user", "assistant") and ts:
        c.last = ts
    msg = d.get("message")
    if kind == "assistant" and isinstance(msg, dict) and isinstance(msg.get("content"), list):
        for b in msg["content"]:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in CODING:
                _tool(c, b)


def _tool(c: Chat, b: dict) -> None:
    c.coding = True
    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
    p = inp.get("file_path") or inp.get("notebook_path")
    # shortcut: past PATHS files (SUBJECTS commits) the count stops; raise them if pages say so
    if isinstance(p, str) and p and len(c.files) < PATHS:
        c.files.setdefault(p[:1000], None)
    if b.get("name") == "Bash" and isinstance(inp.get("command"), str):
        for s in commits(inp["command"]):
            if (s := _line(s)) and len(c.commits) < SUBJECTS:
                c.commits.setdefault(s, None)


def _edited(c: Chat, path) -> None:
    """A file a tool wrote, summarised as _tool does."""
    if isinstance(path, str) and path:
        _tool(c, {"name": "Edit", "input": {"file_path": path}})


def _ran(c: Chat, cmd) -> None:
    """A shell command a tool ran (a list is an argv), summarised as _tool does."""
    if isinstance(cmd, list):
        cmd = " ".join(x for x in cmd if isinstance(x, str))
    _tool(c, {"name": "Bash", "input": {"command": cmd if isinstance(cmd, str) else ""}})


# ---- other coding agents' sessions ------------------------------------------------------------
# Each reader takes one parsed record and skips a type it does not know. Formats as their
# open-source code defines them; fixtures are hand-written from it, never copied from a session.

# Codex puts its own context in user-role messages: a whole message in one tag
# (<environment_context>, <user_instructions>, <turn_aborted>...) or AGENTS.md's text (openai/codex
# codex-rs/core/src/context/contextual_user_message.rs)
_CODEX_CONTEXT = re.compile(r"\A\s*(?:<([\w-]+)\b[^>]*>.*</\1>\s*\Z|# AGENTS\.md instructions\b)",
                            re.S)
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", re.M)


def _add_codex(c: Chat, d) -> None:
    """One Codex rollout line {timestamp, type, payload} (openai/codex codex-rs/history/src/
    rollout_payload.rs RolloutItemWire, codex-rs/protocol/src/models.rs ResponseItem): the cwd
    from session_meta and turn_context, the user's messages and the shell and apply_patch
    calls. A sub-agent's or internal session (session_meta source) is never a page."""
    p = d.get("payload") if isinstance(d, dict) else None
    if not isinstance(p, dict) or c.sub:
        return
    kind, ts = d.get("type"), str(d.get("timestamp") or "")
    if kind in ("session_meta", "turn_context"):
        src = p.get("source")
        c.sub = c.sub or isinstance(src, dict) and ("subagent" in src or "internal" in src)
        if not c.cwd and isinstance(p.get("cwd"), str):
            c.cwd = p["cwd"][:1000]
        return
    if kind != "response_item":
        return
    t = p.get("type")
    if t == "message" and p.get("role") in ("user", "assistant"):
        if ts:
            c.last = ts
        if p["role"] == "user" and isinstance(p.get("content"), list):
            text = "\n".join(b["text"] for b in p["content"] if isinstance(b, dict)
                             and b.get("type") == "input_text" and isinstance(b.get("text"), str)
                             and not _CODEX_CONTEXT.match(b["text"])).strip()
            if text:
                c.turn(ts, text)
    elif t in ("function_call", "custom_tool_call", "local_shell_call"):
        args = p.get("action") if t == "local_shell_call" else p.get("input")
        if t == "function_call":
            try:
                args = json.loads(p.get("arguments") or "")
            except (TypeError, ValueError):
                args = None
        patch = args if isinstance(args, str) else \
            args.get("input") if isinstance(args, dict) else None
        if isinstance(patch, str) and "*** Begin Patch" in patch:
            for f in _PATCH_FILE.findall(patch):
                _edited(c, f.strip())
        elif isinstance(args, dict) and (cmd := args.get("command") or args.get("cmd")):
            _ran(c, cmd)


def _add_copilot(c: Chat, d) -> None:
    """One Copilot CLI events.jsonl line {id, parentId, timestamp, type, data, agentId?,
    checkpoint?} (the session events of microsoft/copilot-sdk nodejs/src/generated/
    session-events.ts, https://github.com/microsoft/copilot-sdk): the cwd from session.start
    and session.resume, the user's messages, the files tools edited and the shell commands.
    A sub-agent's event, a replayed checkpoint and a message an agent wrote are skipped."""
    if not isinstance(d, dict) or d.get("agentId") or d.get("checkpoint") \
            or not isinstance(x := d.get("data"), dict):
        return
    kind, ts = d.get("type"), str(d.get("timestamp") or "")
    if kind in ("session.start", "session.resume"):
        ctx = x.get("context")
        if not c.cwd and isinstance(ctx, dict) and isinstance(ctx.get("cwd"), str):
            c.cwd = ctx["cwd"][:1000]
    elif kind == "user.message" and isinstance(x.get("content"), str) \
            and not x.get("agentAuthored") and not x.get("isAutopilotContinuation"):
        c.turn(ts, x["content"])
    elif kind == "tool.execution_complete" and isinstance(x.get("fileEdits"), list):
        for e in x["fileEdits"]:
            _edited(c, e.get("path") if isinstance(e, dict) else None)
    elif kind == "tool.execution_start" and isinstance(info := x.get("shellToolInfo"), dict):
        args = x.get("arguments")
        _ran(c, args.get("command") if isinstance(args, dict) and isinstance(
            args.get("command"), str) else info.get("displayCommand"))
    if kind in ("user.message", "assistant.message") and ts:
        c.last = ts


_READERS = {"codex": _add_codex, "copilot-cli": _add_copilot}


def _parts(x) -> str:
    """The text of a Gemini PartListUnion: a string, a part {text} or a list of them."""
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        return x["text"] if isinstance(x.get("text"), str) else ""
    return "\n".join(_parts(p) for p in x) if isinstance(x, list) else ""


def _gemini(path: Path, pace: Callable[[], None]) -> Chat:
    """A Gemini CLI session replayed whole, as chatRecordingService.ts loads it: a message
    record (one with the same id replaces it in place), `$rewindTo` (that message and the ones
    after it dropped; all of them for an unknown id), `$patch` (content, removeIds, orderIds),
    `$set` (a legacy full `messages` list), metadata; or a legacy .json ConversationRecord.
    Records can rewrite earlier ones, so nothing is carried between passes. A message keeps
    only its type, time, the user's text (displayContent, else content) and the tool calls."""
    msgs: dict[str, list] = {}  # id -> [type, timestamp, user text, [(tool, args)]]
    meta: dict = {}

    def message(m) -> None:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str):
            return
        kind = m.get("type")
        text = _parts(m.get("displayContent") or m.get("content")) if kind == "user" else ""
        calls = [(t.get("name"), t.get("args")) for t in m.get("toolCalls") or []
                 if isinstance(t, dict) and isinstance(t.get("args"), dict)] \
            if kind == "gemini" and isinstance(m.get("toolCalls"), list) else []
        msgs[m["id"]] = [kind, str(m.get("timestamp") or ""), text, calls]

    def record(r) -> None:
        nonlocal msgs
        if not isinstance(r, dict):
            return
        if isinstance(r.get("$rewindTo"), str):
            ids = list(msgs)
            cut = ids.index(r["$rewindTo"]) if r["$rewindTo"] in ids else 0
            msgs = {i: msgs[i] for i in ids[:cut]}
        elif isinstance(p := r.get("$patch"), dict):
            for u in [p, *(p.get("updates") if isinstance(p.get("updates"), list) else [])]:
                m = msgs.get(u.get("id")) if isinstance(u, dict) else None
                if m and m[0] == "user" and u.get("content") is not None:
                    m[2] = _parts(u["content"])
            for i in p.get("removeIds") if isinstance(p.get("removeIds"), list) else []:
                if isinstance(i, str):
                    msgs.pop(i, None)
            if isinstance(p.get("orderIds"), list):
                last = [i for i in p["orderIds"] if isinstance(i, str) and i in msgs]
                msgs = {i: msgs[i] for i in [*(i for i in msgs if i not in last), *last]}
        elif isinstance(r.get("$set"), dict):
            meta.update({k: v for k, v in r["$set"].items() if k != "messages"})
            if isinstance(r["$set"].get("messages"), list):
                msgs = {}
                for m in r["$set"]["messages"]:
                    message(m)
        elif isinstance(r.get("id"), str):
            message(r)
        elif isinstance(r.get("sessionId"), str):
            meta.update(r)

    if path.suffix == ".json":
        try:
            data = json.loads(path.read_bytes().decode("utf-8", "replace"))
        except ValueError:  # a broken file is an empty chat, as a broken transcript is
            data = None
        if isinstance(data, dict):
            meta.update({k: v for k, v in data.items() if k != "messages"})
            for m in data.get("messages") if isinstance(data.get("messages"), list) else []:
                message(m)
    else:
        with path.open("rb") as f:
            for b, _ in _lines(f):
                pace()
                try:
                    record(json.loads(b.decode("utf-8", "replace")) if b else None)
                except ValueError:
                    continue
    c = Chat()
    if meta.get("kind") == "subagent":
        return c
    try:  # the project folder Gemini CLI records beside its chats (config/projectRegistry.ts)
        c.cwd = (path.parent.parent / ".project_root").read_text(encoding="utf-8").strip()[:1000]
    except (OSError, UnicodeError):
        pass
    for kind, ts, text, calls in msgs.values():
        if kind == "user" and text.strip():
            c.turn(ts, text)
        if kind in ("user", "gemini") and ts:
            c.last = ts
        # tool names and parameters: packages/core/src/tools/definitions/base-declarations.ts
        for name, args in calls:
            if name in ("write_file", "replace"):
                _edited(c, args.get("file_path"))
            elif name == "run_shell_command":
                _ran(c, args.get("command"))
    return c


def commits(cmd: str) -> list[str]:
    """The subject of each `git commit -m` in a shell command (a heredoc's first line too)."""
    out = []
    for m in _COMMIT.finditer(cmd):
        if not (g := _MSG.search(cmd, m.end())):
            continue
        text = g[1].replace('\\"', '"') if g[1] is not None else g[2] if g[2] is not None \
            else g[3]
        if text.lstrip().startswith("$(cat"):
            text = text.split("\n", 1)[1] if "\n" in text else ""
        if first := next((ln.strip() for ln in text.splitlines() if ln.strip()), ""):
            out.append(first)
    return out


# ---- the page ---------------------------------------------------------------------------------

@dataclass
class Page:
    title: str
    day: str
    body: str
    text: str  # what the category check reads
    related: list = field(default_factory=list)


def _when(ts: str) -> datetime | None:
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _first(text: str) -> str:
    return next((s for s in (ln.strip().lstrip("#>*-` \t") for ln in text.splitlines()) if s),
                "")


def _line(text: str, n: int = 120) -> str:
    return render.plain(chats.redact(text), n)


def _n(xs, word: str) -> str:
    return f"{len(xs)} {word}{'s' * (len(xs) != 1)}"


def _list(xs: list[str], cap: int, total: int | None = None) -> list[str]:
    total = len(xs) if total is None else total
    return [f"- {x}" for x in xs[:cap]] + ([f"- …and {total - cap} more"] if total > cap else [])


def _rel(p: str, base: str) -> str | None:
    """`p` relative to the session's folder; None for a path outside it."""
    p = p.replace("\\", "/")
    if base and p.casefold().startswith(base.casefold() + "/"):
        p = p[len(base) + 1:]
    return None if _ABS.match(p) or p.startswith("..") else p


def build(source: str, path: Path, c: Chat, ctx: Ctx) -> Page:
    """The chat page for `c` (which has at least one user turn)."""
    label = LABELS[source]
    t0 = _when(c.first) or datetime.fromtimestamp(path.stat().st_mtime, UTC)
    day = local_day(t0)
    month = day[:8] + "01"
    up = f"[[{month}]] · " if ctx.vault.path_for("journal", month).is_file() else "· "
    first, kept = c.head, c.kept
    last = local_day(_when(c.last) or t0)
    when = f"When: {day}" + (f" to {last}" if last > day else "")  # R3-6: the fact header
    if not c.coding:
        title = _line(first, TITLE_CHARS) or f"{label} chat {day}"
        out = [f"# {title}", "", f"Timeline: {up}Source: {label} · Date: {day} · {MINE}", "",
               when, f"Who: me and {label}", ""]
        if len(kept) < c.n or c.late:
            more = f", then what I said about myself in {c.late} later ones" if c.late else ""
            out += [f"The first {len(kept) - c.late} of my {c.n} turns{more}.", ""]
        for ts, t in kept:
            if w := _when(ts):
                out.append(f"### {w.astimezone():%Y-%m-%d %H:%M}")
            out += [f"**Me:** {_ESCAPE.sub(lambda m: chr(92), t)}", ""]
        return Page(title, day, _body(out), "\n".join([title, *(t for _, t in kept)]))
    base = c.cwd.replace("\\", "/").rstrip("/")
    # a Gemini CLI session with no recorded folder: its project's folder in ~/.gemini/tmp
    name = base.split("/.claude/worktrees/")[0].rsplit("/", 1)[-1] or (
        path.parent.parent if source == "gemini-cli" else path.parent).name
    title = _line(f"{name}: {first}" if first else f"Coding in {name}", TITLE_CHARS)
    asked = c.asked
    files = [f"`{r.replace('`', '')}`" for r in (_rel(p, base) for p in c.files) if r]
    done = list(c.commits)
    pid = ctx.project(name)
    named = f"[[{pid}|{topics.alias(name)}]]" if pid else _line(name)
    span = f"on {day}" if last <= day else f"from {day} to {last}"
    out = [f"# {title}", "", f"Timeline: {up}Source: {label} · Date: {day}", "",
           when, f"Who: me and {label}", f"Where: {named}", "",
           f"Coding session in {named} {span}: {c.n_asked} request{'s' * (c.n_asked != 1)}, "
           f"{_n(files, 'file')} changed, {_n(done, 'commit')}.", "", "## What I asked",
           *_list(asked, REQUESTS, c.n_asked)]
    if c.own:  # D2 L4: what I said about myself, for the safety net and the fact harvest
        out += ["", "## In my words"]
        for _, t in c.own:
            out += ["", f"**Me:** {t}"]
    if files:
        out += ["", "## Files touched", *_list(files, FILES)]
    if done:
        out += ["", "## Commits", *_list(done, COMMITS)]
    return Page(title, day, _body(out), "\n".join([title, *asked, *done, *(t for _, t in c.own)]),
                [f"[[{pid}]]"] if pid else [])


def _body(lines: list[str]) -> str:
    """The page body, redacted once more as a whole, ending in one newline."""
    return chats.redact("\n".join(lines)).rstrip("\n") + "\n"


def page_id(source: str, path: Path, page: Page) -> str:
    """Stable for a transcript: its first turn's day and title, and a hash of its id."""
    h = hashlib.sha1(_key(source, path).encode("utf-8")).hexdigest()[:6]
    slug = slugify(page.title)[:40].rstrip("-")
    return "-".join(x for x in ("chat", page.day, slug, h) if x)


def _core(body: str) -> str:
    """A page body as hashed: no `Topics:` line (topics.place adds it), LF line ends."""
    body = body.replace("\r\n", "\n")
    return topics._topics_line(body, None, "\n")


def _hash(body: str) -> str:
    return hashlib.sha256(_core(body).encode("utf-8")).hexdigest()[:16]


# ---- one pass ---------------------------------------------------------------------------------

class Ctx:
    """What one pass looks up at most once: settings, the project pages and (for exports) the
    chat pages an earlier import wrote."""

    def __init__(self, vault: Vault, settings: privacy.Settings | None = None):
        self.vault = vault
        # an imported AI chat is global unless its category says otherwise
        self.settings = replace(settings or privacy.load_settings(), visible_to_claude=True)
        self.folder = chat_archive.folder()  # where a new page goes (C5: the archive)
        self._projects: dict[str, str] | None = None
        self._pages: dict | None = None
        self._excluded: dict[str, bool] = {}
        self._off: set[str] | None = None  # A24: the session ids marked off
        self._off_pages: set[str] | None = None
        self._heads: dict | None = None  # path -> (source, opening, start)
        self._openings: dict = {}  # opening -> [(source, start, path)]

    def audience(self, text: str) -> str:
        return self.judge(text)[1]

    def judge(self, text: str) -> tuple[str | None, str]:
        """(the sensitive category `text` falls in or None, that category's audience)."""
        cat = privacy.category("", "/", text)[0]
        return cat, privacy.audience(cat, self.settings)

    def excluded(self, cwd: str) -> bool:
        if cwd not in self._excluded:
            self._excluded[cwd] = bool(cwd) and taint.excluded(cwd) is not None
        return self._excluded[cwd]

    def marked_off(self, name: str) -> bool:
        """A24: did the prime hook mark this session off (prime_hook.OFF)? `name`: a
        transcript's file stem, the session id itself or ending in `-<id>` (Codex)."""
        if self._off is None:
            try:
                self._off = {p.name for p in (self.vault.root / prime_hook.OFF).iterdir()}
            except OSError:
                self._off = set()
        return name in self._off or any(name.endswith("-" + i) for i in self._off)

    def off_pages(self) -> set[str]:
        """A24: the chat pages written for a session before it was marked off (`marked_off`),
        by the chat_saved ledger; the event log is read only when a session is marked."""
        if self._off_pages is None:
            self._off_pages = set()
            # marked_off("") only loads the marks
            ledger = _ledger(EventLog(self.vault).iter_events()) if (
                self.marked_off("") or self._off) else {}
            for (_, cid), p in ledger.items():
                if isinstance(cid, str) and self.marked_off(cid.rpartition("/")[2]):
                    self._off_pages.update(i for i in p.get("pages") or () if isinstance(i, str))
        return self._off_pages

    def project(self, name: str) -> str | None:
        """A non-private project page whose id or title is `name` (as slugs)."""
        if self._projects is None:
            self._projects = {}
            for n in self.vault.iter_notes("project"):
                if not is_private(n):
                    for k in (n.id, str(n.fields.get("title") or "")):
                        if s := slugify(k):
                            self._projects.setdefault(s, n.id)
        return self._projects.get(slugify(name))

    def duplicate(self, source: str, c: Chat, day: str) -> str | None:
        """A chat page from before (the 15 Nov import, not this module's) of the same app whose
        first `**Me:**` line matches `c`'s first turn, dated within a day of it."""
        if self._pages is None:
            self._pages = {}
            for p in self.vault.note_paths("resource"):  # resources/ and archive/chats/
                try:
                    with p.open("rb") as f:
                        head = f.read(4096).decode("utf-8", "replace")
                except OSError:
                    continue
                tl = topics.TL_RE.search(head)
                src = _SOURCE.search(tl[0]) if tl and MINE not in tl[0] else None
                me = _ME.search(head)
                if src and me and (k := _match(me[1])):
                    d = _DATE.search(tl[0])
                    self._pages.setdefault((src[1].casefold(), k), []).append(
                        (d[1] if d else "", p.stem))
        k = _match(c.head)
        for d, pid in self._pages.get((LABELS[source].casefold(), k), []) if k else []:
            try:
                near = abs((date.fromisoformat(d) - date.fromisoformat(day)).days) <= 1
            except ValueError:  # no date, or not a real one
                near = True
            if near:
                return pid
        return None

    def partner(self, path: Path) -> Path | None:
        """For a claude.ai export conversation its live Chat-tab log, for a live log its export
        conversation: the one other-kind chat with the same opening started within SAME_CHAT,
        and `path` the only such chat for it; None when none or several match."""
        if self._heads is None:
            self._heads = {}
            for folder, s in (EXPORT, (chats.LIVE, chats.LIVE)):
                for p in (chats.export_root() / folder).glob("*.jsonl"):
                    if h := _opening(p):
                        self._heads[p] = (s, *h)
                        self._openings.setdefault(h[0], []).append((s, h[1], p))

        def near(p: Path) -> list:
            s, k, t = self._heads[p]
            return [q for s2, t2, q in self._openings[k] if s2 != s and abs(t2 - t) <= SAME_CHAT]
        m = near(path) if path in self._heads else []
        return m[0] if len(m) == 1 and near(m[0]) == [path] else None


def _said(text: str) -> str:
    return " ".join(chats.redact(text).split())[:OPENING]


def _opening(p: Path) -> tuple[str, datetime] | None:
    """A chat's first message as compared (_said) and its time; None without one."""
    try:
        with closing(chats._turns(p)) as ts:
            first = next(((t0, t) for r, t0, t in ts if r == "user"), None)
    except OSError:
        return None
    w = _when(first[0]) if first else None
    return (_said(first[1]), w) if w else None


def _merged(export: Path, live: Path, pace: Callable[[], None]) -> Chat:
    """One chat from both: the export's turns, then the live log's turns after the export's
    last message that follow the last live turn the export holds (each turn once)."""
    c, _ = read(export, None, pace)
    ex = chats.turns(export)
    said = [_said(t) for r, _, t in ex if r == "user"]
    end = max((w for _, ts, _ in ex if (w := _when(ts))), default=None)
    j, new = 0, []
    for r, ts, t in chats.turns(live):
        if r != "user":
            continue
        if (k := _said(t)) in said[j:]:  # in order, so a repeated "go on" later is still new
            j += said[j:].index(k) + 1
            new.clear()
        elif end is not None and (w := _when(ts)) is not None and w > end:
            new.append((ts, t))
    for ts, t in new:
        c.turn(ts, t)
        c.last = ts
    return c


def _pid(e) -> str | None:
    """The page of a ledger entry."""
    pages = e.get("pages") if isinstance(e, dict) else None
    return pages[0] if isinstance(pages, list) and pages and isinstance(pages[0], str) else None


def _match(line: str) -> str:
    return re.sub(r"\W+", "", chats.redact(line).casefold())[:60]


def _ledger(events) -> dict:
    """(source, chat id) -> the payload of its latest chat_saved event, or UNDONE when that
    event was voided (`keepfold undo`): the chat is not imported again."""
    evs = list(events)
    voided = {e.get("ref") for e in evs if e.get("kind") == "void"}
    out = {}
    for e in evs:
        p = e.get("payload")
        if e.get("kind") == "log" and isinstance(p, dict) and p.get("kind") == "chat_saved":
            out[(p.get("source"), p.get("chat_id"))] = UNDONE if e.get("id") in voided else p
    return out


def made_private(vault: Vault, events, settings: privacy.Settings | None = None) -> list[dict]:
    """The imported chats this module wrote private for a sensitive category and that are still
    private, newest first: [{id, title, app, date, category}]; titles plain (`render.plain`),
    never chat text. The category is the one recorded when the page was written; a page
    written before it was recorded is judged again from its body under the settings of now
    (listed only when that category is local-only)."""
    ctx = Ctx(vault, settings)
    out: dict[str, dict] = {}
    for p in _ledger(events).values():
        pid = _pid(p)
        if not p.get("auto") or pid is None or pid in out:
            continue
        try:
            n = vault.read("resource", pid)
        except VaultError:
            continue
        if n.fields.get("private") is not True:
            continue
        cat = p.get("category")
        if not isinstance(cat, str):
            cat, aud = ctx.judge(n.body)
            if aud != "local" or cat is None:
                continue
        out[pid] = {"id": pid, "title": render.plain(str(n.fields.get("title") or pid)),
                    "app": LABELS.get(p.get("source"), str(p.get("source") or "")),
                    "date": str(n.fields.get("date") or ""), "category": cat}
    return sorted(out.values(), key=lambda r: (r["date"], r["id"]), reverse=True)


def _event(evlog: EventLog, ledger: dict, source: str, cid: str, title: str, pid: str,
           h: str, category: str | None = None) -> None:
    """The chat_saved ledger line. The key makes a retry after a crash a no-op; `ledger` is
    kept current for this pass. `category`: the sensitive category that made the page
    private (`made_private` lists it)."""
    key = "chat-import:" + hashlib.sha1(f"{source}:{cid}".encode()).hexdigest()[:16] + f":{h}"
    payload = {"kind": "chat_saved", "source": source, "chat_id": cid, "title": title,
               "pages": [pid], "auto": True, "hash": h, **({"category": category} if category
                                                            else {})}
    evlog.append(session="import", kind="log", source="imported", key=key,
                 fields={"payload": payload})
    ledger[(source, cid)] = payload


class _Edited(Exception):
    pass


def _rewrite(vault: Vault, path: Path, page: Page, was: str, private: bool) -> None:
    """Replace the page body, keeping its `Topics:` line and frontmatter (the title follows the
    heading unless the owner renamed it); `private` sets `private: true` (never unset).
    _Edited when the body is no longer the one hashed `was`."""
    def change(text: str) -> str:
        parts = _split(text)
        if parts is None:
            raise _Edited
        head, fm, close, body = parts
        if _hash(body) != was:
            raise _Edited
        nl = _newline(text)
        m = topics.TOP_RE.search(body)
        data = _load_fm(fm)
        h1 = body.lstrip().split("\n", 1)[0].removeprefix("# ").strip()
        retitle = data.get("title") == h1 != page.title
        if retitle or (private and not truthy(data.get("private"))):
            if retitle:
                data["title"] = page.title
            if private:
                data["private"] = True
            fm = _dump_fm(data, nl)
        new = topics._topics_line(page.body, m[0] if m else None, "\n")
        return head + fm + close + new.replace("\n", nl)
    vault.update_text(path, change)


def one(vault: Vault, evlog: EventLog, ctx: Ctx, ledger: dict, source: str, path: Path,
        prev: dict | None = None, pace: Callable[[], None] = lambda: None) -> tuple[str, dict]:
    """Write or update one transcript's page from its STATE record `prev`; returns what
    happened (a count's name) and the new record."""
    cid = chats._cid(path)
    had = ledger.get((source, cid))
    if had is UNDONE:  # the owner undid its import: never brought back
        return "undone", {}
    if had is not None and not had.get("auto"):
        return "saved_by_hand", {}
    if prev and isinstance(prev.get("adopted"), str):
        return "earlier_import", prev
    c, rec = read(path, prev, pace, source)
    if not c.n:
        return "empty", rec
    if ctx.excluded(c.cwd):
        return "excluded_folder", rec
    keys = [(source, cid)]  # the ledger keys this page is written for
    if source in (EXPORT[1], chats.LIVE) and (other := ctx.partner(path)) is not None:
        okey = (EXPORT[1] if source == chats.LIVE else chats.LIVE, chats._cid(other))
        o = ledger.get(okey)
        mine, theirs = _pid(had), _pid(o)
        # a hand-saved partner, or two pages already (written before this rule), stay apart
        if (o is None or o.get("auto")) and not (mine and theirs and mine != theirs):
            c = _merged(*((path, other) if source == EXPORT[1] else (other, path)), pace)
            keys.append(okey)
            had = had if mine else o
    if c.off or ctx.marked_off(path.stem):  # A24: STATE keeps no text of it either
        return "off_chat", {**rec, "chat": Chat(n=c.n, off=True).dump()} if rec else rec
    page = build(source, path, c, ctx)
    cat, audience = ctx.judge(page.text)
    if audience == "skip":
        return "skipped_category", rec
    # D-15: an import is no proof the owner typed a //tag (tags.SAID), so its tags only
    # propose; a private (local-only) chat's stay in its private page, off the shared me/* pages
    if c.tags and audience != "local":
        try:
            tags.apply_in(vault, [tuple(t) for t in c.tags], evlog, forget_said=False,
                          said=tags.SAID["import"])
        except (OSError, ValueError, VaultError) as e:  # the page is still written
            log.warning("chat import: the //tags of %s not saved (%s)", cid,
                        getattr(e, "code", type(e).__name__))
    pid = _pid(had)
    if pid is None and source not in AGENTS and (old := ctx.duplicate(source, c, page.day)):
        # kept in STATE only, not the event log: thousands of these would each cost an append
        return "earlier_import", {"adopted": old}
    pid = pid or page_id(source, path, page)
    h, md = _hash(page.body), vault.path_for("resource", pid)

    def mark() -> None:
        for s, i in keys:
            e = ledger.get((s, i))
            if e is None or e.get("hash") != h or _pid(e) != pid:
                _event(evlog, ledger, s, i, page.title, pid, h,
                       cat if audience == "local" else None)
    if not md.is_file():
        if had is not None:
            return "deleted_by_you", rec  # never brought back
        fields = {"title": page.title, "kind": "resource", "resource_type": "other",
                  "status": "reference", "covers": [], "related": page.related,
                  "date": page.day, **({"private": True} if audience == "local" else {})}
        vault.create_note("resource", pid, fields, page.body, folder=ctx.folder)
        mark()
        return "saved", rec
    now = _hash(vault.read("resource", pid).body)
    if now == h:
        mark()
        return "unchanged", rec
    if had is None or had.get("hash") != now:
        return "edited_by_you", rec
    try:
        _rewrite(vault, md, page, now, audience == "local")
    except _Edited:
        return "edited_by_you", rec
    mark()
    return "updated", rec


def run(vault: Vault, *, progress: Callable | None = None,
        cancelled: Callable[[], bool] | None = None,
        settings: privacy.Settings | None = None, now: float | None = None) -> dict:
    """One pass over pending() under LOCK (VaultError("busy") while another runs), newest
    first, STATE saved after each transcript. Returns counts: saved, updated, unchanged, failed
    and each kind of skip; `stopped` when cancelled. `progress(done, total, seconds)` follows
    each chat."""
    taint.check_config("the chat import")
    with vault.job_lock(LOCK, 0, BUSY), closing(State(vault)) as state:
        todo = pending(vault, now, state.sigs())
        counts: Counter = Counter()
        if not todo:
            return {}
        evlog = EventLog(vault)
        ledger = _ledger(evlog.iter_events())
        ctx, t0, pace = Ctx(vault, settings), time.monotonic(), Pace(cancelled)
        if ctx.folder:
            chat_archive.ensure_ignored(vault)  # an older vault: before a page lands there
        for k, (source, path, sig) in enumerate(todo, 1):
            key = _key(source, path)
            try:
                if cancelled is not None and cancelled():
                    raise _Stop
                pace()
                what, rec = one(vault, evlog, ctx, ledger, source, path, state.get(key), pace)
            except _Stop:
                counts["stopped"] = 1
                break
            except (OSError, ValueError, VaultError) as e:
                log.warning("chat import: %s not imported (%s)", chats._cid(path),
                            getattr(e, "code", type(e).__name__))
                what, rec = "failed", {}  # read from the start once it changes
                if isinstance(e, OSError) or getattr(e, "code", None) in TRANSIENT:
                    sig = None  # tried again next pass
            counts[what] += 1
            if sig is not None:
                state.put(key, sig, rec)
            if progress is not None:
                progress(k, len(todo), time.monotonic() - t0)
        if n := counts["saved"] + counts["updated"]:
            vault.append_log(local_day(datetime.now(UTC)), "chat",
                             f"{n} chat{'s' * (n != 1)} saved automatically")
        return dict(counts)


def eta(done: int, total: int, seconds: float) -> str:
    """The progress line: "12 of 300 chats", plus a percent and the time left past FEW."""
    line = f"{done} of {total} chats"
    if total <= FEW:
        return line
    left = seconds / max(done, 1) * (total - done)
    when = f"{round(left)} s" if left < 90 else f"{round(left / 60)} min"
    return f"{line} ({done * 100 // total}%), about {when} left"
