"""Past conversations, read-only, for /keepfold history and /keepfold save.

Claude Code transcripts are `<root>/<project-dir>/<session-id>.jsonl` (root: ~/.claude/projects,
or KEEPFOLD_CHATS_DIR). Chats from claude.ai, ChatGPT, Gemini and Perplexity come from their data
exports, which `import_export` converts into the same line format under
`export_root()/<source folder>/` (EXPORT_DIRS; claude.ai's source is `claude-chat`). A chat
in a Claude app with no transcript of its own (Claude Desktop's Chat tab) is logged there live
over MCP (`log_live`, folder and source LIVE). Only user/assistant text survives: tool calls and
results, sidechains, meta lines and system-reminder/command wrappers are dropped.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from itertools import chain, islice
from pathlib import Path

from keepfold import atomic
from keepfold.events import live
from keepfold.secret_rules import mask
from keepfold.vault import VaultError

PART_CHARS, LINE_CHARS, FIRST_LINE_CHARS, MAX_LIMIT = 12_000, 1_500, 120, 50
LIVE_SECONDS = 120  # the newest transcript touched this recently is the chat being written
_WRAPPER = re.compile(r"<(system-reminder|task-notification|command-[\w-]+|local-command-[\w-]+)"
                      r"\b[^>]*>.*?</\1>", re.S)


def root() -> Path:
    return Path(os.environ.get("KEEPFOLD_CHATS_DIR") or Path.home() / ".claude" / "projects")


def export_root() -> Path:
    """Where imported chat-export conversations live (outside the vault and the repo)."""
    return Path(os.environ.get("KEEPFOLD_CHAT_EXPORT_DIR")
                or Path.home() / ".keepfold" / "claude-chat")


EXPORT_PROJECT = "claude-ai"
# import source -> (its folder under export_root(), the source list_chats reports)
EXPORT_DIRS = {"claude": (EXPORT_PROJECT, "claude-chat"), "chatgpt": ("chatgpt", "chatgpt"),
               "gemini": ("gemini", "gemini"), "perplexity": ("perplexity", "perplexity")}
LIVE = "claude-live"  # chats a Claude app logs over MCP (keepfold_prime, keepfold_log chat_turn)
LIVE_ID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")  # a live chat's id: a ULID the server made
SOURCES = ("claude-code", *(s for _, s in EXPORT_DIRS.values()), LIVE)
_UUID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
EXPECTED = ("Expected a claude.ai or ChatGPT export (the .zip, or its conversations.json), a "
            "Google Takeout of Gemini Apps activity (My Activity/Gemini Apps/MyActivity.json or "
            ".html), a Perplexity data export (conversations-*.json), or "
            "Perplexity threads exported as Markdown (.md files, a folder or a zip of them).")

# How to get each export, for the app's guided import and docs/GUIDE.md ("google-activity"
# feeds keepfold.browse.takeout, not import-chats).
GUIDES = {
    "claude": {
        "name": "Claude (claude.ai)",
        "link": "https://support.claude.com/en/articles/9450526-how-can-i-export-my-claude-ai-data",
        "steps": ["On claude.ai (web or desktop app, not the phone app) open Settings > Privacy.",
                  "Click Export data.",
                  "Open the email from Anthropic and download the .zip (the link works for "
                  "24 hours)."],
        "wait": "a few minutes",
        "pick": "the downloaded .zip, as it is"},
    "chatgpt": {
        "name": "ChatGPT",
        "link": "https://chatgpt.com/#settings/DataControls",
        "steps": ["On chatgpt.com click your profile picture > Settings > Data controls.",
                  "Next to Export data click Export, then Confirm export.",
                  "Open the email from OpenAI and download the .zip (the link works for "
                  "24 hours)."],
        "wait": "minutes to a few hours (OpenAI says up to 7 days)",
        "pick": "the downloaded .zip, as it is (it holds conversations.json)"},
    "gemini": {
        "name": "Gemini",
        "link": "https://takeout.google.com",
        "steps": ["Open takeout.google.com and click Deselect all.",
                  "Tick My Activity, click All activity data included, click Deselect all, "
                  "tick Gemini Apps, then OK.",
                  "Leave Activity records as HTML (the default) or set them to JSON under "
                  "Multiple formats; Keepfold reads both.",
                  "Click Next step, keep 'Send download link via email' and .zip, then "
                  "Create export.",
                  "Download the .zip from Google's email (the link works for 7 days)."],
        "wait": "usually the same day; up to a few days",
        "pick": "the Takeout .zip, as it is"},
    "perplexity": {
        "name": "Perplexity",
        "link": "https://www.perplexity.ai/account/details",
        "steps": ["On perplexity.ai open Settings > Account (perplexity.ai/account/details).",
                  "Click Export my data. If Settings > Account has no such button, ask "
                  "Perplexity's privacy contact (named in its privacy policy) for your data.",
                  "Download the .zip from Perplexity's email soon: the link expires.",
                  "For a few threads only: open each one, choose Export > Markdown from its "
                  "menu, and save the .md files in one folder."],
        "wait": "minutes to a day for the account export; instant for one thread",
        "pick": "the export .zip as it is (it holds conversations-*.json), or the .md files "
                "or their folder"},
    "google-activity": {
        "name": "Google activity (Search, YouTube, Chrome)",
        "link": "https://takeout.google.com",
        "steps": ["Open takeout.google.com and click Deselect all.",
                  "Tick My Activity, click All activity data included, click Deselect all, "
                  "tick Search, YouTube and Chrome, then OK.",
                  "Leave Activity records as HTML (the default) or set them to JSON under "
                  "Multiple formats; Keepfold reads both.",
                  "Click Next step, keep 'Send download link via email' and .zip, then "
                  "Create export.",
                  "Download the .zip from Google's email (the link works for 7 days)."],
        "wait": "usually the same day; up to a few days",
        "pick": "the Takeout .zip, as it is; it lands private, under the browse rules"},
}


class Got(tuple):
    """import_export's result; `skipped` counts Takeout HTML rows whose date was unreadable.
    The import report (importers.report_line): `files` read, conversations `read`,
    `duplicate` (one id twice in the export: the latest kept) and `empty` (no text or id)."""
    skipped = files = read = duplicate = empty = 0

    def report(self) -> dict:
        n = self[1] if len(self) == 2 else self[1] + self[2]
        return {"read": self.read, "imported": n, "private": 0, "duplicate": self.duplicate,
                "reasons": {"no text or no id": self.empty,
                            "unreadable date": self.skipped}, "files": self.files}


def _stamp(t) -> float:
    """A conversation's update time as a number, for picking the latest copy (0 if unreadable)."""
    try:
        return float(t) if isinstance(t, int | float) else \
            datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, TypeError):
        return 0.0


def _latest(convs, got: Got) -> list:
    """C6: one copy per conversation id (a split export may hold one twice): the most recently
    updated, in first-seen order. Counts `read` and `duplicate` on `got`."""
    best: dict = {}
    for cid, updated, turns in convs:
        got.read += 1
        k = cid if isinstance(cid, str) else ("", got.read)  # no id: kept, _save skips it
        if k in best:
            got.duplicate += 1
            if _stamp(updated) <= _stamp(best[k][1]):
                continue
        best[k] = (cid, updated, turns)
    return list(best.values())


def import_export(src: Path, source: str | None = None, dry_run: bool = False) -> tuple:
    """Convert a chat export (claude.ai, ChatGPT, Gemini Takeout or Perplexity Markdown; the
    .zip, a folder or one file) into one redacted transcript per conversation under
    export_root()/<source folder>/; re-importing overwrites (dedupe on source and id).
    `source` forces the service when detection fails. Returns (source, conversations); with
    `dry_run`, writes nothing and returns (source, new, already imported)."""
    src = Path(src)
    if source is not None and source not in EXPORT_DIRS:
        raise VaultError("invalid_argument", f"--source must be one of {', '.join(EXPORT_DIRS)}.")
    from keepfold.browse import takeout
    from keepfold.importers import _files, json_value  # importers imports this module
    files = _files(src)
    if src.suffix.lower() == ".zip" or src.is_dir():
        files = [f for f in files if _wanted(f[0])]
    if not files:
        raise VaultError("invalid_argument", f"Found no chat export in {src.name}. {EXPECTED}")
    js = [f for f in files if not f[0].lower().endswith(".md")]
    pplx = not js or (source == "perplexity" and len(js) < len(files))  # Markdown threads
    name = src.name if pplx or len(js) > 1 else js[0][0]
    skipped, parts, got = 0, [], Got()
    try:
        if pplx:
            mds = [(n, read().decode("utf-8"), mtime) for n, _, mtime, read in files
                   if n.lower().endswith(".md")]
        # C6: every JSON (or Takeout HTML) file of the export, not only the first one
        for name, _, _, read in [] if pplx else js:
            html_file = name.lower().endswith((".html", ".htm"))  # Takeout's HTML Gemini rows
            part, n = takeout.html_rows(read(), name) if html_file else (json_value(read), 0)
            skipped += n
            if isinstance(part, dict) and isinstance(part.get("conversations"), list):
                part = part["conversations"]  # Perplexity's account export wraps its threads
            if not isinstance(part, list | Iterator):
                raise VaultError("invalid_argument", f"{name} should hold a list. {EXPECTED}")
            parts.append(part if isinstance(part, list) else _named(name, part))
            got.files += 1
    except (OSError, ValueError, zipfile.BadZipFile) as e:
        raise VaultError("invalid_argument", f"Cannot read {name}: {e}") from None
    name = name if len(js) == 1 else src.name
    if pplx:
        got.files = len(mds)
        out = Got(_save("perplexity", _latest(_perplexity(mds), got), dry_run, got))
        out.__dict__.update(got.__dict__)
        return out
    rows = chain.from_iterable(parts)  # C11: a big file's rows stream in as they convert
    head = list(islice(rows, 1))
    data = chain(head, rows)
    source = source or _detect(head)
    if source is None:
        raise VaultError("invalid_argument", f"Cannot tell which service {name} comes from "
                         f"(is it empty?); add --source {'|'.join(EXPORT_DIRS)}. {EXPECTED}")
    if source == "gemini":
        data = list(data)  # _gemini keeps each of its rows anyway
    if source == "gemini" and data and not any(map(_is_gemini, data)):
        raise VaultError("invalid_argument", f"{name} holds no Gemini Apps activity (Search, "
                         "YouTube or Chrome rows are browsing, not chats); import it as "
                         "google-activity instead.")
    convs = {"claude": _claude, "chatgpt": _chatgpt, "gemini": _gemini,
             "perplexity": _perplexity_json}[source](data)
    out = Got(_save(source, _latest(convs, got), dry_run, got))
    out.__dict__.update(got.__dict__, skipped=skipped)
    return out


def _named(name: str, rows):
    """A streamed file's rows: an error midway names the file, as for one read whole."""
    try:
        yield from rows
    except (OSError, ValueError, zipfile.BadZipFile) as e:
        raise VaultError("invalid_argument", f"Cannot read {name}: {e}") from None


def _wanted(name: str) -> bool:
    """Chat files in a zip or folder; a top-level MyActivity.json counts, as in
    `importers.detect`, which checks it is Gemini's."""
    from keepfold.browse import takeout
    n = name.replace("\\", "/").lower()
    base = n.rsplit("/", 1)[-1]
    return (base.startswith("conversations") and base.endswith(".json")) or base.endswith(".md") \
        or (bool(takeout.activity_file(n)) and ("gemini" in n or "/" not in n))


def _is_gemini(e) -> bool:
    """A Gemini row in any language: "Gemini Apps", "Apps de Gemini", "Gemini-Apps"..."""
    return isinstance(e, dict) and ("Gemini" in str(e.get("header", "")) or any(
        "Gemini" in str(p) for p in e.get("products") or ()))


def _detect(data: list) -> str | None:
    d = data[0] if data and isinstance(data[0], dict) else {}
    if "mapping" in d:
        return "chatgpt"
    if "chat_messages" in d:
        return "claude"
    if "entries" in d and "context_uuid" in d:
        return "perplexity"
    if _is_gemini(d):
        return "gemini"
    return None


def _iso(t) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat() if isinstance(t, int | float) \
        else str(t or "")


def _claude(data):
    for c in data:
        if not isinstance(c, dict):
            continue
        turns = []
        for m in c.get("chat_messages") or []:
            role = {"human": "user", "assistant": "assistant"}.get(m.get("sender"))
            blocks = [b.get("text") for b in m.get("content") or []
                      if isinstance(b, dict) and b.get("type") == "text"
                      and isinstance(b.get("text"), str)]
            if role:
                turns.append((role, str(m.get("created_at") or ""),
                              "\n".join(blocks) or m.get("text") or ""))
        yield c.get("uuid"), c.get("updated_at"), turns


def _chatgpt(data):
    """Walks `mapping` from current_node up to the root: the branch the user last saw."""
    for c in data:
        nodes = c.get("mapping") if isinstance(c, dict) else None
        if not isinstance(nodes, dict):
            continue
        turns, seen = [], set()
        nid = c.get("current_node") or next(reversed(nodes), None)
        while isinstance(nid, str) and nid in nodes and nid not in seen:
            seen.add(nid)
            node = nodes[nid] if isinstance(nodes[nid], dict) else {}
            m = node.get("message") or {}
            role = (m.get("author") or {}).get("role")
            if role in ("user", "assistant") and not (m.get("metadata") or {}).get(
                    "is_visually_hidden_from_conversation"):
                parts = (m.get("content") or {}).get("parts") or []
                turns.append((role, _iso(m.get("create_time")),
                              "\n".join(p for p in parts if isinstance(p, str))))
            nid = node.get("parent")
        yield c.get("conversation_id") or c.get("id"), c.get("update_time"), turns[::-1]


# shortcut: unconfirmed wording for non-English prompt labels, verify with a real export: the
# label is "Prompted " in English, else any "<label>: " (or full-width "：") up to 40
# characters; a non-prompt row with a colon slips in as a user line. Add labels if seen.
_PROMPT = re.compile(r"(?:Prompted |[^:：\n]{1,40}(?:: |：))(.+)", re.S)
_GEMINI_CHAT = re.compile(r"/app/(?:c/)?([A-Za-z0-9-]{8,64})")
_TAG = re.compile(r"<[^>]+>")
# a heuristic, the usual web-analytics session timeout, not a measured boundary
GEMINI_SESSION_GAP = timedelta(minutes=30)


def _gemini(data):
    """One My Activity row per prompt. Rows carry no conversation id in the exports seen, so
    a row joins its titleUrl's chat when it has one; the other prompts form a chat until
    GEMINI_SESSION_GAP passes between two of them. Such a chat's id is its first prompt's UTC
    minute, `YYYY-MM-DD-HHMM` (unique: chats start more than the gap apart)."""
    convs: dict[str, list] = {}
    loose = []  # (time, row, prompt match) of rows without a chat id
    for e in data:
        if not _is_gemini(e) or not isinstance(e.get("time"), str):
            continue  # Search, YouTube and Chrome rows are browsing, never chat lines
        m = _PROMPT.match(str(e.get("title") or ""))
        if not m:
            continue
        if chat := _GEMINI_CHAT.search(str(e.get("titleUrl") or "")):
            convs.setdefault(chat.group(1), []).extend(_gemini_turns(e, m))
            continue
        try:
            t = datetime.fromisoformat(e["time"].replace("Z", "+00:00"))
        except ValueError:
            continue
        loose.append((t if t.tzinfo else t.replace(tzinfo=UTC), e, m))
    loose.sort(key=lambda x: x[0])
    cid = prev = None
    for t, e, m in loose:
        if prev is None or t - prev > GEMINI_SESSION_GAP:
            cid = t.astimezone(UTC).strftime("%Y-%m-%d-%H%M")
        prev = t
        convs.setdefault(cid, []).extend(_gemini_turns(e, m))
    for cid, turns in convs.items():
        turns.sort(key=lambda t: t[1])  # newest-first rows; stable, so a prompt stays first
        yield cid, turns[-1][1], turns


def _gemini_turns(e, m) -> list:
    answer = " ".join(html.unescape(_TAG.sub(" ", h["html"]))
                      for h in e.get("safeHtmlItem") or []
                      if isinstance(h, dict) and isinstance(h.get("html"), str))
    return [("user", e["time"], m.group(1)), ("assistant", e["time"], answer)]


_PPLX_SKIP = re.compile(r"\s*(<.*>|\[\^[^\]]+\]:.*|---)\s*$")  # logo, ⁂ divider, footnotes
_CITE = re.compile(r"\[\^[^\]]+\]")
# a JSON answer's [1]: a 1-3 digit number in brackets right after a word, punctuation or marker
_NUM_CITE = re.compile(r"(?<=[\w.,;:!?)\]'\"])\[\d{1,3}\](?![\d(])")
_CODE = re.compile(r"(```.*?(?:```|\Z)|`[^`\n]*`)", re.S)  # fences (even unclosed), inline code


def _perplexity(files):
    """A thread's Markdown export: each `# ` line is a question, the lines up to the next one
    its answer. A thread's id is a hash of its whole text: threads that open with the same
    question stay apart, and the same file renamed or imported again is the same thread (a
    re-export after follow-ups is a new one)."""
    for _, text, mtime in files:
        ts, turns = _iso(mtime), []
        for line in text.splitlines():
            if line.startswith("# "):
                turns.append(("user", ts, line[2:].strip()))
            elif turns and not _PPLX_SKIP.match(line):
                if turns[-1][0] == "user":
                    turns.append(("assistant", ts, ""))
                turns[-1] = (*turns[-1][:2], turns[-1][2] + _CITE.sub("", line) + "\n")
        yield hashlib.sha1(text.encode("utf-8")).hexdigest()[:16], mtime, turns


def _uncite(text: str) -> str:
    """`text` without citation markers like [1], except inside inline code and code fences."""
    return "".join(part if i % 2 else _NUM_CITE.sub("", part)
                   for i, part in enumerate(_CODE.split(text)))


def _perplexity_json(data):
    """Perplexity's account export (conversations-<stamp>.json): a thread's entries are its
    question/answer turns, oldest first. Ids are context_uuid; `_save` keeps the id of the same
    thread imported before as Markdown (hashed id)."""
    for c in data:
        if not isinstance(c, dict):
            continue
        turns = []
        for e in c.get("entries") or []:
            if isinstance(e, dict):
                ts = str(e.get("created_at") or "")
                a = e.get("answer")
                turns += [("user", ts, e.get("query")),
                          ("assistant", ts, _uncite(a) if isinstance(a, str) else a)]
        yield c.get("context_uuid"), c.get("updated_at"), turns


def _pplx_key(turns) -> str | None:
    """A thread's every question and its whole first answer, from (role, text) pairs,
    lowercased letters and digits only, so its Markdown and JSON exports match; threads that
    only open the same way ("hi") get different keys."""
    turns = [(r, re.sub(r"\W+", "", t.lower())) for r, t in turns]
    qs = [t for r, t in turns if r == "user"]
    a = next((t for r, t in turns if r == "assistant"), "")
    return "\0".join([a, *qs]) if qs and qs[0] else None


def _save(source: str, convs, dry_run: bool = False, got: Got | None = None) -> tuple:
    out = export_root() / EXPORT_DIRS[source][0]
    if not dry_run:
        out.mkdir(parents=True, exist_ok=True)
    n = old = 0
    seen = set()  # a conversation twice in one export is written and counted once
    known = {}  # Perplexity: a thread stored from the other export form (Markdown or JSON)
    if source == "perplexity" and out.is_dir():
        for p in out.glob("*.jsonl"):
            known.setdefault(_pplx_key((r, t) for r, _, t in _turns(p)), p.stem)
        known.pop(None, None)
    for cid, updated, turns in convs:
        lines = [json.dumps({"type": r, "message": {"content": redact(t)}, "timestamp": ts})
                 for r, ts, t in turns if isinstance(t, str) and t.strip()]
        if known and isinstance(cid, str):
            key = _pplx_key((r, redact(t)) for r, _, t in turns
                            if isinstance(t, str) and t.strip())
            had = known.get(key)
            if had and (len(had) == 16) != (len(cid) == 16):  # Markdown ids are 16-hex hashes
                cid = had
                del known[key]  # a stored thread takes at most one incoming thread
        if not isinstance(cid, str) or not _UUID.match(cid) or not lines or cid in seen:
            if got is not None:
                got.empty += not (isinstance(cid, str) and cid in seen)
            continue
        seen.add(cid)
        p = out / f"{cid}.jsonl"
        if dry_run:
            old += p.is_file()
            n += not p.is_file()
            continue
        atomic.replace(p, "\n".join(lines) + "\n", fsync=False, newline=None)  # one per chat: hot
        try:  # list order follows file times, so give each file its conversation's time
            t = updated if isinstance(updated, int | float) else \
                datetime.fromisoformat(str(updated).replace("Z", "+00:00")).timestamp()
            os.utime(p, (t, t))
        except (ValueError, OverflowError, OSError):
            pass
        n += 1
    return (source, n, old) if dry_run else (source, n)


PRIVATE = "[private]"
_PRIVATE_TAG = re.compile(r"<\s*(/?)\s*private\s*>", re.I)


def drop_private(text: str) -> str:
    """`text` with each `<private>…</private>` span (any case, across lines, nested or
    repeated) replaced by PRIVATE: what a person marks private is never saved. An unclosed
    `<private>` drops everything after it, the safe reading; a stray `</private>` stays."""
    out, depth, pos = [], 0, 0
    for m in _PRIVATE_TAG.finditer(text):
        if not m[1]:
            if not depth:
                out.append(text[pos:m.start()])
            depth += 1
        elif depth:
            depth -= 1
            if not depth:
                out.append(PRIVATE)
                pos = m.end()
    return "".join([*out, PRIVATE if depth else text[pos:]])


def redact(text: str) -> str:
    """`text` with private spans dropped (drop_private) and secrets masked: every copy of chat
    text Keepfold keeps (live logs, imported exports, chat pages, the recall log) goes through
    here."""
    return mask(drop_private(text))


def turns(path: Path) -> list[tuple[str, str, str]]:
    """(role, timestamp, text) per conversation turn with text left after cleaning."""
    return list(_turns(path))


def _turns(path: Path):
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if text := _text(d):
                yield d["type"], str(d.get("timestamp") or ""), text


def _text(d) -> str:
    """The cleaned text of one transcript line's user or assistant turn; '' for anything else
    (a sidechain or meta line, a tool call or result, thinking, an image)."""
    if (not isinstance(d, dict) or d.get("type") not in ("user", "assistant")
            or d.get("isSidechain") or d.get("isMeta")
            or not isinstance(d.get("message"), dict)):
        return ""
    content = d["message"].get("content")
    if isinstance(content, str):
        blocks = [content]
    elif isinstance(content, list):  # tool_use, tool_result, thinking, image: dropped
        blocks = [b.get("text") for b in content
                  if isinstance(b, dict) and b.get("type") == "text"]
    else:
        return ""
    return "\n".join(_WRAPPER.sub("", b) for b in blocks if isinstance(b, str)).strip()


def path_for(chat_id: str) -> Path:
    """The transcript for `<project-dir>/<session-id>`, refused outside the two roots."""
    for base in (root().resolve(), export_root().resolve()):
        p = (base / f"{chat_id}.jsonl").resolve()
        if str(chat_id).count("/") != 1 or p.parent.parent != base:
            raise VaultError("invalid_id", "A chat id looks like <project-dir>/<session-id>, "
                             "as keepfold_chats list returns it.")
        if p.is_file():
            return p
    raise VaultError("unknown_id", f"No chat '{chat_id}'; use an id from keepfold_chats list.")


def source_of(p: Path) -> str:
    if p.parent.parent.resolve() != export_root().resolve():
        return "claude-code"
    return next((s for d, s in [*EXPORT_DIRS.values(), (LIVE, LIVE)] if d == p.parent.name),
                "claude-chat")


def log_live(cid: str, turns: list[tuple[str, str]], now: datetime) -> None:
    """Append (role, text) turns, redacted, to the live chat log `cid` as transcript lines
    stamped `now`; chat_import makes the page."""
    out = export_root() / LIVE
    out.mkdir(parents=True, exist_ok=True)
    lines = "".join(json.dumps({"type": r, "message": {"content": redact(t)},
                                "timestamp": now.isoformat()}) + "\n"
                    for r, t in turns if t.strip())
    with (out / f"{cid}.jsonl").open("a", encoding="utf-8") as f:
        f.write(lines)  # one write: a reader sees whole lines (chat_import waits for quiet)


def cowork_files() -> Iterator[Path]:
    """Claude Code transcripts of Claude Desktop's Cowork sessions, in the app's own data
    folders: %LOCALAPPDATA%/Claude-Data/* and %APPDATA%/Claude on Windows,
    ~/Library/Application Support/Claude on macOS, $XDG_CONFIG_HOME/Claude (default
    ~/.config/Claude) on Linux."""
    for var, sub in (("LOCALAPPDATA", "Claude-Data/*"), ("APPDATA", "Claude")):
        if base := os.environ.get(var):
            for d in Path(base).glob(f"{sub}/local-agent-mode-sessions"):
                yield from d.glob("**/.claude/projects/*/*.jsonl")
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Claude"
    elif sys.platform.startswith("linux"):
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "Claude"
    else:
        return
    for sub in ("local-agent-mode-sessions", "claude-code-sessions"):
        yield from (base / sub).glob("**/.claude/projects/*/*.jsonl")


def codex_files() -> Iterator[Path]:
    """OpenAI Codex CLI session rollouts: $CODEX_HOME (default ~/.codex)/sessions/YYYY/MM/DD/
    rollout-<time>-<id>.jsonl (openai/codex codex-rs/rollout/src/lib.rs SESSIONS_SUBDIR and
    rollout_file_name.rs, https://github.com/openai/codex). Shortcut: a cold rollout Codex
    compressed (.jsonl.zst) or moved to archived_sessions/ is not read (the stdlib has no zstd
    before Python 3.14); its page, if written before, stays."""
    base = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return (base / "sessions").glob("*/*/*/rollout-*.jsonl")


def gemini_files() -> Iterator[Path]:
    """Google Gemini CLI chat sessions: ($GEMINI_CLI_HOME or ~)/.gemini/tmp/<project>/chats/
    session-*.jsonl, and a legacy session-*.json that has no .jsonl of the same name (Gemini CLI
    copies one to the other when it is resumed) (google-gemini/gemini-cli packages/core/src/
    services/chatRecordingService.ts and config/storage.ts,
    https://github.com/google-gemini/gemini-cli). Sub-agent sessions (a folder under chats/)
    and the other files in tmp/ are not read."""
    base = Path(os.environ.get("GEMINI_CLI_HOME") or Path.home()) / ".gemini" / "tmp"
    for p in base.glob("*/chats/session-*.json*"):
        if p.suffix == ".jsonl" or p.suffix == ".json" and not p.with_suffix(".jsonl").exists():
            yield p


def copilot_files() -> Iterator[Path]:
    """GitHub Copilot CLI sessions: $COPILOT_HOME (default ~/.copilot)/session-state/<session
    id>/events.jsonl (https://docs.github.com/en/copilot/reference/copilot-cli-reference/
    cli-config-dir-reference, `session-state/`)."""
    base = Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")
    return (base / "session-state").glob("*/events.jsonl")


def _files() -> list[Path]:
    """Transcripts of both sources, newest mtime first, without the Claude Code one being
    written right now (G3)."""
    code = sorted(root().glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if code and time.time() - code[0].stat().st_mtime < LIVE_SECONDS:
        code = code[1:]
    return sorted(code + [p for d in [*(d for d, _ in EXPORT_DIRS.values()), LIVE]
                          for p in export_root().glob(f"{d}/*.jsonl")],
                  key=lambda p: p.stat().st_mtime, reverse=True)


def _cid(p: Path) -> str:
    return f"{p.parent.name}/{p.stem}"


def unlearned_count(learned: set[str]) -> int:
    """Listable chats (2+ user turns) not in `learned`; each file is read only up to its
    second user turn."""
    return sum(1 for p in _files() if _cid(p) not in learned and len(list(islice(
        (t for t in _turns(p) if t[0] == "user"), 2))) == 2)


def list_chats(saved: set[str], since: str | None = None, limit: int = 20,
               learned: set[str] = frozenset(), unlearned: bool = False) -> list[dict]:
    if not 1 <= limit <= MAX_LIMIT:
        raise VaultError("invalid_argument", f"limit must be from 1 to {MAX_LIMIT}.")
    if since is not None:
        try:
            date.fromisoformat(since)
        except ValueError:
            raise VaultError("invalid_argument", "since must be a date like 2026-11-13.") \
                from None
    out = []
    # shortcut: files are parsed newest-mtime first and parsing stops at `limit`; a file
    # touched after its last turn can shift the order slightly
    for p in _files():
        if len(out) >= limit:
            break
        cid = _cid(p)
        if unlearned and cid in learned:
            continue
        ts = turns(p)
        users = [t for r, _, t in ts if r == "user"]
        if len(users) < 2:
            continue
        updated = ts[-1][1]
        if since and updated[:10] < since:
            continue
        out.append({"id": cid, "source": source_of(p), "project": p.parent.name,
                    "started": ts[0][1],
                    "updated": updated, "turns": len(ts),
                    "first_user_line": redact(users[0].strip().splitlines()[0])
                    [:FIRST_LINE_CHARS], "saved": cid in saved, "learned": cid in learned})
    return sorted(out, key=lambda c: c["updated"], reverse=True)


def read_parts(chat_id: str, who: str = "all") -> list[str]:
    """The condensed `User:`/`Assistant:` text (only `User:` lines when who="user") in parts
    of at most PART_CHARS."""
    parts, cur = [], ""
    for role, _, text in turns(path_for(chat_id)):
        if who == "user" and role != "user":
            continue
        line = f"{role.capitalize()}: {' '.join(redact(text).split())}"
        if len(line) > LINE_CHARS:
            line = line[:LINE_CHARS - 1] + "…"
        if cur and len(cur) + 1 + len(line) > PART_CHARS:
            parts.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    return [*parts, cur]


def saved_ids(events, kind: str = "chat_saved") -> set[tuple[str, str]]:
    """(source, chat_id) of every `kind` log event (chat_saved, or chat_learned: read for what
    it says about the user) that is not voided."""
    out = set()
    for e in live(events):
        p = e.get("payload")
        if e.get("kind") == "log" and isinstance(p, dict) and p.get("kind") == kind:
            out.add((p.get("source"), p.get("chat_id")))
    return out
