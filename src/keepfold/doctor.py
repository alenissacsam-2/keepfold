"""Environment checks for `keepfold doctor` (SPEC §7): each returns (status, name, detail, fix)."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

from keepfold import claims, share
from keepfold.app import targets
from keepfold.clients import claude_desktop_config_paths
from keepfold.contract import MAX_CHARS as CONTRACT_MAX_CHARS
from keepfold.events import EventLog
from keepfold.session import _git, git_choice, git_exe
from keepfold.vault import CHAT_ARCHIVE, PRIVATE_BROWSE_DIR, Vault


def connects() -> list[str]:
    """The "Where Keepfold connects" section: a heading, then one line per destination off this
    PC (keepfold.egress)."""
    from keepfold import egress
    return ["Where Keepfold connects:", *(f"  {s}" for s in egress.summary())]


def check_python() -> tuple[str, str, str, str]:
    name = "Python and FTS5"
    ver = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info < (3, 14):  # noqa: UP036 (the check is the point)
        return "FAIL", name, f"Python {ver}", "Run keepfold through `uv run` (Python 3.14+)."
    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.Error as e:
        return "FAIL", name, f"sqlite3 has no FTS5: {e}", "Use the uv-managed Python 3.14."
    return "PASS", name, f"Python {ver}, sqlite {sqlite3.sqlite_version} with FTS5", ""


SYNC_MARKS = {"onedrive": "OneDrive", "dropbox": "Dropbox", "google drive": "Google Drive",
              "googledrive": "Google Drive", "my drive": "Google Drive", "icloud": "iCloud",
              "clouddocs": "iCloud"}  # casefolded path text -> the sync service


def check_onedrive(root: Path) -> tuple[str, str, str, str]:
    """WARN when the vault sits in a cloud-synced folder (OneDrive, Dropbox, Google Drive,
    iCloud): sync breaks the lock and silently copies private pages off the machine."""
    name = "Vault outside cloud sync"
    root = Path(root).resolve()
    text = str(root).casefold()
    synced = next((svc for mark, svc in SYNC_MARKS.items() if mark in text), None)
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        base = os.environ.get(var)
        if base and root.is_relative_to(Path(base).resolve()):
            synced = "OneDrive"
    if not synced and sys.platform == "darwin" and _in_icloud_documents(root):
        synced = "iCloud (Desktop & Documents)"
    if synced:
        return ("WARN", name, f"{root} is inside {synced}",
                f"Move the vault out of {synced}; sync can break the lock and atomic writes, "
                "and silently copies private pages and voice samples to the cloud.")
    return "PASS", name, str(root), ""


def _in_icloud_documents(root: Path) -> bool:
    """macOS syncs ~/Documents and ~/Desktop to iCloud when Desktop & Documents is on (iCloud
    Drive then has a Documents folder), though the vault's path never says iCloud."""
    home = Path.home()
    return ((home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Documents").is_dir()
            and any(root.is_relative_to((home / d).resolve()) for d in ("Documents", "Desktop")))


def _old_repo(root: Path) -> bool:
    """git is off but the vault still has a repo git can read: its remotes and history still
    matter for privacy, so those read-only checks still run."""
    return (root / ".git").exists() and git_exe() is not None


def _remote_warn(name: str, remotes: list[str], off: bool) -> tuple[str, str, str, str]:
    return ("WARN", name, ("git is off in your settings, but the vault's repo still has "
                           if off else "") + f"remotes: {', '.join(remotes)}",
            "Remove them with `git remote remove <name>`; a push would publish private "
            "people pages and voice samples. The vault stays local.")


def check_git(root: Path) -> tuple[str, str, str, str]:
    name = "Vault git: no remote, push blocked"
    root = Path(root).resolve()
    if not git_choice()[0]:
        if _old_repo(root) and (remotes := _git(root, "remote").stdout.split()):
            return _remote_warn(name, remotes, off=True)
        return ("PASS", name, "git is off in your settings (your choice): Keepfold keeps no "
                "history of the vault and `keepfold undo` is off", "")
    if not (root / ".git").is_dir() and git_exe() is None:
        return ("WARN", name, "git is not installed, so the vault has no history and undo does "
                "nothing", "Install git: in the Keepfold app, Status → Git → Install Git "
                "(Windows; or run `winget install --id Git.Git -e --scope user`); else get it "
                "from git-scm.com. The app then starts the vault's history by itself; from a "
                "terminal, run `keepfold init <vault>` after the install.")
    if not (root / ".git").is_dir():
        return "FAIL", name, "the vault has no git repo", "Run `keepfold init <vault>`."
    if not (root / ".git" / "hooks" / "pre-push").is_file():
        return "FAIL", name, "no pre-push hook", "Run `keepfold init <vault>` to add it."
    remotes = _git(root, "remote").stdout.split()
    if remotes:
        return _remote_warn(name, remotes, off=False)
    return "PASS", name, "no remote; pre-push hook present", ""


def check_private_browse_git(root: Path) -> tuple[str, str, str, str]:
    """v7 C3: private browse pages must not be in the vault's git. Keepfold untracks them on the
    next browse import, but never rewrites history; this says how."""
    from keepfold.browse import forget

    name = "Private browsing out of git"
    root = Path(root)
    git_on, kept = git_choice()
    if not git_on:
        if _old_repo(root) and (n := forget.private_in_history(root)):
            return _history_warn(name, n)
        return "PASS", name, "git is off in your settings (see Vault git)", ""
    if kept:
        return "PASS", name, "private browsing pages are kept in git by your choice", ""
    if not (root / ".git").exists():
        return "PASS", name, "no repo (see Vault git)", ""
    if _git(root, "ls-files", "--", PRIVATE_BROWSE_DIR).stdout.strip():
        return ("WARN", name, "browse/private/ is still tracked by git",
                "Run `keepfold browse import` once (it untracks the folder), or run "
                "`git rm -r --cached browse/private` in the vault and commit.")
    if _git(root, "check-ignore", "-q", PRIVATE_BROWSE_DIR + "/x.md").returncode:
        if not (root / PRIVATE_BROWSE_DIR).exists():  # an older vault, nothing private yet
            return ("PASS", name, "no private pages yet; the .gitignore line is added before "
                    "the first one", "")
        return ("WARN", name, "browse/private/ is not in the vault's .gitignore",
                f"Run `keepfold init {root}` or `keepfold browse import`; either adds the line.")
    n = forget.private_in_history(root)
    if n:
        return _history_warn(name, n)
    return "PASS", name, "browse/private/ is ignored and in no commit", ""


def check_chat_archive_git(root: Path) -> tuple[str, str, str, str]:
    """v9 C5: imported chat pages sit in archive/chats/, which the vault's .gitignore keeps out
    of git."""
    name = "Chat archive out of git"
    root = Path(root)
    if not (root / ".git").exists():
        return "PASS", name, "no repo (see Vault git)", ""
    if _git(root, "check-ignore", "-q", CHAT_ARCHIVE + "/x.md").returncode:
        return ("WARN", name, f"{CHAT_ARCHIVE}/ is not in the vault's .gitignore",
                f"Run `keepfold init {root}` or `keepfold chats archive`; either adds the line.")
    if _git(root, "ls-files", "--", CHAT_ARCHIVE).stdout.strip():
        return ("WARN", name, f"{CHAT_ARCHIVE}/ is still tracked by git",
                f"Run `git rm -r --cached {CHAT_ARCHIVE}` in the vault and commit.")
    return "PASS", name, f"{CHAT_ARCHIVE}/ is ignored", ""


def check_browse_legacy(root: Path) -> tuple[str, str, str, str]:
    """F3: the browse files from before the store (`browse.legacy`), not read in yet."""
    from keepfold.browse import legacy
    from keepfold.vault import Vault
    name = "Old browse files"
    left = legacy.pending(Vault(root))
    if not left:
        return "PASS", name, "none left to read in", ""
    return ("WARN", name, f"{len(left)} not read into the browsing store yet: "
            + ", ".join(Path(r).name for r in left),
            "Run `keepfold browse import` (or open the app): it reads them in and renames each "
            "to <name>.migrated.")


def _history_warn(name: str, n: int) -> tuple[str, str, str, str]:
    return ("WARN", name, f"{n} commit{'s' * (n != 1)} in the vault's git history hold "
            "private browse pages",
            "They are no longer committed, but old commits keep them. To remove them, "
            "back up the vault, then in it run `git filter-repo --path browse/private "
            "--invert-paths` (or `git filter-branch --index-filter \"git rm -r -q --cached "
            "--ignore-unmatch browse/private\" -- --all`), then `git reflog expire "
            "--expire=now --all` and `git gc --prune=now`. Keepfold never rewrites history "
            "itself.")


def check_git_identity(root: Path) -> tuple[str, str, str, str]:
    """N11: with no git identity every session commit fails silently, so `keepfold undo` has
    nothing to revert."""
    name = "Vault git identity"
    if not git_choice()[0]:
        return "PASS", name, "git is off in your settings (see Vault git)", ""
    if not (Path(root) / ".git").is_dir():
        return "PASS", name, "no repo (see Vault git)", ""
    email = _git(Path(root), "config", "user.email").stdout.strip()
    if not email:
        return ("WARN", name, "git has no user.email here, so session commits fail and "
                "`keepfold undo` does nothing",
                f"Run `keepfold init {root}`; it sets Keepfold <keepfold@localhost> for this "
                "vault only.")
    return "PASS", name, email, ""


def _ago(seconds: float) -> str:
    for unit, n in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= n:
            k = int(seconds // n)
            return f"{k} {unit}{'s' * (k != 1)} ago"
    return "just now"


def check_git_state(root: Path) -> list[tuple[str, str, str, str]]:
    """The age of the vault's newest commit, and whether a git lock is left in its repo."""
    import time

    last, lock = "Last vault commit", "index.lock present"
    root = Path(root)
    if not git_choice()[0]:
        return [("PASS", n, "git is off in your settings (see Vault git)", "")
                for n in (last, lock)]
    if not (root / ".git").is_dir():
        return [("PASS", n, "no repo (see Vault git)", "") for n in (last, lock)]
    t = _git(root, "log", "-1", "--format=%ct").stdout.strip()
    rows = [("PASS", last, _ago(time.time() - int(t)) if t.isdigit() else "none yet", "")]
    try:
        age = time.time() - (root / ".git" / "index.lock").stat().st_mtime
    except OSError:
        return [*rows, ("PASS", lock, "no", "")]
    return [*rows, ("WARN", lock, f"yes, {_ago(age)}",
                    "A git command may still be running. If none is, Keepfold removes a lock "
                    "older than 10 minutes by itself, or delete .git/index.lock in the vault.")]


MODEL_READY = ".complete"  # `keepfold model download` writes it into models_dir() when it is done


def search_model_ready() -> bool:
    """The search model is whole: the download finished. Older downloads have no marker; they
    count when no file is left half-written (Hugging Face's `*.incomplete`)."""
    from keepfold import semantic

    d = semantic.models_dir()
    if (d / MODEL_READY).is_file():
        return True
    return d.is_dir() and any(d.iterdir()) and not any(d.rglob("*.incomplete"))


def installer() -> bool:
    """Keepfold runs from a KeepfoldSetup.exe install (no terminal commands for its user)."""
    from keepfold import update  # update imports cli, which imports this module

    return update.mode() == "installer"


def app_place() -> str | None:
    """Where this user opens the app from, worded as server.reopen() words it: the Start menu
    for an installer install, Applications or the app menu for the Mac or Linux app; None for
    a terminal install."""
    from keepfold import update

    m = update.mode()
    if m == "installer":
        return "the Start menu"
    if m == "app":
        return "Applications or Launchpad" if sys.platform == "darwin" else "the app menu"
    return None


def app_hint() -> str:
    """How this user opens the app: from where app_place() says, else the command."""
    return f"open Keepfold from {p}" if (p := app_place()) else "run `keepfold app`"


def check_search_model() -> tuple[str, str, str, str]:
    from keepfold import semantic

    d = semantic.models_dir()
    if search_model_ready():
        return "PASS", "Search model", str(d), ""
    return ("WARN", "Search model", "not downloaded: recall uses keywords only and a history "
            "import files every page under Other",
            "In the Keepfold app (Start menu), Setup → Download search model." if installer() else
            f"Open Keepfold from {p}, then Setup → Download search model."
            if (p := app_place()) else
            "Run `keepfold model download` (about 150 MB, once) or use `keepfold app` → Setup.")


def check_on_path() -> tuple[str, str, str, str]:
    """The Claude Code plugin's server and hooks run `keepfold` by name; hooks fail silently."""
    if found := shutil.which("keepfold"):
        return "PASS", "`keepfold` on PATH", found, ""
    return ("WARN", "`keepfold` on PATH", "not found: the Claude Code plugin's server and hooks "
            "cannot start it",
            "Sign out of Windows and in again (setup added Keepfold to your PATH), or run "
            "KeepfoldSetup.exe again." if installer() else
            "Run `uv tool update-shell`, then open a new terminal.")


def check_ai_apps() -> tuple[str, str, str, str]:
    """N9: WARN when no AI app the app's Connect section lists has a `keepfold` entry."""
    name = "AI app connected"
    on = [t.label for t in targets.targets() if t.connected()]
    if on:
        return "PASS", name, ", ".join(on), ""
    return ("WARN", name, "no AI app runs Keepfold yet, so nothing reads or records anything",
            f"Open Keepfold from {p} and tick an app under Connect AI apps."
            if (p := app_place()) else
            "Run `keepfold app` and tick an app under Connect, or `keepfold setup`.")


def check_old_connections() -> tuple[str, str, str, str]:
    """R4: WARN while an AI app still holds an entry under the old name Cortex (an MCP entry,
    a hook, a rules block, the cortex@cortex plugin): targets.migrate_old_entries, dry run."""
    name = "Old Cortex connections"
    try:
        rows = [r for r in targets.migrate_old_entries(dry_run=True) if r["action"] != "nothing"]
    except Exception as e:  # noqa: BLE001  (a check never stops doctor)
        return "WARN", name, f"could not check: {e}", ""
    if not rows:
        return "PASS", name, "none left", ""
    return ("WARN", name, "; ".join(f"{r['app']}: {r['file']}" for r in rows),
            " ".join(dict.fromkeys(
                (f"{r['app']}: run `keepfold migrate` to move it ({r['note']})."
                 if r["action"] == "moved" else f"{r['app']}: {r['note']}") for r in rows)))


def check_local_model() -> tuple[str, str, str, str]:
    from keepfold import localllm
    from keepfold.taint import config

    name = "Local model"
    conf = config()
    if not localllm.enabled(conf):
        return "PASS", name, "off", ""
    got = localllm.status(conf)  # read-only, like `keepfold llm status`
    if got["error"]:  # a non-loopback url or a cloud model
        return "WARN", name, got["error"], "Fix `localllm` in ~/.keepfold/config.yaml."
    if (runner := got["runner"]) is None:
        return ("WARN", name, "on, but not running: no local runner answers with the "
                "configured model", "Run `keepfold llm start` (or `keepfold app` → Setup → Start).")
    return "PASS", name, runner.describe(), ""


def check_kill(root: Path) -> tuple[str, str, str, str]:
    """v5 PHASE4 V5: show the kill switch state (a state, not a fault)."""
    on = share.killed(Vault(root))
    return "PASS", "Kill switch", ("on: act-as-me and the reader role "
                                   "are refused (`keepfold kill --off`)" if on else "off"), ""


def check_exclude() -> tuple[str, str, str, str]:
    """Folders where Claude Code gets no priming and no Keepfold tools (a state); FAIL while
    ~/.keepfold/config.yaml cannot be read, since every privacy choice then fails closed; WARN on
    a top-level key nothing in Keepfold reads (a misspelt `exclude:` excludes nothing)."""
    from keepfold.taint import CONFIG_FIX, config_error, exclude_list, unknown_keys

    if err := config_error():
        return "FAIL", "Settings file", err, CONFIG_FIX
    folders = "; ".join(exclude_list()) or "none"
    if odd := unknown_keys():
        return ("WARN", "Excluded folders", f"{folders}; the settings file has a key Keepfold "
                f"does not read: {', '.join(odd)}",
                "Check the spelling in ~/.keepfold/config.yaml (a misspelt `exclude:` excludes "
                "nothing), or delete the line.")
    return "PASS", "Excluded folders", folders, ""


def check_me_pages(root: Path) -> tuple[str, str, str, str]:
    """Pages added by later versions (e.g. me/voice.md) on an older vault."""
    from keepfold.cli import ME_PAGES  # cli imports doctor at module level

    missing = [rel for rel in ME_PAGES if not (Path(root) / rel).exists()]
    if missing:
        return ("WARN", "me/ pages", "missing: " + ", ".join(missing),
                f"Run `keepfold init {root}`; it adds missing pages and changes nothing else.")
    return "PASS", "me/ pages", f"{len(ME_PAGES)} present", ""


SYNC_COPY_FIX = "Open both, keep what you want in the original, delete the copy."


def check_sync_copies(root: Path) -> tuple[str, str, str, str]:
    """WARN on a sync tool's conflict copies (sync_copies.find_visible): a copied page is not
    read (its edit lives only there); the event log reads each event once, copies included.
    Up to 5 are named; a private page's copies are only counted, as an agent may run doctor."""
    from keepfold import sync_copies

    name = "Sync conflict copies"
    shown, private = sync_copies.find_visible(Vault(root))
    if not shown and not private:
        return "PASS", name, "none", ""
    parts = [", ".join(c for c, _ in shown[:5])] if shown else []
    if len(shown) > 5:
        parts.append(f"{len(shown) - 5} more")
    if private:
        parts.append(f"{private} cop{'y' if private == 1 else 'ies'} of private pages")
    return ("WARN", name, f"{len(shown) + private} found: {' and '.join(parts)}",
            f"{SYNC_COPY_FIX} Keepfold does not read a copied page, so an edit made on the "
            "other device is only in the copy.")


def check_damaged_stores(root: Path) -> list[tuple[str, str, str, str]]:
    """One WARN naming the damaged stores Keepfold moved aside (keepfold.quarantine) and started
    again; no row when there are none."""
    from keepfold import quarantine

    names = [p.relative_to(root).as_posix() for p in quarantine.found(root)
             if quarantine.STORE.fullmatch(p.name)]
    if not names:
        return []
    return [("WARN", "Damaged databases moved aside", ", ".join(names),
             "Keepfold found these damaged (often a sync tool copying a database apart from its "
             "-wal file), set them aside and started fresh ones: search vectors are built "
             "again, chats are read again. Delete the copies (with their -wal and -shm files) "
             "once recall and chat import work again.")]


def check_id_clashes(root: Path) -> tuple[str, str, str, str]:
    name = "Note ids unique across kinds"
    seen: dict[str, list[str]] = {}
    for kind in Vault.KIND_DIRS:
        for path in sorted(p for d in Vault.dirs(kind) for p in (Path(root) / d).glob("*.md")):
            seen.setdefault(path.stem, []).append(kind)
    clashes = [f"{id} ({', '.join(kinds)})" for id, kinds in seen.items() if len(kinds) > 1]
    if clashes:
        return ("WARN", name, f"{len(clashes)} clash(es): {'; '.join(clashes)}",
                "Rename one note in each pair; the index keeps only one note per id.")
    return "PASS", name, f"{len(seen)} ids", ""


# RR-38: the import header (`Source: <label> · Date:`) or a chat page's Timeline line
_IMPORT_HEAD = re.compile(r"^(?:Timeline: [^\n]*?)?Source: ([^·\n]+?) · Date: [^\n]*$", re.M)
OTHER_SOURCE = "other"  # a label no Keepfold importer writes: never printed, it may be page text


def _quartiles(xs: list[int]) -> list[int]:
    s = sorted(xs)
    return [s[round(q * (len(s) - 1))] for q in (0.25, 0.5, 0.75)]


def check_import_shapes(root: Path) -> list[tuple[str, str, str, str]]:
    """RR-38: per import source, its pages' length quartiles (characters below the header
    line) and the share of pages that are mostly links (sources.link_lines over 60% of the
    lines, the quality gate's rule) or numbers only (under 10% letters and at least 30% digits
    of their non-space characters). Numbers only: no page text, and a label Keepfold's importers
    do not write shows as OTHER_SOURCE."""
    from keepfold import chat_import, importers, sources
    known = {*chat_import.LABELS.values(), *importers.LABELS.values(), "Calendar", "Documents",
             "Git", "Highlights", "Pocket"}
    shapes: dict[str, list[tuple[int, bool, bool]]] = {}
    for p in Vault(root).note_paths("resource"):
        try:
            text = p.read_text("utf-8", errors="replace")
        except OSError:
            continue
        if not (m := _IMPORT_HEAD.search(text, text.find("\n---", 3) + 1)):
            continue
        body = text[m.end():]
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        chars = [c for c in body if not c.isspace()]
        links = bool(lines) and sources.link_lines(lines) > 0.6 * len(lines)
        numbers = bool(chars) and sum(c.isalpha() for c in chars) < 0.1 * len(chars) \
            and sum(c.isdigit() for c in chars) >= 0.3 * len(chars)
        label = m.group(1).strip() if m.group(1).strip() in known else OTHER_SOURCE
        shapes.setdefault(label, []).append((len(body.strip()), links, numbers))
    if not shapes:
        return [("PASS", "Import shapes", "no imported pages", "")]
    out = []
    for label, rows in sorted(shapes.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        q, n = _quartiles([r[0] for r in rows]), len(rows)
        out.append(("PASS", f"Import shapes: {label}", (
            f"{n:,} page{'s' * (n != 1)}, length quartiles {q[0]:,} / {q[1]:,} / {q[2]:,} "
            f"characters, mostly links {sum(r[1] for r in rows) / n:.0%}, numbers only "
            f"{sum(r[2] for r in rows) / n:.0%}"), ""))
    return out


def check_claims(root: Path) -> tuple[str, str, str, str]:
    """Live claims per authorship tier (v5 F3); a preview, so doctor records no baseline."""
    v = Vault(root)
    rows = claims.tiers(v, EventLog(v).iter_events())
    return "PASS", "Claim tiers", ", ".join(
        f"{t} {sum(r['tier'] == t for r in rows)}" for t in claims.TIERS), ""


def check_contract_size(root: Path) -> tuple[str, str, str, str]:
    name = "Teaching contract size"
    p = Path(root) / "me" / "teaching-contract.md"
    n = len(p.read_bytes().decode("utf-8")) if p.is_file() else 0  # whole file, before scoping
    if n > CONTRACT_MAX_CHARS:
        return ("WARN", name, f"me/teaching-contract.md is {n} characters (whole file, before "
                f"domain scoping); keepfold_start cuts the contract at {CONTRACT_MAX_CHARS} "
                "(about 1,200 tokens)",
                "Trim the core rules or move domain-specific ones under `## Domain overrides`.")
    return "PASS", name, f"{n} characters", ""


def _base(s) -> str:
    """File name of a Windows or POSIX path, whatever OS reads it."""
    return re.split(r"[\\/]", str(s))[-1].casefold()


def _is_keepfold_server(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    base = _base
    args = entry.get("args") if isinstance(entry.get("args"), list) else []
    return base(entry.get("command", "")).startswith("keepfold") or (
        "mcp" in args and any(base(a).startswith("keepfold") for a in args))


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _has_keepfold(path: Path) -> bool:
    """`path`'s mcpServers hold a Keepfold server."""
    servers = _read_json(path).get("mcpServers")
    return isinstance(servers, dict) and any(_is_keepfold_server(e) for e in servers.values())


def plugin_enabled(settings: Path | None = None) -> bool:
    """True when ~/.claude/settings.json enables a `keepfold@...` Claude Code plugin."""
    settings = settings or Path.home() / ".claude" / "settings.json"
    plugins = _read_json(settings).get("enabledPlugins")
    return isinstance(plugins, dict) and any(
        str(k).startswith("keepfold@") and v is True for k, v in plugins.items())


def last_calls(root: Path) -> dict[str, float]:
    """Client name -> the time (epoch s) of its latest call, from the marks the server writes
    (mcp_server.SEEN: one file per client, its mtime the call, rewritten at most once a
    minute)."""
    out = {}
    for p in (Path(root) / ".keepfold" / "clients").glob("*.txt"):
        try:
            out[p.read_text(encoding="utf-8").strip() or p.stem] = p.stat().st_mtime
        except OSError:
            continue
    return out


def _when(t: float | None) -> str:
    from datetime import datetime
    return f"last call {datetime.fromtimestamp(t):%Y-%m-%d %H:%M}" if t else \
        "no call recorded yet"


def check_clients(root: Path, settings: Path | None = None,
                  desktop_configs: list[Path] | None = None) -> list[tuple[str, str, str, str]]:
    """Per Claude client: Keepfold configured, the Claude Code prime hook, and its last call."""
    calls = last_calls(root)
    seen = {k.casefold(): t for k, t in calls.items()}

    def last(chat: bool) -> float | None:  # the Chat tab names itself claude-ai; Code claude-code
        ts = [t for k, t in seen.items() if "claude" in k and ("claude-code" in k) != chat]
        return max(ts, default=None)

    rows = []
    paths = claude_desktop_config_paths() if desktop_configs is None else desktop_configs
    have = [str(p) for p in paths if _has_keepfold(p)]
    name, t = "Claude Desktop (Chat)", last(True)
    if not paths:
        rows.append(("PASS", name, "not installed", ""))
    elif not have:
        rows.append(("WARN", name, f"Keepfold is not in {paths[0]}, so its chats are not "
                     "logged", "Tick Claude Desktop under Connect in the Keepfold app (or run "
                     "`keepfold setup`), then quit Claude Desktop from the tray and open it "
                     "again."))
    else:
        rows.append(("PASS" if t else "WARN", name, f"Keepfold in {'; '.join(have)}; {_when(t)}",
                     "" if t else "Quit Claude Desktop from the tray, open it again and start "
                     "a new chat; if no call shows up, see docs/INSTALL.md (Troubleshooting)."))
    home = Path.home()
    settings = settings or home / ".claude" / "settings.json"
    plugin = plugin_enabled(settings)
    entry = _has_keepfold(home / ".claude.json")
    hook = plugin or "prime-hook" in json.dumps(_read_json(settings).get("hooks") or {})
    name, t = "Claude Code", last(False)
    if not targets.ClaudeCode().detect():  # the app's test: `claude` on PATH counts too
        rows.append(("PASS", name, "not installed", ""))
        return rows
    how = "plugin" if plugin else "~/.claude.json" if entry else \
        "the desktop config only (its Code tab)" if have else ""
    detail = (f"Keepfold from {how}" if how else "Keepfold not configured") + \
        f"; prime hook {'installed' if hook else 'missing'}; {_when(t)}"
    ok = bool(how) and hook and t
    rows.append(("PASS" if ok else "WARN", name, detail, "" if ok else
                 "Tick Claude Code under Connect in the Keepfold app: it installs the Keepfold "
                 "plugin, which brings the server and the prime hook. Its chats are saved "
                 "from their transcripts either way."
                 if not (plugin and hook) else
                 "Restart Claude Code and start a new chat; if no call shows up, see "
                 "docs/INSTALL.md (Troubleshooting)."))
    return rows


def check_duplicate_servers(settings: Path | None = None,
                            desktop_configs: list[Path] | None = None
                            ) -> tuple[str, str, str, str]:
    """WARN when the Claude Code plugin and a desktop-config entry both register Keepfold."""
    name = "One Keepfold server per client"
    plugin_on = plugin_enabled(settings)
    desktop = [str(p) for p in (claude_desktop_config_paths() if desktop_configs is None
                                else desktop_configs) if _has_keepfold(p)]
    if plugin_on and desktop:
        return ("WARN", name, "the Claude Code plugin and the desktop config "
                f"({'; '.join(desktop)}) both register Keepfold; the desktop app's Code tab loads "
                "both, so every tool and session is duplicated there",
                "Keep both if you use them: Claude Chat needs the desktop entry and the Claude "
                "Code CLI needs the plugin. In the desktop Code tab, expect two copies.")
    return "PASS", name, "plugin" if plugin_on else ("desktop config" if desktop else "none"), ""


def check_moved(root: Path | None) -> list[tuple[str, str, str, str]]:
    """R3: "Moved from Cortex" (keepfold.migrate.status: done, nothing to move, not moved yet,
    both folders), and a WARN while a CORTEX_* variable is set (Keepfold reads only KEEPFOLD_*)."""
    from keepfold import migrate
    state, value, fix = migrate.status(root)
    rows = [] if value == "nothing to move" else [  # only for people who had Cortex
        ("PASS" if state == "ok" else "WARN", "Moved from Cortex", value, fix)]
    if old := migrate.old_env():
        rows.append(("WARN", "Old setting names", ", ".join(old) + " still set",
                     "Rename " + ", ".join(f"{k} to KEEPFOLD_{k[len('CORTEX_'):]}" for k in old)
                     + ": Keepfold no longer reads the CORTEX_ names."))
    return rows
