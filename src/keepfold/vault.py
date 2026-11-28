"""Vault parsing and safe writes: lock, atomic replace, ID and path checks, secret scan.

This is the only module that touches vault files (SPEC §4, CLAUDE.md).
"""

from __future__ import annotations

import copy
import functools
import io
import itertools
import json
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePath

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from keepfold import atomic, vault_format
from keepfold.atomic import REPLACE_TRIES

log = logging.getLogger(__name__)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,80}$")
# The private browse folder, vault-relative: kept from Claude, git and the index.
PRIVATE_BROWSE_DIR = "browse/private"
# C5: imported AI chat pages (resources), vault-relative; git-ignored, read like resources/
CHAT_ARCHIVE = "archive/chats"

# SPEC §15a.2: fields marked * per kind. TOOL_PROPS are written only by set_tool_props.
SET_FIELD_WHITELIST = {
    "problem": {"status", "solved_on"},
    "assessment": {"date", "outcome", "score", "summary"},
    "application": {"status"},
    "track": {"current_day"},
    "resource": {"status", "summary", "url"},
    "note": {"summary"},
    "project": {"status", "summary"},
    "decision": {"status", "summary", "review_on"},
    "loop": {"status", "due", "summary"},
}
TOOL_PROPS = {"keepfold_status", "keepfold_next"}
ENUMS = {
    ("problem", "status"): {"todo", "attempted", "solved"},
    # taken: the test is done and the result is not out yet; only pending counts as coming up
    ("assessment", "outcome"): {"pending", "taken", "pass", "fail", "unknown"},
    ("application", "status"): {"applied", "oa", "interview", "offer", "rejected", "withdrawn"},
    # phase4-wiki.md §2, §2a
    ("resource", "resource_type"): {"book", "paper", "course", "video", "repo", "article", "other"},
    ("resource", "status"): {"queued", "reading", "done", "reference"},
    ("note", "note_type"): {"fleeting", "literature", "permanent", "structure"},
    ("project", "status"): {"idea", "active", "paused", "shipped", "archived"},
    ("decision", "status"): {"proposed", "accepted", "superseded", "retired"},
    ("loop", "status"): {"open", "done", "dropped"},
    ("loop", "loop_type"): {"commitment", "question", "idea"},
}

SECRET_RES = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"api[_-]?key\s*[:=]\s*\S{12,}", re.IGNORECASE),
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),  # Stripe
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),  # Slack
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),  # Google API key
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"),  # GitLab
    re.compile(r"\bhf_[A-Za-z]{30,}"),  # Hugging Face
    re.compile(r"\bnpm_[A-Za-z0-9]{36}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # JWT
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@"),  # credentials in a URL
    re.compile(r"\bpassword\s*[:=]\s*\S{6,}", re.IGNORECASE),
    # a 64-hex value soon after a private-key word (pasted wallet keys); bare tx hashes pass
    re.compile(r"priv(?:ate)?[_ -]?key[^\n]{0,40}?(?<![0-9a-f])(?:0x)?[0-9a-f]{64}(?![0-9a-f])",
               re.IGNORECASE),
]

# path -> ((ino, mtime_ns, size), fields, body): in-process only, so re-reads skip the YAML parse.
# shortcut: a same-size in-place rewrite within one mtime tick is missed (FAT: 2 s ticks).
_PARSED: dict[Path, tuple[tuple[int, ...], CommentedMap, str]] = {}

HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")
UNCHECKED_RE = re.compile(r"^\s*[-*] \[ \] ")
CHECKED_RE = re.compile(r"^\s*[-*] \[[xX]\] ")

LOCK_STALE_S = 120
# lock tokens this process holds now: a lock file with this pid and another token is one it
# failed to delete (Windows refuses while another process reads it), so never a live holder
_HELD: set[str] = set()
_SEQ = itertools.count()
# a token's "born" and the holder's start time read later may differ by this much: born is
# written to the second, and ps's etime (macOS) counts whole seconds
_BORN_TOL_S = 3
# Windows creation times never move; /proc's and ps's are now-relative and shift with a clock step
_FIXED_START = os.name == "nt"


def in_temp_folder(p: Path | str) -> bool:
    """Whether `p` lies inside the system temp folder (path parts, not a string prefix;
    case-insensitive on Windows). Windows can empty that folder at any time."""
    try:
        t, q = Path(tempfile.gettempdir()).resolve().parts, Path(p).resolve().parts
    except OSError:
        return False
    if os.name == "nt":
        t, q = tuple(x.casefold() for x in t), tuple(x.casefold() for x in q)
    return q[:len(t)] == t


def sqlite_ro_uri(p: PurePath) -> str:
    """A read-only SQLite URI for `p`. SQLite rejects a URI authority, so a UNC path (a share,
    or a mapped drive after resolve()) keeps an empty one: file:////server/share/..."""
    u = p.as_uri()
    return ("file:////" + u[len("file://"):] if not u.startswith("file:///") else u) + "?mode=ro"


class VaultError(Exception):
    """A vault failure with a tool error code from docs/tools.md."""

    def __init__(self, code: str, message: str, did_you_mean: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.did_you_mean = did_you_mean


def invalid(msg: str) -> VaultError:
    return VaultError("invalid_argument", msg)


def check_day(name: str, value) -> None:
    """B10: YYYY-MM-DD and a real day, or the exam clock silently skips the note."""
    try:
        ok = bool(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
                  and date.fromisoformat(value))
    except ValueError:
        ok = False
    if not ok:
        raise invalid(f"{name} must be a date like 2026-11-30.")


@dataclass
class Note:
    path: Path
    kind: str
    id: str
    fields: CommentedMap
    body: str


TRUTHY = {"true", "yes", "y", "on", "1"}


def truthy(v) -> bool:
    """A frontmatter flag set in any truthy form a person might type: true, yes, y, on, 1
    (any case, quoted or not)."""
    return v is True or str(v).strip().casefold() in TRUTHY


def is_private(note: Note) -> bool:
    # v5 PHASE4 V4: people pages are private unless their frontmatter says `private: false`
    p = note.fields.get("private")
    return truthy(p) or (note.kind == "person" and p is not False)


def secret_pattern(text: str) -> str | None:
    """The first SECRET_RES pattern `text` matches (the regex, never the match), or None."""
    return next((rx.pattern for rx in SECRET_RES if rx.search(text)), None)


def scan_secrets(text: str) -> None:
    if secret_pattern(text):
        raise VaultError("secret_detected", "The text looks like it holds a key or token; "
                         "remove it and try again.")


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 1_000_000  # never wrap
    return y


def _split(text: str) -> tuple[str, str, str, str] | None:
    """Split into (open fence, frontmatter, close fence, body), or None without frontmatter."""
    m = re.match(r"\ufeff?---[ \t]*(\r?\n)", text)  # an optional BOM stays in the open fence
    if not m:
        return None
    close = re.compile(r"^---[ \t]*(\r?\n|\Z)", re.M).search(text, m.end())
    if not close:
        raise ValueError("frontmatter fence is not closed")
    return text[: m.end()], text[m.end() : close.start()], close.group(0), text[close.end() :]


def _newline(text: str) -> str:
    return "\r\n" if text.split("\n", 1)[0].endswith("\r") else "\n"


_FLAT = re.compile(r'([A-Za-z_][\w-]*): ("(?:[^"\\]|\\.)*"|-?(?:0|[1-9][0-9]*)|true|false)')


def _flat_fm(fm: str) -> CommentedMap | None:
    """Flat `key: "json string" | int | true | false` lines (generated pages, sites._fm) read
    without ruamel, which took ~5 ms a page (R2-10: minutes for 20k site pages), to the values
    and types ruamel gives them; None for anything else."""
    out = CommentedMap()
    for line in fm.splitlines():
        m = _FLAT.fullmatch(line)
        if not m or m[1] in out:
            return None
        v = m[2]
        if v[0] == '"':
            try:
                out[m[1]] = DoubleQuotedScalarString(json.loads(v))
            except ValueError:  # an escape JSON lacks, such as \x41: ruamel reads it
                return None
        else:
            out[m[1]] = v == "true" if v in ("true", "false") else int(v)
    return out


def _load_fm(fm: str) -> CommentedMap:
    if (flat := _flat_fm(fm)) is not None:
        return flat
    data = _yaml().load(fm) if fm.strip() else CommentedMap()
    if data is None:
        return CommentedMap()
    if not isinstance(data, CommentedMap):
        raise ValueError("frontmatter is not a mapping")
    return data


OWN_PARSE_ERRORS = ("frontmatter fence is not closed", "frontmatter is not a mapping")


def parse_error(e: BaseException, skip: int = 1) -> str:
    """A parse error as its class and 1-based file line only, for logs and MCP errors: ruamel's
    text quotes the offending line, which may be private. `skip`: the lines before the YAML (a
    page's front matter starts after its `---` fence)."""
    mark = getattr(e, "problem_mark", None) or getattr(e, "context_mark", None)
    line = getattr(mark, "line", None)
    if str(e) in OWN_PARSE_ERRORS:  # _split/_load_fm's own words, which hold no note text
        return str(e)
    return type(e).__name__ + (f" at line {line + 1 + skip}" if line is not None else "")


def _dump_fm(data: CommentedMap, nl: str) -> str:
    buf = io.StringIO()
    _yaml().dump(data, buf)
    out = buf.getvalue()
    return out.replace("\n", nl) if nl != "\n" else out


def _to_yaml(value):
    """Plain Python values to the vault format: quoted strings, flow-style lists and maps."""
    if isinstance(value, str):
        return DoubleQuotedScalarString(value)
    if isinstance(value, (list, tuple)):
        seq = CommentedSeq(_to_yaml(v) for v in value)
        seq.fa.set_flow_style()
        return seq
    if isinstance(value, dict):
        m = CommentedMap((_to_yaml(k), _to_yaml(v)) for k, v in value.items())
        m.fa.set_flow_style()
        return m
    return value


def _section(lines: list[str], heading: str) -> tuple[int, int]:
    """Return [start, end) line indexes of the section under `heading` (text without #s)."""
    want = heading.lstrip("#").strip()
    for i, line in enumerate(lines):
        m = HEADING_RE.match(line.rstrip("\r\n"))
        if m and m.group(2) == want:
            level = len(m.group(1))
            for j in range(i + 1, len(lines)):
                n = HEADING_RE.match(lines[j].rstrip("\r\n"))
                if n and len(n.group(1)) <= level:
                    return i + 1, j
            return i + 1, len(lines)
    raise VaultError("invalid_argument", f"Heading '{want}' is not in the note; "
                     "use an existing heading.")


def _touch(path: Path) -> None:
    try:
        path.read_bytes()
    except OSError:
        pass


def warm(paths: list[Path]) -> None:
    """Read files once in parallel before a sequential parse: the first open of a newly
    written file waits for the antivirus scan (~9 ms each here, R2-10), and scans overlap."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(16) as ex:
        list(ex.map(_touch, paths))


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if os.name == "nt":
        # os.kill(pid, 0) would terminate the process on Windows; query it instead.
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5  # access denied: it exists
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _etime_s(out: str) -> int | None:
    """Seconds in ps's etime output, `[[dd-]hh:]mm:ss`, or None when it is not that."""
    m = re.fullmatch(r"\s*(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)\s*", out)
    if not m:
        return None
    d, h, mm, ss = (int(g or 0) for g in m.groups())
    return ((d * 24 + h) * 60 + mm) * 60 + ss


def _pid_started(pid: int) -> float | None:
    """When process `pid` started (epoch seconds), or None when that cannot be read."""
    try:
        if sys.platform == "darwin":  # no /proc: ps's elapsed time is locale-free
            from keepfold._proc import NO_WINDOW
            secs = _etime_s(subprocess.run(["ps", "-o", "etime=", "-p", str(pid)],
                                           capture_output=True, text=True, timeout=2,
                                           creationflags=NO_WINDOW).stdout)
            return None if secs is None else time.time() - secs
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return None
            t = [wintypes.FILETIME() for _ in range(4)]
            ok = k32.GetProcessTimes(wintypes.HANDLE(h), *map(ctypes.byref, t))
            k32.CloseHandle(wintypes.HANDLE(h))
            # creation time: 100 ns ticks since 1601
            return ((t[0].dwHighDateTime << 32 | t[0].dwLowDateTime) / 1e7 - 11644473600
                    if ok else None)
        # field 22 of /proc/<pid>/stat: clock ticks since boot; /proc/stat btime: the boot
        ticks = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
        boot = next(float(line.split()[1]) for line in
                    Path("/proc/stat").read_text().splitlines() if line.startswith("btime "))
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration, AttributeError,
            subprocess.SubprocessError):
        return None


@functools.cache
def _born(pid: int) -> float:
    """This process's start time for its lock tokens (keyed by pid, so a fork reads its own); 0
    when unknown."""
    return _pid_started(pid) or 0


class Vault:
    KIND_DIRS = {
        "concept": "concepts", "area": "areas", "domain": "domains", "mistake": "mistakes",
        "problem": "problems", "assessment": "assessments", "application": "applications",
        "track": "tracks", "session": "sessions", "me": "me", "resource": "resources",
        "note": "notes", "project": "projects", "decision": "decisions",
        "journal": "journal", "loop": "loops", "person": "people",
        "browse": "browse",  # browse-brain.md §3: generated day pages; browse/private/ is not read
        "rollup": "rollups",  # keepfold.rollups: generated week, month and year pages
    }
    # more folders a kind's pages may sit in, read like its own; a new page goes to the main
    # folder unless create_note names one of these (keepfold.chat_archive)
    EXTRA_DIRS = {"resource": (CHAT_ARCHIVE,)}

    @classmethod
    def dirs(cls, kind: str) -> tuple[str, ...]:
        """A kind's folders, vault-relative: its own, then EXTRA_DIRS."""
        return (cls.KIND_DIRS[kind], *cls.EXTRA_DIRS.get(kind, ()))

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        # the move from Cortex (keepfold.migrate), before anything reads or writes .keepfold/:
        # a set lookup after the first Vault of this root, two stats with nothing to move
        from keepfold import migrate
        if why := migrate.on_open(self.root):
            raise VaultError("vault_locked", why)
        self._tlock = threading.RLock()
        self._depth = 0
        self._jobs: set[tuple[str, int]] = set()  # (job lock, thread) held, see job_lock
        # path -> write number of its last write or removal, so a session close or a CLI
        # command commits only the files this process wrote (see wrote_since)
        self.written: dict[Path, int] = {}
        self.writes = 0

    def _track(self, path: Path) -> None:
        self.writes += 1
        self.written[Path(path)] = self.writes

    def wrote_since(self, mark: int) -> set[Path]:
        """Files written or removed through this Vault after `self.writes` was `mark`."""
        return {p for p, n in list(self.written.items()) if n > mark}

    # ---- paths ------------------------------------------------------------

    def path_for(self, kind: str, id: str) -> Path:
        if kind not in self.KIND_DIRS:
            raise VaultError("invalid_argument", f"Unknown kind '{kind}'; use one of "
                             f"{', '.join(self.KIND_DIRS)}.")
        if not isinstance(id, str) or not ID_RE.fullmatch(id):
            raise VaultError("invalid_id", "IDs must match ^[a-z0-9][a-z0-9.-]{0,80}$.")
        path = self._contained(self.root / self.KIND_DIRS[kind] / f"{id}.md")
        if not path.is_file():  # an extra folder's page; else the main folder's path
            return next((p for d in self.EXTRA_DIRS.get(kind, ())
                         if (p := self._contained(self.root / d / f"{id}.md")).is_file()), path)
        return path

    def _contained(self, path: Path) -> Path:
        p = Path(path)
        p = (p if p.is_absolute() else self.root / p).resolve()
        if not p.is_relative_to(self.root):
            raise VaultError("invalid_id", "The path must stay inside the vault.")
        return p

    # ---- reading ----------------------------------------------------------

    def _parse(self, path: Path, kind: str) -> Note:
        st = path.stat()  # before the read: a change after it misses the cache next time
        key = (st.st_ino, st.st_mtime_ns, st.st_size)  # an atomic replace gets a new file id
        hit = _PARSED.get(path)
        # key[1:]: dashboard._snapshot_parses seeds from the index snapshot, which has no inode
        if hit is None or hit[0] not in (key, key[1:]):
            text = path.read_bytes().decode("utf-8")
            parts = _split(text)
            fields = CommentedMap() if parts is None else _load_fm(parts[1])
            hit = _PARSED[path] = (key, fields, text if parts is None else parts[3])
        # shortcut: a shallow copy guards top-level edits only; nested lists are shared, and
        # no caller mutates them. Deep-copy here if one ever must.
        return Note(path, kind, path.stem, copy.copy(hit[1]), hit[2])

    def read(self, kind: str, id: str) -> Note:
        path = self.path_for(kind, id)
        if not path.is_file():
            raise VaultError("unknown_id", f"No {kind} note '{id}'; check the id with find.")
        try:
            return self._parse(path, kind)
        except Exception as e:  # YAML, fence or encoding errors
            raise VaultError("invalid_argument",
                             f"{path.name} cannot be parsed: {parse_error(e)}") from e

    def read_path(self, path: Path) -> Note:
        """Read a vault file outside the kind folders, such as BRIEF.md, Today.md or inbox.md."""
        p = self._contained(path)
        if not p.is_file():
            raise VaultError("unknown_id", f"{p.name} does not exist in the vault.")
        kinds = {d: k for k, d in self.KIND_DIRS.items()}
        extra = {d: k for k, ds in self.EXTRA_DIRS.items() for d in ds}
        try:
            return self._parse(p, "file" if p.parent == self.root else
                               extra.get(p.parent.relative_to(self.root).as_posix())
                               or kinds.get(p.parent.name, "file"))
        except Exception as e:
            raise VaultError("invalid_argument",
                             f"{p.name} cannot be parsed: {parse_error(e)}") from e

    def note_paths(self, kind: str) -> list[Path]:
        """The .md files of a kind folder (note_entries)."""
        return [Path(e.path) for e in self.note_entries(kind)]

    def note_entries(self, kind: str) -> Iterator[os.DirEntry]:
        """The directory entries of a kind folder's .md files: the one folder rule of
        note_paths and the index's scan. browse/ is read recursively (the site pages sit in
        browse/sites/<category>/) except its private/ subfolder, which is never entered."""
        deep = kind == "browse"
        hidden = os.path.normcase(self.root / PRIVATE_BROWSE_DIR)
        todo = [str(self.root / d) for d in reversed(
            (self.KIND_DIRS[kind],) if deep else self.dirs(kind))]
        while todo:
            try:
                with os.scandir(todo.pop()) as it:
                    entries = list(it)
            except OSError:  # no such folder (yet), or not a folder
                continue
            sub = []
            for e in entries:
                if os.path.normcase(e.name).endswith(".md"):  # as glob: any case on Windows
                    yield e
                if deep and e.is_dir(follow_symlinks=False) and os.path.normcase(
                        e.path) != hidden:
                    sub.append(e.path)
            todo += reversed(sub)

    def iter_notes(self, kind: str | None = None) -> Iterator[Note]:
        kinds = [kind] if kind else list(self.KIND_DIRS)
        sites = self.root / self.KIND_DIRS["browse"] / "sites"
        for k in kinds:
            if k not in self.KIND_DIRS:
                raise VaultError("invalid_argument", f"Unknown kind '{k}'.")
            for path in sorted(self.note_paths(k)):
                # browse site pages (browse/sites/<category>/, up to tens of thousands) are
                # for find and recall, which read note_paths; whole-vault walks skip them (R2-10)
                if path.parent.parent == sites:
                    continue
                if not ID_RE.fullmatch(path.stem):
                    log.warning("skipping %s: file name is not a valid id", path)
                    continue
                try:
                    yield self._parse(path, k)
                except Exception as e:
                    log.warning("skipping %s: %s", path, parse_error(e))

    # ---- lock ---------------------------------------------------------------

    @contextmanager
    def lock(self, timeout: float = 5.0):
        """Exclusive vault lock: .keepfold/lock (O_EXCL) holding "pid ts host". Reentrant per
        Vault."""
        if not self._tlock.acquire(timeout=timeout):
            raise VaultError("vault_locked", "The vault is busy; try again in a moment.")
        try:
            if self._depth == 0:
                self._acquire_file(timeout)
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0:
                    self._release_file()
        finally:
            self._tlock.release()

    @contextmanager
    def job_lock(self, rel: str, timeout: float, busy: str):
        """A lock file `rel` for a long job (a browse import), apart from the vault lock so
        short writes go on meanwhile: the same "pid ts host" file and stale rules (a crashed
        holder never leaves it stuck), reentrant per Vault and thread. VaultError("busy",
        `busy`) when another holder keeps it past `timeout`."""
        key = (rel, threading.get_ident())
        if key in self._jobs:
            yield
            return
        path = self.root / rel
        try:
            token = self._take(path, timeout)
        except VaultError as e:
            if e.code != "vault_locked":  # the format gate's refusal keeps its own code
                raise
            raise VaultError("busy", busy) from None
        self._jobs.add(key)
        try:
            yield
        finally:
            self._jobs.discard(key)
            self._drop(path, token)

    @property
    def _lock_path(self) -> Path:
        return self.root / ".keepfold" / "lock"

    def _acquire_file(self, timeout: float) -> None:
        self._token = self._take(self._lock_path, timeout)

    def _take(self, path: Path, timeout: float) -> str:
        vault_format.check_writable(self.root)  # D7 §2.1: every lock passes the format gate
        path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        while True:
            # 3 counter digits after the ms keep tokens unique: before Python 3.13 time.time() on
            # Windows ticks every 15.6 ms, so two takes in one tick shared a token, the loser's
            # discard unmarked the holder's, and its live lock looked like a leftover to steal
            # the 4th field, born, is this process's start time (audit #2). An older Keepfold
            # splits it into the host, sees another PC and applies the 120 s age rule: the same
            # as a hostname change, accepted
            token = (f"{os.getpid()} {time.time():.3f}{next(_SEQ) % 1000:03d} "
                     f"{socket.gethostname()} {_born(os.getpid()):.0f}")
            _HELD.add(token)  # before the file shows it, so no reader takes it for a leftover
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except (FileExistsError, PermissionError):
                _HELD.discard(token)
                if self._steal_if_stale(path):
                    continue
                if time.monotonic() >= deadline:
                    raise VaultError("vault_locked", "Another Keepfold process holds the vault "
                                     "lock; try again in a moment.") from None
                time.sleep(0.05)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(token)
            return token

    def _steal_if_stale(self, path: Path) -> bool:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            age = time.time() - path.stat().st_mtime
        except OSError:
            return False  # vanished or being written; retry the create
        if content in _HELD:
            return False  # this process holds it now, even if gethostname() changed since
        # a holder this host can see is stale when its pid is dead, or when the process with
        # that pid started after the lock was written (G4: a pid reused after a reboot); its
        # start time unknown, a lock under 10 x LOCK_STALE_S stays (R2-3: a long sites.write).
        # No readable pid, or another host's: the file's age decides
        try:
            pid_s, ts_s, *rest = content.split()  # gethostname() never holds a space
            pid, ts = int(pid_s), float(ts_s)
            born = float(rest[1]) if len(rest) > 1 else 0  # 0: an older token or unknown
            age = time.time() - ts
            local = not rest or rest[0] == socket.gethostname()
        except ValueError:
            local = False
        if local and pid == os.getpid():
            pass  # this process's own leftover: not in _HELD (checked above), so stale
        elif local and _pid_alive(pid):
            started = _pid_started(pid)
            known = bool(born) and started is not None
            if known and abs(started - born) <= _BORN_TOL_S:
                return False  # the holder itself, whatever the clock did since the take
            if known and _FIXED_START:
                pass  # another start time on a fixed clock: a reused pid, so stale
            elif (started <= ts + 5 if started is not None  # 5 s of clock slack
                    else age <= 10 * LOCK_STALE_S):
                return False
        elif not local and age <= LOCK_STALE_S:
            return False
        # shortcut: a second stealer could race between this read and unlink; the window is
        # tiny and only opens after a crash. Upgrade to rename-then-verify if it ever bites.
        try:
            if path.read_text(encoding="utf-8", errors="replace") == content:
                path.unlink()
                log.warning("stole a stale vault lock (%s)", content)
        except OSError:
            pass
        return True

    def _release_file(self) -> None:
        self._drop(self._lock_path, self._token)

    @staticmethod
    def _drop(path: Path, token: str) -> None:
        _HELD.discard(token)  # a file left behind is now a leftover this process may steal
        for attempt in range(REPLACE_TRIES):
            try:
                if path.read_text(encoding="utf-8", errors="replace") == token:
                    path.unlink()
                return
            except FileNotFoundError:
                return
            except OSError:  # Windows: another process is reading the file; it lets go soon
                time.sleep(0.01 * (attempt + 1))

    # ---- writing --------------------------------------------------------------

    def _atomic_write(self, path: Path, text: str | bytes,
                      expect: os.stat_result | None = None) -> bool:
        """Temp file + os.replace with retry. Returns False if the file changed since `expect`."""
        def unchanged() -> bool:
            st = path.stat()
            return (st.st_mtime_ns, st.st_size) == (expect.st_mtime_ns, expect.st_size)

        try:
            if not atomic.replace(path, text, guard=None if expect is None else unchanged):
                return False
        except PermissionError:
            raise VaultError("vault_locked", f"{path.name} is open in another "
                             "program; try again.") from None
        self._track(path)
        return True

    def _mutate(self, path: Path, change: Callable[[str], str]) -> bool:
        """Lock, read, apply `change`, write; reread and reapply if the file changed meanwhile."""
        with self.lock():
            for _ in range(5):
                try:
                    st = path.stat()
                    text = path.read_bytes().decode("utf-8")
                except FileNotFoundError:
                    raise VaultError("unknown_id", f"{path.name} does not exist; check the "
                                     "id with find.") from None
                new = change(text)
                if new == text:
                    return False
                if self._atomic_write(path, new, expect=st):
                    return True
                log.info("%s changed during the edit; rereading", path)
            raise VaultError("vault_locked", f"{path.name} keeps changing; try again.")

    def _edit_fields(self, kind: str, id: str, values: dict) -> None:
        def change(text: str) -> str:
            parts = _split(text)
            if parts is None:
                raise VaultError("invalid_argument", f"{id} has no frontmatter.")
            head, fm, close, body = parts
            data = _load_fm(fm)
            for k, v in values.items():
                if v is None:
                    data.pop(k, None)
                else:
                    data[k] = _to_yaml(v)
            return head + _dump_fm(data, _newline(text)) + close + body

        self._mutate(self.path_for(kind, id), change)

    def set_field(self, kind: str, id: str, field: str, value) -> None:
        if field in TOOL_PROPS or field == "merged_into":
            raise VaultError("invalid_argument", f"'{field}' is computed by Keepfold and cannot "
                             "be set by hand.")
        if field not in SET_FIELD_WHITELIST.get(kind, set()):
            allowed = sorted(SET_FIELD_WHITELIST.get(kind, set()))
            raise VaultError("invalid_argument", f"'{field}' cannot be set on a {kind}; "
                             f"settable fields: {', '.join(allowed)}.")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise VaultError("invalid_argument", "The value must be a string or a number.")
        allowed_values = ENUMS.get((kind, field))
        if allowed_values and value not in allowed_values:
            raise VaultError("invalid_argument", f"{field} must be one of "
                             f"{', '.join(sorted(allowed_values))}.")
        if (kind, field) == ("assessment", "date"):
            check_day(field, value)
        scan_secrets(str(value))
        self._edit_fields(kind, id, {field: value})

    def set_tool_props(self, kind: str, id: str, status: str, next_date: str | None) -> None:
        """Write keepfold_status and keepfold_next; a None next_date removes keepfold_next."""
        self._edit_fields(kind, id, {"keepfold_status": status, "keepfold_next": next_date})

    def append_to_section(self, kind: str, id: str, heading: str, text: str) -> None:
        scan_secrets(text)

        def change(full: str) -> str:
            parts = _split(full)
            prefix = "".join(parts[:3]) if parts else ""
            body = parts[3] if parts else full
            nl = _newline(full)
            lines = body.splitlines(keepends=True)
            start, end = _section(lines, heading)
            at = start
            for j in range(end - 1, start - 1, -1):
                if lines[j].strip():
                    at = j + 1
                    break
            if at > 0 and not lines[at - 1].endswith("\n"):
                lines[at - 1] += nl
            new = text.replace("\r\n", "\n").rstrip("\n").replace("\n", nl) + nl
            if at < len(lines) and HEADING_RE.match(lines[at].rstrip("\r\n")):
                new += nl  # keep a blank line before the next heading
            lines.insert(at, new)
            return prefix + "".join(lines)

        self._mutate(self.path_for(kind, id), change)

    def check_item(self, kind: str, id: str, section: str, item_text: str) -> bool:
        """Tick the one unchecked item in `section` containing `item_text`.

        Returns False if it was already ticked; raises invalid_argument if none or several match.
        """
        changed = False

        def change(full: str) -> str:
            nonlocal changed
            parts = _split(full)
            prefix = "".join(parts[:3]) if parts else ""
            lines = (parts[3] if parts else full).splitlines(keepends=True)
            start, end = _section(lines, section)
            hits = [i for i in range(start, end)
                    if UNCHECKED_RE.match(lines[i]) and item_text in lines[i]]
            if len(hits) > 1:
                raise VaultError("invalid_argument", f"{len(hits)} items in '{section}' match; "
                                 "give more of the item text.")
            if not hits:
                if any(CHECKED_RE.match(lines[i]) and item_text in lines[i]
                       for i in range(start, end)):
                    changed = False
                    return full
                raise VaultError("invalid_argument", f"No unchecked item in '{section}' "
                                 "matches; check the item text.")
            lines[hits[0]] = lines[hits[0]].replace("[ ]", "[x]", 1)
            changed = True
            return prefix + "".join(lines)

        self._mutate(self.path_for(kind, id), change)
        return changed

    def create_note(self, kind: str, id: str, fields: dict, body: str,
                    folder: str | None = None) -> Path:
        """A new page in its kind's folder, or in `folder`, one of Vault.dirs(kind)."""
        path = self.path_for(kind, id)
        if folder is not None:
            if folder not in self.dirs(kind):
                raise VaultError("invalid_argument", f"A {kind} page cannot go in {folder}/.")
            if not path.exists():
                path = self._contained(self.root / folder / f"{id}.md")
        if fields.get("id", id) != id:
            raise VaultError("invalid_argument", "fields.id must match the note id.")
        data = CommentedMap((k, _to_yaml(v)) for k, v in {"id": id, **fields}.items())
        if body and not body.endswith("\n"):
            body += "\n"
        text = "---\n" + _dump_fm(data, "\n") + "---\n" + body
        scan_secrets(text)
        with self.lock():
            if path.exists():
                raise VaultError("invalid_argument", f"{kind} '{id}' already exists; patch it "
                                 "instead.")
            # ids are unique across kinds: the index keys nodes by id alone
            # and across the stamp's declared folders, a newer Keepfold's kinds too (D7 §1.6)
            other = next((k for k in self.KIND_DIRS for d in self.dirs(k)
                          if (self.root / d / f"{id}.md").is_file()), None) or next(
                ("page" for d in vault_format.declared_dirs(self.root)
                 if (self.root / d / f"{id}.md").is_file()), None)
            if other:
                title = fields.get("title") or id
                raise VaultError("invalid_id", f"The id '{id}' is already the {other} "
                                 f"'{id}', and ids are unique across kinds (force does not "
                                 f"override this); use that note or choose another title, "
                                 f"such as '{title} {kind}' (id {id}-{kind}).", [id])
            path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(path, text)
        return path

    def append_journal(self, day: str, hhmm: str, text: str) -> None:
        """Add `- HH:MM text` under ## Log of the journal page for `day`, creating it if missing."""
        text = " ".join(text.split())
        scan_secrets(text)
        from keepfold.relink import journal_up  # lazy: relink imports this module

        with self.lock():
            if not self.path_for("journal", day).is_file():
                up = journal_up(self, day)  # Obsidian's graph: day -> month -> topic map
                self.create_note("journal", day, {"title": f"Journal {day}", "kind": "journal",
                                                  "date": day},
                                 (f"Up: [[{up}]]\n\n" if up else "") + "## Log\n")
            self.append_to_section("journal", day, "Log", f"- {hhmm} {text}")

    def append_log(self, day: str, type: str, title: str) -> None:
        """Add `## [day] type | title` to log.md; earlier lines are never touched."""
        line = f"## [{day}] {type} | {' '.join(str(title).split())}\n"
        path = self.root / "log.md"
        with self.lock():
            old = path.read_bytes().decode("utf-8") if path.is_file() else "# Log\n\n"
            self.write_text(path, old + line)

    def remove(self, path: Path) -> bool:
        """Delete one vault file under the lock, retrying a Windows sharing error. False if it
        was not there."""
        p = self._contained(path)
        with self.lock():
            _PARSED.pop(p, None)
            for attempt in range(REPLACE_TRIES):
                try:
                    p.unlink()
                    self._track(p)
                    return True
                except FileNotFoundError:
                    return False
                except PermissionError:
                    if attempt == REPLACE_TRIES - 1:
                        raise VaultError("vault_locked", f"{path.name} is open in another "
                                         "program; try again.") from None
                    time.sleep(0.05 * 2**attempt)
        return False  # unreachable

    def move(self, src: Path, dst: Path) -> None:
        """Move one vault file under the lock: `dst` written atomically with `src`'s bytes and
        mtime, then `src` removed. A crash between leaves the same bytes in both, and a rerun
        finishes; VaultError when `dst` already holds something else."""
        src, dst = self._contained(src), self._contained(dst)
        with self.lock():
            st, data = src.stat(), src.read_bytes()
            if dst.exists() and dst.read_bytes() != data:
                raise VaultError("invalid_argument", f"{dst.name} is already in "
                                 f"{dst.parent.relative_to(self.root).as_posix()}/.")
            dst.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(dst, data)
            os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))  # pages keep their dates
            self.remove(src)

    def update_text(self, path: Path, change: Callable[[str], str], default: str = "") -> bool:
        """Read-modify-write a page the owner may edit at the same time (B13): `change` (which
        may run more than once) gets the text, or `default` if the file is missing, and an
        outside edit made meanwhile is reread instead of overwritten. False: nothing changed."""
        p = self._contained(path)

        def scanned(text: str) -> str:
            new = change(text)
            if new != text:
                scan_secrets(new)
            return new

        with self.lock():
            if p.is_file():
                return self._mutate(p, scanned)
            new = scanned(default)
            if new == default:
                return False
            p.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(p, new)
            return True

    def write_text(self, path: Path, text: str) -> Path:
        """Atomic write for generated files (Today.md, session notes, dashboard). Files under
        `.keepfold/` are git-ignored local caches and views, never committed, so they skip the
        secret scan (scanning an 18.8 MB dashboard took ~10 s at 5k pages)."""
        p = self._contained(path)
        if not p.is_relative_to(self.root / ".keepfold"):
            scan_secrets(text)
        with self.lock():
            p.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(p, text)
        return p
