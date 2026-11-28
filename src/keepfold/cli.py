"""The `keepfold` command-line interface. Logic lives in the core modules."""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from collections import Counter
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import typer

from keepfold import backup as backup_mod
from keepfold import dashboard as dashboard_mod
from keepfold import doctor as doctor_mod
from keepfold import levels, planner, render, taint, vault_format
from keepfold import session as session_mod
from keepfold._proc import NO_WINDOW, proxy_env
from keepfold.catalog import Catalog, page_slug, resolve_or_inbox
from keepfold.catalog import sync as catalog_sync
from keepfold.events import EventLog, local_day, log_assessment, parse_ts, void_event
from keepfold.session import _git
from keepfold.update import CHECKOUT, REPO  # stdlib-only; update must not import cli (a cycle)
from keepfold.vault import CHAT_ARCHIVE, PRIVATE_BROWSE_DIR, Vault, VaultError, in_temp_folder

EPILOG = ("About you: claims [--unasked|--summary], voice, export context · "
          "Study: today, brief, reflect, recall, log, eval · "
          "Import: import [--dry-run|--private], import-chats, watch, digest · "
          "Chats: chats, harvest status, safety-net, fact-harvest · "
          "Vault: lint, relink, concept duplicates, topics, replay, undo, backup, restore · "
          "Setup: app, setup [--print-mcp-config], init, doctor, model download, exclude")
# typer 0.29 prints <bool> after every flag, --help too; a flag takes no value, so show none
_display_type = typer.core.TyperOption.display_type_rich
typer.core.TyperOption.display_type_rich = \
    lambda self, ctx: None if self.is_flag else _display_type(self, ctx)
app = typer.Typer(no_args_is_help=True, epilog=EPILOG)
log_app = typer.Typer(no_args_is_help=True, help="Record evidence (source external).")
concept_app = typer.Typer(no_args_is_help=True, help="Edit the concept catalog.")
app.add_typer(log_app, name="log")
app.add_typer(concept_app, name="concept")
catalog_app = typer.Typer(no_args_is_help=True, help="Keep catalog.yaml in step with the notes.")
app.add_typer(catalog_app, name="catalog")
session_app = typer.Typer(no_args_is_help=True, help="Close orphan sessions.")
app.add_typer(session_app, name="session")
model_app = typer.Typer(no_args_is_help=True, help="The local embedding model for recall.")
app.add_typer(model_app, name="model")
eval_app = typer.Typer(help="Measure ranking on logged usage; bare `keepfold eval` runs `all`.")
app.add_typer(eval_app, name="eval")
topics_app = typer.Typer(no_args_is_help=True, help="The topic tree: hubs, filing, rebuilds.")
app.add_typer(topics_app, name="topics")

VaultOpt = Annotated[Path | None, typer.Option(
    "--vault", envvar="KEEPFOLD_VAULT", help="Vault path; else KEEPFOLD_VAULT, else "
    "~/.keepfold/config.yaml.")]
SECTIONS = ("me", "tracks", "concepts", "areas", "domains", "mistakes", "problems",
            "assessments", "applications", "sessions", "events", ".keepfold", "resources", "notes",
            "projects", "decisions", "loops", "raw")
PRE_PUSH = "#!/bin/sh\necho 'keepfold: this vault is local only; push refused.' >&2\nexit 1\n"
WORKSPACE_IGNORE = ".obsidian/workspace*.json"
PRIVATE_BROWSE_IGNORE = PRIVATE_BROWSE_DIR + "/"  # v7 C3: private browsing never enters git
CHAT_ARCHIVE_IGNORE = CHAT_ARCHIVE + "/"  # v9 C5: imported AI chats never enter git
TMP_IGNORE = ".*.tmp"  # D1 §2.2: a crash's leftover vault._atomic_write temp file never enters git
FORMAT_KEEP = "!.keepfold/format.json"  # D7 §1.3: the format stamp is in the vault's history
GITIGNORE = (f".keepfold/*\n!.keepfold/config.yaml\n{FORMAT_KEEP}\n{WORKSPACE_IGNORE}\n"
             f"{PRIVATE_BROWSE_IGNORE}\n{CHAT_ARCHIVE_IGNORE}\n{TMP_IGNORE}\n")
SESSION_ID_RE = re.compile(r"^s_[0-9A-HJKMNP-TV-Z]{26}$")
OLD_COLOR_GROUPS = [("[keepfold_status:solid]", "1D9E75"), ("[keepfold_status:learning]", "BA7517"),
                    ("[keepfold_status:needs_refresh]", "993556"), ("path:domains", "7F77DD"),
                    ("path:areas", "888780"), ("path:projects", "7F77DD"),
                    ("path:resources", "888780"), ("path:notes", "888780"),
                    ("path:decisions", "888780")]  # before v6: init upgrades only these
COLOR_GROUPS = [  # first match wins in Obsidian, so the order matters
    ("file:my-topic-map", "FFFFFF"), ("tag:#topic", "F472B6"),
    ('path:"me/" OR path:"people/"', "FDE68A"),
    ("[keepfold_status:solid]", "22C55E"), ("[keepfold_status:needs_refresh]", "F59E0B"),
    ("[keepfold_status:learning]", "3B82F6"), ("[keepfold_status:unverified]", "22D3EE"),
    ("[keepfold_status:not_started]", "475569"),
    ('path:"domains/"', "8B5CF6"), ('path:"areas/"', "2DD4BF"), ('path:"problems/"', "FB923C"),
    ('path:"mistakes/"', "EF4444"), ('path:"assessments/" OR path:"applications/"', "FACC15"),
    ('path:"projects/"', "84CC16"), ('path:"decisions/"', "10B981"),
    ('path:"journal/"', "C4B5FD"), ('path:"notes/"', "A78BFA"), ('path:"loops/"', "FDBA74"),
    ('path:"resources/"', "5B4636"),
    ('path:"sessions/" OR path:"events/" OR path:"views/" OR path:"raw/"', "3F3F46"),
    ("file:index OR file:inbox OR file:log OR file:Today OR file:BRIEF", "E2E8F0")]
BASES = {
    "views/Due.base": """filters:
  and:
    - file.inFolder("concepts")
    - keepfold_next <= today()
views:
  - type: table
    name: Due
    order:
      - file.name
      - keepfold_status
      - keepfold_next
    sort:
      - property: keepfold_next
        direction: ASC
""",
    "views/Weak.base": """filters:
  and:
    - file.inFolder("concepts")
    - or:
        - keepfold_status == "learning"
        - keepfold_status == "needs_refresh"
views:
  - type: table
    name: Weak
    order:
      - file.name
      - keepfold_status
      - keepfold_next
""",
    "views/Projects.base": """filters:
  and:
    - file.inFolder("projects")
views:
  - type: table
    name: Projects
    groupBy:
      property: status
      direction: ASC
    order:
      - file.name
      - status
      - summary
      - stack
""",
    "views/Resources.base": """filters:
  and:
    - file.inFolder("resources")
views:
  - type: table
    name: Resources
    groupBy:
      property: status
      direction: ASC
    order:
      - file.name
      - status
      - resource_type
      - summary
""",
    "views/Mistakes.base": """filters:
  and:
    - file.inFolder("mistakes")
views:
  - type: table
    name: Mistakes
    order:
      - file.name
      - file.mtime
    sort:
      - property: file.mtime
        direction: DESC
""",
}
VERDICTS = {"pass": "correct", "partial": "partial", "fail": "wrong"}


def _pointer() -> Path:
    return Path.home() / ".keepfold" / "config.yaml"


def _write_config(**keys) -> Path:
    """Set `keys` in ~/.keepfold/config.yaml, keeping its other keys (exclude, rerank)."""
    return taint.write_config(keys)


def _vault(path: Path | Vault | None) -> Vault:
    if isinstance(path, Vault):  # undo reruns commands on its locked Vault (the lock is per Vault)
        return path
    if path is None and (v := taint.config().get("vault")):
        path = Path(v)
    if path is None:
        raise VaultError("invalid_argument", "No vault set; run `keepfold app` (or `keepfold "
                         "setup` in a terminal) to set Keepfold up, or pass --vault PATH or set "
                         "KEEPFOLD_VAULT.")
    if not Path(path).is_dir():
        raise VaultError("invalid_argument", f"The vault folder {path} does not exist; "
                         "run `keepfold init <path>`.")
    _opened.append(Vault(Path(path)))
    return _opened[-1]


def _refuse_excluded(action: str) -> None:
    """Commands that print me/ text refuse inside an `exclude:` folder, as the server does:
    an agent there could otherwise read the user's lines through Bash."""
    if folder := taint.excluded(*taint.session_dirs()):
        raise VaultError("invalid_argument", f"{folder} is excluded; refusing to {action}.")


_opened: list[Vault] = []  # the vaults this command opened; _errors commits what it wrote
_depth = 0  # undo reruns replay and today; only the outermost command commits


def _commit_cli(name: str) -> None:
    """`keepfold: cli <name>` commit of the files this command wrote (none: no commit). Other
    uncommitted changes stay for the next session close's `other changes` commit."""
    for v in dict.fromkeys(_opened):
        if v.writes:
            session_mod._commit(v, f"keepfold: cli {name}", v.wrote_since(0))


def _errors(fn):
    """Print a VaultError as one line on stderr and exit 1, instead of a traceback. On
    success, commit the vault files the command wrote (not for `mcp`: its sessions commit)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        global _depth
        if _depth == 0:
            _opened.clear()
        _depth += 1
        try:
            out = fn(*args, **kwargs)
        except VaultError as e:
            typer.echo(f"error ({e.code}): {e}", err=True)
            raise typer.Exit(1) from None
        finally:
            _depth -= 1
        if _depth == 0 and fn.__name__ != "mcp":
            _commit_cli(fn.__name__.removesuffix("_cmd").replace("_", " "))  # "log taught"
        return out
    return wrapper


def _version(value: bool) -> None:
    if value:
        typer.echo(f"keepfold {version('keepfold')}")
        raise typer.Exit()


@app.callback()
def main(
    _v: bool = typer.Option(False, "--version", callback=_version, is_eager=True),
) -> None:
    """Keepfold, your local second brain. New here? Run `keepfold app`."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # state glyphs on a cp1252 console
        except (AttributeError, ValueError):
            pass


def _obsidian(vault: Vault, did: list, skipped: list) -> None:
    """Graph colours and Bases views. colorGroups are set only when graph.json has none or only
    old Keepfold defaults (order matters, so merging into the user's own groups would scramble
    them); every other graph setting is kept."""
    graph = vault.root / ".obsidian" / "graph.json"
    data = json.loads(graph.read_text(encoding="utf-8")) if graph.exists() else {}
    have = [g.get("query") for g in data.get("colorGroups") or []]
    new = [{"query": q, "color": {"a": 1, "rgb": int(h, 16)}} for q, h in COLOR_GROUPS]
    if have == [g["query"] for g in new]:
        skipped.append(".obsidian/graph.json colorGroups")
    elif set(have) <= {q for q, _ in OLD_COLOR_GROUPS}:
        data["colorGroups"] = new
        graph.parent.mkdir(exist_ok=True)
        vault.write_text(graph, json.dumps(data, indent=2) + "\n")
        did.append(".obsidian/graph.json colorGroups")
    else:
        skipped.append(".obsidian/graph.json colorGroups (custom colour groups)")
    for rel, text in BASES.items():
        if (vault.root / rel).exists():
            skipped.append(rel)
        else:
            (vault.root / rel).parent.mkdir(exist_ok=True)
            vault.write_text(vault.root / rel, text)
            did.append(rel)


def _page(summary: str, *headings: str, intro: str = "") -> str:
    body = "".join(f"\n## {h}\n" for h in headings)
    return f"---\nsummary: {summary}\n---\n\n# {summary}\n{intro}{body}"


CLAIM_TAIL = ("Past", "Not me")  # v5 F1: reserved on every claim page, never served
ME_PAGES = {
    "me/profile.md": _page("Who I am", "Background", "Current focus", *CLAIM_TAIL),
    "me/teaching-contract.md": _page(
        "How to teach me", "Core rules",
        intro="\nAsk before you teach. Keep answers short.\n") + (
        "\n- Ask a closed-book question before explaining.\n\n## Domain overrides\n\n"
        "<!-- Add `### <domain id>` subsections (e.g. `### math`); only matching ones load. -->\n"),
    "me/preferences.md": _page("My preferences", "Coding", "Communication", "Learning",
                               "Workflow", "Environment", "Career", "Personal", *CLAIM_TAIL),
    "me/goals.md": _page("My goals", "Now", "Later", *CLAIM_TAIL),
    "me/setup.md": _page("My setup", "Hardware", "Software", *CLAIM_TAIL),
    "me/personality.md": _page(
        "How I tick", "Values", "How I think", "How I decide", "Strengths", "Growth edges",
        "Communication", "Energy and routines", *CLAIM_TAIL,
        intro="\nOne line per entry: `- <trait> — seen: <evidence> (confirmed YYYY-MM-DD)`.\n"),
    "me/portrait.md": _page(
        "My monthly portrait",
        intro="\nOne `## YYYY-MM` section per month, drafted by the assistant and approved by "
              "me.\n"),
    "me/core.md": _page(  # prime.core_lines: pinned, first in every prime
        "Always about me", "Core", *CLAIM_TAIL,
        intro="\nPinned: every chat starts with these lines, word for word, ahead of "
              "everything else (500 characters in all; a line that does not fit is left out "
              "whole). Only lines you write or confirm count.\n"),
    "me/voice.md": _page(  # PHASE4 V2: served only by keepfold_as_me
        "How I write", "Everyone", *CLAIM_TAIL,
        intro="\nStyle rules per audience: one `## <audience>` section each (professor, friend, "
              "people/<slug>), e.g. `- Formal greeting, at most 5 sentences, signs \"Regards\"`."
              "\n"),
}


# ---- init, doctor ---------------------------------------------------------------

@app.command()
@_errors
def init(
    path: Path,
    default: Annotated[bool, typer.Option(
        "--default", help="Make this the default vault even when one is already set.")]
    = False,
) -> None:
    """Create or adopt a vault (idempotent; never touches notes); point ~/.keepfold at it when no
    default vault is set yet, or with --default."""
    _report(*_init(path.resolve(), default=default))
    typer.echo("Next: run `keepfold app`")


def _report(did: list[str], skipped: list[str], warned: list[str] = ()) -> None:
    for line in did:
        typer.echo(f"created  {line}")
    for line in skipped:
        typer.echo(f"skipped  {line} (exists)")
    for line in warned:
        typer.echo(f"warning  {line}")


INSTALL_MARKER = "keepfold-install.txt"  # installer/setup_app.py drops it in the install folder


def install_folder(p: Path) -> Path | None:
    """The KeepfoldSetup.exe install folder `p` is in (or is), else None."""
    p = Path(p).resolve()
    return next((d for d in (p, *p.parents) if (d / INSTALL_MARKER).is_file()), None)


def _init(root: Path, default: bool = False) -> tuple[list[str], list[str], list[str]]:
    """The work of `keepfold init`: (created, skipped, warnings). The ~/.keepfold pointer moves
    only when it names no vault yet or `default` is set (every chat's hook and server read it)."""
    if root.exists() and not root.is_dir():
        raise VaultError("invalid_argument", f"{root} is a file; pick a folder.")
    if inst := install_folder(root):
        raise VaultError("invalid_argument", f"{root} is inside the folder Keepfold is installed "
                         f"in ({inst}); uninstalling Keepfold empties that folder. Pick another "
                         "folder for your notes.")
    created = not root.exists() or not any(root.iterdir())  # else an adopted folder: "pre-1.0"
    root.mkdir(parents=True, exist_ok=True)
    vault = Vault(root)
    did, skipped = [], []
    for name in SECTIONS:
        (skipped if (root / name).is_dir() else did).append(f"{name}/")
        (root / name).mkdir(exist_ok=True)
    for rel, text in ((".keepfold/config.yaml", "schema: 1\n"), (".gitignore", GITIGNORE),
                      (".gitattributes", "* text=auto eol=lf\n")):  # keep LF despite core.autocrlf
        if (root / rel).exists():
            skipped.append(rel)
        else:
            vault.write_text(root / rel, text)
            did.append(rel)
    git_on, private_kept = session_mod.git_choice()
    ignore = root / ".gitignore"
    old = ignore.read_text(encoding="utf-8")
    # older vaults: append if missing; the private line only while private browsing stays out
    # of git (the default)
    for line in (FORMAT_KEEP, WORKSPACE_IGNORE,
                 *(() if private_kept else (PRIVATE_BROWSE_IGNORE,)), CHAT_ARCHIVE_IGNORE,
                 TMP_IGNORE):
        if line not in old.splitlines():
            old += ("" if old.endswith("\n") or not old else "\n") + line + "\n"
            vault.write_text(ignore, old)
            did.append(".gitignore line")
    if private_kept:
        from keepfold.browse.forget import drop_private_ignore
        if drop_private_ignore(vault):
            did.append(".gitignore: browse/private/ kept in git (your choice)")
    _obsidian(vault, did, skipped)
    for rel, text in ME_PAGES.items():
        if (root / rel).exists():
            skipped.append(rel)
        else:
            vault.write_text(root / rel, text)
            did.append(rel)
    if vault_format.AUTO_STAMP and vault_format.write_stamp(vault, created=created):
        did.append(vault_format.STAMP)
    warned = []
    if not git_on:  # the owner's choice; an existing .git is left as it is
        skipped.append("git (off in settings)")
        return _init_pointer(root, default, did, skipped, warned)
    if (root / ".git").exists():
        skipped.append("git init")
    elif session_mod.git_exe() is None:
        warned.append("git not found: no vault history or push block yet; install git, then "
                      "rerun `keepfold init` or `keepfold setup`")
    else:
        r = _git(root, "init", "-q")
        if r.returncode:
            raise VaultError("invalid_argument", f"git init failed: {r.stderr.strip()}")
        did.append("git init")
    hook = root / ".git" / "hooks" / "pre-push"
    if hook.exists():
        skipped.append("pre-push hook")
    elif (root / ".git").is_dir():
        vault.write_text(hook, PRE_PUSH)
        os.chmod(hook, 0o755)
        did.append("pre-push hook")
    # without an identity every session commit fails silently and `keepfold undo` does nothing
    if ((root / ".git").is_dir() and session_mod.git_exe()
            and not _git(root, "config", "user.email").stdout.strip()):
        _git(root, "config", "user.name", "Keepfold")
        _git(root, "config", "user.email", "keepfold@localhost")
        did.append("git identity Keepfold <keepfold@localhost> (this vault only)")
    # D7 §1.3: git syncs its objects and refs, so a power cut cannot leave a broken history
    if ((root / ".git").is_dir() and session_mod.git_exe()
            and not _git(root, "config", "core.fsync").stdout.strip()):
        _git(root, "config", "core.fsync", "committed")
        did.append("git core.fsync committed")
    if (root / ".git").is_dir() and session_mod.git_exe() and vault.writes:
        # the starter pages get a first version, so `keepfold undo` has one to go back to
        if session_mod._commit(vault, "keepfold: init", vault.wrote_since(0)):
            did.append("git commit keepfold: init")
    return _init_pointer(root, default, did, skipped, warned)


def _init_pointer(root: Path, default: bool, did: list[str], skipped: list[str],
                  warned: list[str]) -> tuple[list[str], list[str], list[str]]:
    """The last step of `_init`: point ~/.keepfold at the vault if it names none (or `default`)."""
    current = taint.config().get("vault")
    if not current:  # a first setup: What's new starts at this version, not at BEFORE_NEWS
        from keepfold.app import news
        news.start()
    if (default and current and Path(current).is_dir() and not in_temp_folder(current)
            and in_temp_folder(root)):  # a probe or a stray click must not take the vault over
        warned.append(f"Kept your vault {current}: {root} is a temporary folder, which Windows "
                      "can empty. Pick a folder outside Temp." if sys.platform == "win32" else
                      f"Kept your vault {current}: {root} is a temporary folder, which the "
                      "system can empty. Pick a folder in your home folder.")
    elif default or not current:
        did.append(f"pointer {_write_config(vault=str(root))}")
    elif Path(current).resolve() == root:
        skipped.append(f"pointer {_pointer()}")
    else:
        warned.append(f"kept the default vault {current}; run `keepfold init \"{root}\" "
                      "--default` to make this one the default")
    return did, skipped, warned


@app.command()
def doctor(vault: VaultOpt = None, privacy: Annotated[bool, typer.Option(
        "--privacy", help="Print only where Keepfold connects.")] = False,
        security: Annotated[bool, typer.Option(
            "--security", help="Fire six memory attacks at a throwaway made-up vault "
            "(never your notes) and say PASS or FAIL for each.")] = False,
        report: Annotated[bool, typer.Option(
            "--report", help="Print the details to paste into a bug report: versions and "
            "each check's state, no paths, names or notes.")] = False) -> None:
    """Check Python/FTS5, the search model, PATH, the local model, OneDrive and the vault git,
    then list where Keepfold connects; exit 1 on any FAIL. --security: the offline red team
    (keepfold.redteam) instead. --report: the details for a bug report."""
    if report:
        from keepfold.app import status
        try:
            root = _vault(vault).root
        except VaultError:
            root = None
        typer.echo(status.report(root))
        return
    if privacy:
        typer.echo("\n".join(doctor_mod.connects()))
        return
    if security:
        from keepfold import redteam

        probes = redteam.run()
        for p in probes:
            typer.echo(p.line())
        bad = sum(not p.ok for p in probes)
        typer.echo(f"\n{len(probes) - bad} of {len(probes)} attacks stopped"
                   + (f"; {bad} got through." if bad else "."))
        if bad:
            raise typer.Exit(1)
        return
    results = [doctor_mod.check_python(), doctor_mod.check_duplicate_servers(),
               doctor_mod.check_exclude(), doctor_mod.check_search_model(),
               doctor_mod.check_on_path(), doctor_mod.check_local_model(),
               doctor_mod.check_ai_apps()]
    try:
        root = _vault(vault).root
        results += [doctor_mod.check_onedrive(root), doctor_mod.check_git(root),
                    doctor_mod.check_git_identity(root), *doctor_mod.check_git_state(root),
                    doctor_mod.check_id_clashes(root),
                    doctor_mod.check_sync_copies(root), *doctor_mod.check_damaged_stores(root),
                    doctor_mod.check_contract_size(root), doctor_mod.check_claims(root),
                    doctor_mod.check_kill(root), doctor_mod.check_me_pages(root),
                    doctor_mod.check_private_browse_git(root),
                    doctor_mod.check_chat_archive_git(root),
                    doctor_mod.check_browse_legacy(root), *doctor_mod.check_clients(root),
                    *doctor_mod.check_import_shapes(root)]
    except VaultError as e:
        results.append(("FAIL", "Vault", str(e), f"Open Keepfold from {p} and set "
                        "up a vault under Setup." if (p := doctor_mod.app_place()) else
                        "Run `keepfold app` (or `keepfold setup` in a terminal) to set "
                        "Keepfold up, or pass --vault."))
    # the vault's folder even when opening it failed (a move from Cortex it waits for)
    results += doctor_mod.check_moved(Path(vault) if vault else taint._vault_root())
    for status, name, detail, fix in results:
        typer.echo(f"{status:<4}  {name}: {detail}")
        if fix:
            typer.echo(f"      fix: {fix}")
    typer.echo("\n" + "\n".join(doctor_mod.connects()))
    if any(r[0] == "FAIL" for r in results):
        raise typer.Exit(1)


MOVED = {"nothing": "nothing to move", "done": "done", "both": "both folders exist",
         "blocked": "blocked", "error": "failed"}


@app.command()
def migrate(vaults: Annotated[list[Path] | None, typer.Option(
        "--vault", help="A vault to move (repeatable); else the configured one.")] = None,
        as_json: Annotated[bool, typer.Option(
            "--json", help="One JSON object (state, exit, home, vaults, apps) for the "
            "installer.")] = False) -> None:
    """Move what Cortex (0.6.1 and older) left: ~/.cortex to ~/.keepfold, a vault's .cortex/ to
    .keepfold/ with its ignore lines and note keys, and the AI-app connections. Exit 0 done or
    nothing to do, 2 blocked (an old Cortex still runs), 1 error."""
    from keepfold import migrate as migrate_mod
    r = migrate_mod.everything(vaults or None)
    if as_json:
        typer.echo(json.dumps(r, default=str))  # the installer reads it: never a crash
    else:
        for name, part in (("Settings", r["home"]),
                           *((f"Vault {x['path']}", x) for x in r["vaults"])):
            n = f", {part['notes']} pages updated" if part.get("notes") else ""
            typer.echo(f"{name}: {MOVED.get(part['state'], part['state'])}{n}")
            if part.get("message"):
                typer.echo(f"  {part['message']}")
            if part.get("skipped"):
                typer.echo(f"  {part['skipped']} pages kept their old keys: their front matter "
                           "could not be read.")
        for row in (r["apps"] or {}).get("rows", []):
            if row["action"] != "nothing":
                typer.echo(f"AI app {row['app']}: {row['action']}"
                           + (f"\n  {row['note']}" if row.get("note") else ""))
    raise typer.Exit(r["exit"])


# ---- setup wizard, sample vault -------------------------------------------------

RELEASES = f"https://github.com/{REPO}/releases"
FIRST_PROMPTS = ("what do you know about me?", "/keepfold",
                 "remember that I prefer short answers")
PRIVACY = ("Privacy: the vault stays in a folder on this disk; Keepfold has no account or "
           "telemetry. It downloads the search model, Ollama or a local model only when you ask. "
           "While the app runs it checks GitHub for a new version (turn this off under Status → "
           "Updates). Its server listens on 127.0.0.1 only. Tool results go to the AI model you "
           "chat with. See docs/PRIVACY.md.")


def _marketplace() -> str | None:
    """Where `claude plugin marketplace add` points: this source checkout, else the published
    repo; None while neither exists."""
    if (CHECKOUT / ".claude-plugin" / "marketplace.json").is_file():
        return str(CHECKOUT)
    return None if "<" in REPO else REPO


NO_MARKETPLACE = ("Claude Code plugin: no Keepfold source checkout found and no published repo "
                  "yet. Clone the Keepfold repo, then in Claude Code type `/plugin marketplace add "
                  "<the clone's folder>` and `/plugin install keepfold@keepfold`, and restart it.")


def _default_vault() -> Path:
    return taint._vault_root() or Path.home() / "Keepfold"


@app.command()
@_errors
def setup(
    vault: Annotated[Path | None, typer.Option(
        "--vault", help="Vault folder; asked when missing (default ~/Keepfold).")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Take every default; ask nothing.")]
    = False,
    no_model: Annotated[bool, typer.Option("--no-model", help="Skip the model download.")]
    = False,
    no_desktop: Annotated[bool, typer.Option(
        "--no-desktop", help="Leave the Claude desktop config alone.")] = False,
    no_code: Annotated[bool, typer.Option("--no-code", help="Skip the Claude Code plugin.")]
    = False,
    sample: Annotated[bool | None, typer.Option(
        "--sample/--no-sample", help="Add the sample vault (default: yes for a new vault).")]
    = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the plan; change nothing.")]
    = False,
    print_mcp_config: Annotated[bool, typer.Option(
        "--print-mcp-config", help="Print only an mcpServers JSON snippet that runs `keepfold mcp "
        "--vault` (Cursor, Gemini CLI); change nothing.")] = False,
) -> None:
    """Set up Keepfold step by step (safe to rerun): vault, sample, model, Claude Code, Claude
    desktop, checks."""
    from keepfold import clients
    from keepfold import sample as sample_mod

    if print_mcp_config:
        _print_mcp_config(vault or os.environ.get("KEEPFOLD_VAULT"))  # as VaultOpt resolves it
        return

    def ask(question: str) -> bool:
        return yes or typer.confirm(question, default=True)

    # 1. preflight
    status, name, detail, fix = doctor_mod.check_python()
    typer.echo(f"{status:<4}  {name}: {detail}")
    if status == "FAIL":
        typer.echo(f"      fix: {fix}")
        raise typer.Exit(1)
    if session_mod.git_exe() is None and session_mod.git_choice()[0]:
        typer.echo("WARN  git not found: the vault gets no history or push block until you "
                   "install git and rerun `keepfold setup`.")
    if shutil.which("keepfold") is None:
        typer.echo("WARN  `keepfold` is not on PATH: run `uv tool update-shell`, then open a new "
                   "terminal. Claude Code's plugin needs it on PATH.")

    # 2. vault location
    if vault is None:
        vault = _default_vault() if yes else Path(typer.prompt(  # Explorer's "Copy as path" quotes
            "Vault folder", default=str(_default_vault())).strip().strip("\"'"))
    root = vault.expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise VaultError("invalid_argument", f"{root} is a file; pick a folder.")
    if inst := install_folder(root):  # before the sample pages land there
        raise VaultError("invalid_argument", f"{root} is inside the folder Keepfold is installed "
                         f"in ({inst}); pick another folder for your notes.")
    sync = doctor_mod.check_onedrive(root)
    if sync[0] != "PASS":
        typer.echo(f"WARN  {sync[2]}: {sync[3]}")
    new = not (root / ".keepfold").is_dir()
    if new and root.is_dir() and any(root.iterdir()) and not ask(
            f"{root} has files but is no Keepfold vault yet. Use it? Keepfold adds folders and "
            "never changes your notes."):
        typer.echo("Nothing changed. Run `keepfold setup` again and pick another folder.")
        raise typer.Exit(1)
    if sample is None:
        sample = new and ask("Add a small sample vault (a made-up person, Sam) to try things? "
                             "`keepfold sample remove` clears it.")
    model = not no_model and ask("Download the search model now? About 150 MB, once, from "
                                 "huggingface.co; without it recall uses keywords only.")
    entry = clients.server_entry(clients.mcp_command(), root)
    configs = [] if no_desktop else clients.setup_targets(clients.claude_desktop_config_paths())

    if dry_run:
        typer.echo(f"plan     vault {root} ({'new' if new else 'exists'}): missing folders, "
                   f"me/ pages, .gitignore, git repo, pre-push hook; pointer {_pointer()}")
        if sample:
            missing = [r for r in sample_mod.files() if not (root / r).exists()]
            typer.echo(f"plan     sample: {len(missing)} of {len(sample_mod.files())} pages "
                       f"missing, {len(sample_mod.SEED)} sample events")
        typer.echo("plan     model: download (about 150 MB) into "
                   f"{Path.home() / '.keepfold' / 'models'}" if model else "skip     model")
        src = _marketplace()
        typer.echo("skip     Claude Code plugin" if no_code else
                   f"plan     Claude Code: claude plugin marketplace add {src}; "
                   "claude plugin install keepfold@keepfold" if src
                   else f"plan     {NO_MARKETPLACE}")
        for p in configs:
            try:
                typer.echo(f"plan     Claude desktop {p}: "
                           f"{_desktop_change(p, entry, dry_run=True)}")
            except ValueError as e:
                typer.echo(f"warning  {e}")
        if not no_desktop and not configs:
            typer.echo("plan     Claude desktop: no config folder found; print the entry")
        typer.echo("skip     Claude desktop" if no_desktop else
                   f"         entry: {json.dumps({'keepfold': entry})}")
        typer.echo("plan     run the doctor checks")
        typer.echo("dry run: nothing written")
        return

    # 3-4. sample pages first, so Sam's me/ pages take the place of the empty templates
    did, skipped, warned = [], [], []
    if sample:
        root.mkdir(parents=True, exist_ok=True)
        d, s = sample_mod.seed(Vault(root))
        did, skipped = [f"sample {x}" for x in d], [f"sample {x}" for x in s]
    d, s, warned = _init(root, default=True)  # the wizard's vault choice is explicit
    _report(did + d, skipped + s, warned)

    # 5. model
    if model:
        try:
            model_download(vault=root)
        except Exception as e:  # noqa: BLE001  (offline, disk, Hugging Face down)
            typer.echo(f"warning  model download failed ({e}); recall uses keyword search "
                       "until `keepfold model download` works.")
    else:
        typer.echo("skipped  model (run `keepfold model download` later)")

    # 6. Claude Code
    if no_code:
        typer.echo("skipped  Claude Code plugin")
    elif doctor_mod.plugin_enabled():
        typer.echo("skipped  Claude Code plugin (enabled)")
    else:
        _setup_code(ask)

    # 7. Claude desktop
    if not no_desktop:
        _setup_desktop(configs, entry, ask)

    # 8. verify
    typer.echo("\nChecks:")
    try:
        doctor(vault=root)
    except typer.Exit:
        typer.echo("Fix the FAIL lines above, then rerun `keepfold setup`.")
    typer.echo("\nTry these in Claude: " + " | ".join(f'"{p}"' for p in FIRST_PROMPTS))
    typer.echo(PRIVACY)
    if not yes and typer.confirm("Open the Keepfold app for other AI apps, a local model and "
                                 "browsing?", default=True):
        app_cmd(vault=root)


def _setup_code(ask) -> None:
    src = _marketplace()
    if src is None:
        typer.echo(NO_MARKETPLACE)
        return
    cmds = [["plugin", "marketplace", "add", src], ["plugin", "install", "keepfold@keepfold"]]
    claude = shutil.which("claude")
    if src == REPO and session_mod.git_exe() is None:  # Claude Code clones the repo with git
        from keepfold.app.wizard import GIT_PAGE
        typer.echo("Claude Code needs Git to download the Keepfold plugin from GitHub. Install Git "
                   f"from {GIT_PAGE}, open a new terminal, then run `keepfold setup` again.")
        return
    # runs `claude` itself only once a published repo is chosen; a checkout path is printed
    if claude and "<" not in REPO and ask("Install the Claude Code plugin with `claude`?"):
        for c in cmds:
            r = subprocess.run([claude, *c], capture_output=True, text=True,
                               creationflags=NO_WINDOW)
            ok = r.returncode == 0
            typer.echo(f"{'ran     ' if ok else 'warning '} claude {' '.join(c)}"
                       + ("" if ok else f": {(r.stderr or r.stdout).strip()[:200]}"))
        typer.echo("Restart Claude Code, then type /keepfold.")
        return
    typer.echo("In Claude Code, type these two commands, then restart it:")
    for c in cmds:
        typer.echo(f"  /{' '.join(c)}")


def _desktop_change(p: Path, entry: dict, dry_run: bool = False) -> str:
    """Set mcpServers.keepfold in `p` through the app's writer (timestamped backups); returns
    "created", "updated" or "unchanged". ValueError leaves a file it cannot parse untouched."""
    from keepfold.app import targets

    existed = p.is_file()
    if targets.edit_json(p, "mcpServers", entry, dry_run=dry_run) == "none":
        return "unchanged"
    return "updated" if existed else "created"


def _setup_desktop(configs: list[Path], entry: dict, ask) -> None:
    from keepfold.app import targets

    if not configs:
        typer.echo("Claude desktop: no config folder found. Open Settings > Developer > Edit "
                   "Config and add this under \"mcpServers\":")
        typer.echo(json.dumps({"keepfold": entry}, indent=2))
    for p in configs:
        if not ask(f"Add Keepfold to the Claude desktop config {p}?"):
            typer.echo(f"skipped  {p}")
            continue
        try:
            status = _desktop_change(p, entry)
        except ValueError as e:
            typer.echo(f"warning  {e} Or add this under \"mcpServers\" yourself:")
            typer.echo(json.dumps({"keepfold": entry}, indent=2))
            continue
        typer.echo(f"skipped  {p} (keepfold entry exists)" if status == "unchanged"
                   else f"{status:<8} {p} (backup beside it: {p.name}.keepfold-<time>.bak)"
                   if status == "updated"
                   else f"created  {p}")
    src = _marketplace()
    zip_from = (f"{CHECKOUT / 'dist'} (build it with `uv run python scripts/build_skills.py` "
                f"in {CHECKOUT})" if src == str(CHECKOUT)
                else f"{RELEASES}/latest/download/keepfold-skill.zip")
    typer.echo("Fully quit Claude desktop (tray or menu bar > Quit) and open it again. For "
               f"/keepfold in Chat, upload keepfold-skill.zip from {zip_from} under Settings > "
               "Capabilities > Skills.")
    _paste_text("Chat has no hooks, so Claude uses Keepfold in every chat only when its "
                "instructions say so. " + targets.PASTE_DESKTOP)


def _paste_text(where: str) -> None:
    """Print packs.USE_TEXT (the app's Copy instructions) with where to paste it."""
    from keepfold import packs
    typer.echo(f"\n{where} The text:\n")
    typer.echo(packs.USE_TEXT)


@app.command(hidden=True)  # the uninstaller's step; people use Connect AI apps in the app
def disconnect() -> None:
    """Remove Keepfold from every AI app it is connected to, as Connect AI apps does (backups
    beside each file). The Windows uninstaller runs it when asked."""
    from keepfold.app import targets

    rows = targets.apply_all({t.id: False for t in targets.targets()}, None, typer.echo)
    for r in rows:
        typer.echo(f"{r['change']:<8} {r['label']}: {r.get('file') or ''} {r.get('detail') or ''}"
                   .rstrip())
    if not rows:
        typer.echo("No AI app had Keepfold.")
    if any(r["change"] == "error" for r in rows):
        raise typer.Exit(1)


def uninstall_steps(say) -> bool:
    """`keepfold uninstall`'s steps, shared with the app's Uninstall Keepfold… (server
    app_uninstall): disconnect every AI app, turn off both login entries, delete the
    ~/.local/bin link. The rest still runs after a failed step; True when one failed."""
    import types

    from keepfold import update
    from keepfold.app import actions, desktop

    failed = False
    try:
        disconnect()
    except typer.Exit:  # an AI app's file could not be changed: the rest still runs
        failed = True
    actions.login(types.SimpleNamespace(say=say), None, False)  # the page's switch, off
    if update.mode() == "app" and sys.platform == "darwin":  # SMAppService, not a file
        try:
            desktop.set_autostart(False)
            say("The Keepfold app no longer starts at login.")
        except ValueError as e:
            say(str(e))
            failed = True
    elif (f := desktop.autostart_file()) is not None:
        if f.exists():
            f.unlink()
            say(f"removed {f}")
    elif sys.platform == "win32":
        desktop.set_autostart(False)
        say("The Keepfold app no longer starts with Windows.")
    link = Path.home() / ".local" / "bin" / "keepfold"
    if link.is_symlink():  # a real file there is not ours to delete
        link.unlink()
        say(f"removed {link}")
    return failed


@app.command()
def uninstall(yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask first.")] = False,
              ) -> None:
    """Take Keepfold out of your AI apps and stop it starting at login. Your vault and
    ~/.keepfold are kept; the last line says how to remove the program itself."""
    from keepfold import update

    link = Path.home() / ".local" / "bin" / "keepfold"
    if not yes:
        typer.echo("This will:\n  1. Remove Keepfold from every AI app it is connected to "
                   "(backups beside each file).\n  2. Stop the browsing receiver starting at "
                   "login.\n  3. Stop the Keepfold app starting at login.\n  4. Delete the "
                   f"{link} link, if it is one.\nYour vault and ~/.keepfold are kept.")
        if not typer.confirm("Go ahead?"):
            typer.echo("Nothing changed.")
            return
    failed = uninstall_steps(typer.echo)
    mode = update.mode()
    typer.echo("Move Keepfold to the Trash from Applications." if mode == "app"
               and sys.platform == "darwin"
               else "Remove the app: sudo apt remove keepfold" if mode == "app"
               else "Use Settings > Apps > Keepfold." if mode == "installer"
               else "Remove the program: uv tool uninstall keepfold")
    typer.echo("Your vault and ~/.keepfold are kept; delete them yourself if you want them gone.")
    if failed:
        raise typer.Exit(1)


CAPTURE_WAIT_S = 60  # a cold start of the app: its server, then its windows


@app.command(hidden=True)  # what GNOME's quick capture shortcut runs
def capture() -> None:
    """Show the Keepfold app's quick capture box, starting the app first when none runs."""
    from keepfold.app import bundle, server
    if url := server.running_url():
        if server.show_capture(url):
            return
        typer.echo("Quick capture comes with the Keepfold app's window; this Keepfold runs in "
                   "the browser.", err=True)
        raise typer.Exit(1)
    bundle.start_app()
    end = time.monotonic() + CAPTURE_WAIT_S
    while time.monotonic() < end:  # the box exists once the app's windows do
        time.sleep(0.5)
        if (url := server.running_url()) and server.show_capture(url):
            return
    typer.echo("The Keepfold app did not start in time.", err=True)
    raise typer.Exit(1)


@app.command(hidden=True)  # the frozen apps' smoke test (release-apps.yml)
def selftest() -> None:
    """Import every Keepfold module and read every package data file; exit 1 on the first
    failure. Catches what a PyInstaller build missed."""
    from keepfold import selftest as st

    try:
        typer.echo(st.run())
    except st.Failed as e:
        typer.echo(f"selftest failed: {e}", err=True)
        raise typer.Exit(1) from None


@app.command("_parse-child", hidden=True)  # the frozen apps' PDF/.docx parse child (RR-37)
def parse_child() -> None:
    """Parse documents from stdin in a child process; see more_sources._child_argv."""
    from keepfold import more_sources

    more_sources._parse_child()


sample_app = typer.Typer(no_args_is_help=True, help="The sample vault (a made-up learner).")
app.add_typer(sample_app, name="sample")


@sample_app.command("remove")
@_errors
def sample_remove(vault: VaultOpt = None) -> None:
    """Delete the `sample: true` pages and void the sample events (history stays)."""
    from keepfold import sample as sample_mod

    v = _vault(vault)
    removed, n = sample_mod.remove(v)
    for rel in removed:
        typer.echo(f"removed  {rel}")
    for rel, text in ME_PAGES.items():  # put back the empty templates Sam's pages replaced
        if not (v.root / rel).exists():
            v.write_text(v.root / rel, text)
            typer.echo(f"created  {rel}")
    typer.echo(f"voided   {n} sample events")


@app.command("lint")
@_errors
def lint_cmd(findability: Annotated[bool, typer.Option(
        "--findability", help="Also check that each long page's own deep text finds it "
        "(top 3, no rerank; keyword only without the model). Report only.")] = False,
        vault: VaultOpt = None) -> None:
    """List vault problems to fix and add a line to log.md."""
    from keepfold import lint as lint_mod

    v = _vault(vault)
    findings, totals = lint_mod.lint_report(v)
    for f in findings:
        typer.echo(render.term(f"{f['kind']}: {f['id']} - {f['detail']}"))
    more = lint_mod.not_shown(findings, totals)
    typer.echo(f"{len(findings)} findings" + (f" (not shown: {more})" if more else ""))
    if findability:
        from keepfold import semantic

        embed = semantic.load_embedder()  # never downloads; None: keyword only
        lost = lint_mod.findability(v, embed)
        for f in lost:
            typer.echo(render.term(f"{f['kind']}: {f['id']} - {f['detail']}"))
        typer.echo(f"{len(lost)} pages not findable by their own text"
                   + ("" if embed else " (keyword only: no model)"))
    v.append_log(local_day(datetime.now(UTC)), "lint", f"{len(findings)} findings")


@app.command("agent-text")
@_errors
def agent_text_cmd(vault: VaultOpt = None) -> None:
    """Print every fixed string an agent can see (server instructions, tool descriptions, next
    hints, SKILL.md, hook templates, the rule-file block), then its size per app profile."""
    from keepfold import agent_text, profiles

    srcs = agent_text.sources()
    for label, channel, text in srcs:
        typer.echo(f"== {label} [{channel}] ({len(text)} chars) ==\n{text}\n")
    try:  # the owner's profiles when a vault is set; DEFAULT otherwise (no vault text is read)
        data = profiles.load(_vault(vault))
    except VaultError:
        data = profiles.DEFAULT
    typer.echo("== chars per app profile ==")
    for name, n in agent_text.sizes(data, srcs):
        typer.echo(f"{name}: {n} ({', '.join(sorted(agent_text.channels(name)))})")


@app.command("relink")
@_errors
def relink_cmd(dry_run: Annotated[bool, typer.Option(
        "--dry-run", help="Report what would change; write nothing.")] = False,
        vault: VaultOpt = None) -> None:
    r"""Add the [\[links]] Obsidian's graph needs (session -> day -> month -> topic map, the
    people hub, frontmatter edges as "[\[id]]"), fix near-miss links, escape junk ones, and
    report unresolved links."""  # \[ keeps Rich markup in --help from eating [links]
    from keepfold import relink

    out = relink.run(_vault(vault), dry_run=dry_run)
    for line in relink.report_lines(out):
        typer.echo(line)
    typer.echo("dry run: nothing written" if dry_run else "written")


@app.command("rollups")
@_errors
def rollups_cmd(since: Annotated[str | None, typer.Option(
        help="Only periods ending in or after this month (YYYY-MM).")] = None,
        vault: VaultOpt = None) -> None:
    """Write the week, month and year rollup pages (rollups/): counts and top items per source,
    built by code, no model. Only pages whose text changed are written."""
    from keepfold import rollups

    out = rollups.run(_vault(vault), since)
    typer.echo("rollups: nothing changed" if out is None else
               f"rollups: {out['written']} written, {out['kept']} unchanged, "
               f"{out['removed']} removed")


@app.command("reflect")
@_errors
def reflect_cmd(vault: VaultOpt = None) -> None:
    """Show the reflect pack: unconfirmed lines, emerging clusters, old fleeting lines, week."""
    from keepfold import reflect as reflect_mod

    v = _vault(vault)
    out = reflect_mod.reflect(v, Catalog.load(v), datetime.now(UTC))
    typer.echo(f"Unconfirmed about-me lines: {len(out['unconfirmed'])}"
               + (f" ({out['unconfirmed_dropped']} older than 60 days not shown)"
                  if out["unconfirmed_dropped"] else ""))
    for u in out["unconfirmed"]:
        typer.echo(render.term(f"  - {u['text']}"
                               + (f" (since {u['since']})" if u["since"] else "")))
    typer.echo(f"Emerging clusters: {len(out['emerging'])}")
    for c in out["emerging"]:
        typer.echo(render.term(f"  - {c['suggested_title']}: {', '.join(c['members'])} "
                               f"(shared: {', '.join(c['shared'])})"))
    typer.echo(f"Old fleeting lines: {len(out['old_fleeting'])}")
    for f in out["old_fleeting"]:
        typer.echo(render.term(f"  - {f['text']} (since {f['since']})"))
    w = out["week"]
    typer.echo(f"Week {w['from']} to {w['to']}: {w['sessions']} sessions, "
               f"{w['correct']}/{w['grades']} correct, {len(w['changed'])} concepts changed")


@app.command("pack")
@_errors
def pack_cmd(name: Annotated[str, typer.Argument(
                 help="general, coding, writing, interview, research or review")],
             chars: Annotated[int, typer.Option(
                 "--chars", help="the most characters: 1500 or 5000")] = 1500,
             vault: VaultOpt = None) -> None:
    """Print a context pack to paste into a web chat (ChatGPT, Gemini): your pinned core, then
    the pack's preference lines, at most 1,500 (or 5,000) characters, never a private page."""
    from keepfold import packs

    if name not in packs.PACKS:
        typer.echo(f"error: the pack is one of {', '.join(packs.PACKS)}.", err=True)
        raise typer.Exit(2)
    if chars not in packs.SIZES:
        typer.echo("error: --chars is 1500 or 5000.", err=True)
        raise typer.Exit(2)
    text = packs.pack(_vault(vault), name, chars)
    if not text:
        typer.echo("Nothing to pack yet: add lines to me/core.md or me/preferences.md.",
                   err=True)
        raise typer.Exit(1)
    typer.echo(render.term(text))


chats_app = typer.Typer(invoke_without_command=True)
app.add_typer(chats_app, name="chats")


@chats_app.callback()
@_errors
def chats_cmd(ctx: typer.Context,
              since: Annotated[str | None, typer.Option(help="Only chats updated on or after "
                                                         "this date (YYYY-MM-DD).")] = None,
              limit: Annotated[int, typer.Option(help="Show at most this many.")] = 20,
              vault: VaultOpt = None) -> None:
    r"""List past chats (Claude Code, and Claude Chat once imported), newest first; \[saved]
    marks ones already saved. `keepfold chats archive` moves chat pages out of git."""  # \[ keeps
    # Rich markup in --help from eating [saved]
    from keepfold import chats

    if ctx.invoked_subcommand:
        return
    v = _vault(vault)
    saved = {c for _, c in chats.saved_ids(EventLog(v).iter_events())}
    for c in chats.list_chats(saved, since, limit):
        mark = " [saved]" if c["saved"] else ""
        typer.echo(render.term(f"{c['updated'][:16]}  {c['id']}  {c['turns']} turns{mark}  "
                               f"{c['first_user_line']}"))


@chats_app.command("archive")
@_errors
def chats_archive_cmd(
        dry_run: Annotated[bool, typer.Option(
            "--dry-run", help="List the pages that would move; change nothing.")] = False,
        undo: Annotated[bool, typer.Option(
            "--undo", help="Instead: move them back to resources/ and keep new chat pages "
                           "there.")] = False,
        vault: VaultOpt = None) -> None:
    """Move imported AI chat pages from resources/ into archive/chats/, which git ignores: they
    stay searchable, links keep working, and they leave the vault's git (old commits keep
    them). New chat pages go there too. --undo moves them back."""
    from keepfold import chat_archive

    v = _vault(vault)
    where, to = (CHAT_ARCHIVE, "resources") if undo else ("resources", CHAT_ARCHIVE)
    r = chat_archive.move(v, undo=undo, dry_run=dry_run)
    n = len(r["ids"])
    for i in r["ids"] if dry_run else ():
        typer.echo(f"  {i}")
    typer.echo(f"{'Would move' if dry_run else 'Moved'} {n} chat page{'s' * (n != 1)} from "
               f"{where}/ to {to}/" + (" (dry run: nothing changed)." if dry_run else "."))
    if r["kept"]:
        typer.echo(f"Left {len(r['kept'])} where they are (another page of that id is in "
                   f"{to}/, or the file is open): {', '.join(r['kept'][:10])}")
    if r["git"]:
        typer.echo(r["git"])
    elif not dry_run and not (v.root / ".git").exists():
        typer.echo("The vault is not a git repo: no git step.")
    if not dry_run:
        typer.echo("New chat pages go to resources/ until you run `keepfold chats archive` again."
                   if undo else "New chat pages go to archive/chats/; `--undo` reverses this.")


@app.command("import-chats")
@_errors
def import_chats_cmd(export: Annotated[Path | None, typer.Argument(
        help="The export: a claude.ai or ChatGPT .zip, a Google Takeout .zip of Gemini Apps "
             "activity (JSON or HTML), or Perplexity thread .md files (one, a folder or a .zip).")]
        = None,
        source: Annotated[str | None, typer.Option(
            help="Force the service when detection fails: claude, chatgpt, gemini or "
                 "perplexity.")] = None,
        auto: Annotated[bool, typer.Option(
            "--auto", help="Instead: write a chat page for each Claude Code chat and imported "
                           "conversation not saved yet or changed since (what the app does "
                           "every 5 minutes); no model.")] = False,
        vault: VaultOpt = None) -> None:
    """Import conversations from a claude.ai, ChatGPT, Gemini or Perplexity export so /keepfold can
    learn from them like Claude Code chats. The service is detected from the file. Secrets are
    redacted; re-importing a newer export updates them. With --auto, save past chats as chat
    pages instead: your own turns only (a coding session as a short summary)."""
    from keepfold import chats, importers

    if auto:
        from keepfold import chat_import
        counts = chat_import.run(_vault(vault), progress=lambda k, n, s: typer.echo(
            chat_import.eta(k, n, s)) if k == n or k % 25 == 0 else None)
        wrote = counts.get("saved", 0) + counts.get("updated", 0)
        said = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(counts.items()))
        typer.echo(f"{wrote} chat page{'s' * (wrote != 1)} written"
                   + (f" ({said})." if counts else "; no new or changed chats."))
        return
    if export is None:
        raise VaultError("invalid_argument", "Give the export to import, or --auto.")
    got = chats.import_export(export, source)
    source, n = got
    typer.echo(f"Imported {n} {chats.GUIDES[source]['name']} conversation{'s' * (n != 1)} "
               f"into {chats.export_root()}. Run /keepfold to learn from them.")
    typer.echo(importers.report_line(chats.GUIDES[source]["name"], got.report()))


@app.command("safety-net")
@_errors
def safety_net_cmd(
        dry_run: Annotated[bool, typer.Option(
            "--dry-run", help="Print what would be recorded; write nothing.")] = False,
        undo: Annotated[bool, typer.Option(
            "--undo", help="Instead: void every event the safety net recorded (with --chat, "
                           "only that chat's).")] = False,
        chat: Annotated[str | None, typer.Option(
            help="With --undo: the chat page id whose events to void.")] = None,
        vault: VaultOpt = None) -> None:
    """Record what the chat model forgot: topic touches for concepts and topics named in your
    recent chat pages, and (with a local model already loaded) the concepts a chat explained,
    as taught. Private chats and hidden subjects are skipped; what a chat model logged is not
    repeated. The app runs it after each chat import."""
    from keepfold import safety_net

    v = _vault(vault)
    if undo:
        n = safety_net.undo(v, chat)
        typer.echo(f"voided {n} safety-net event{'s' * (n != 1)}")
        return
    runner, embed = safety_net.cli_tools()
    out = safety_net.run(v, runner, embed, dry_run=dry_run)
    if runner is None:
        typer.echo("No local model is loaded: only topic touches (load the small model to "
                   "also record taught concepts).")
    said = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(out.items())
                     if isinstance(n, int))
    dry = " [dry run: nothing written]" if dry_run else ""
    typer.echo((out.get("line") or "Safety net: nothing new to record")
               + (f" ({said})" if said else "") + dry)


@app.command("fact-harvest")
@_errors
def fact_harvest_cmd(vault: VaultOpt = None) -> None:
    """Save facts you stated about yourself in your recent chat pages (where you live, work or
    study, what you work on, tools, goals, preferences) as about-me guesses, with a local model
    already loaded. Your own words only; private chats and hidden subjects are skipped. The app
    runs it after each chat import; `keepfold undo <session>` takes a pass back."""
    from keepfold import fact_harvest

    v = _vault(vault)
    runner = fact_harvest.cli_runner()
    if runner is None:
        typer.echo("No local model is loaded: load the small model, then run this again.")
        return
    out = fact_harvest.run(v, runner)
    said = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(out.items())
                     if isinstance(n, int))
    typer.echo(f"Fact harvest: {said or 'nothing new'}"
               + (f" (undo: keepfold undo {out['session']})" if out.get("session") else ""))


@app.command("digest")
@_errors
def digest_cmd(yes: Annotated[bool, typer.Option(
        "--yes", help="Confirm every topic shown without asking.")] = False,
        dry_run: Annotated[bool, typer.Option(
            "--dry-run", help="List only; write nothing and keep the chats for next time.")]
        = False,
        since: Annotated[str | None, typer.Option(
            help="Only chats updated on or after this date (YYYY-MM-DD).")] = None,
        limit: Annotated[int, typer.Option(min=1, max=50, help="Chats per digest.")] = 10,
        vault: VaultOpt = None) -> None:
    """Review chats imported since your last digest: the topics each one seems to be about.
    Each topic you confirm records a topic touch (context, never a grade); the chats listed are
    not shown again. Runs only when you ask; local search only, no model."""
    from datetime import date

    from keepfold import digest, harvest, semantic
    from keepfold.index import Index

    if since is not None:
        try:
            date.fromisoformat(since)
        except ValueError:
            raise VaultError("invalid_argument", "--since must be a date like 2026-11-13.") \
                from None
    v = _vault(vault)
    events = list(EventLog(v).iter_events())
    rows = digest.new_chats(digest.reviewed(events), limit, since)
    if not rows:
        typer.echo("No new imported chats since your last digest.")
        return
    idx, embed = Index(v, Catalog.load(v)), semantic.load_embedder()
    if embed is None:
        typer.echo(semantic.NOT_SET_UP, err=True)
    quiet = harvest.quiet(events)
    confirmed: list[tuple[str, str]] = []
    done: set[str] = set()  # a topic is asked about once per digest
    typer.echo(f"Since your last digest you discussed (from {len(rows)} chat"
               f"{'s' * (len(rows) != 1)}):")
    for r in rows:
        found = [c for c in digest.concepts_for(idx, r["query"], embed, quiet)
                 if c["id"] not in done]
        typer.echo(render.term(f"\n{r['updated'][:10]}  {r['source']}  {r['title']}"))
        if not found:
            typer.echo("  (no matching topic)")
        for c in found:
            line = render.term(f"  {c['title']} ({c['id']})")
            if dry_run:
                typer.echo(line)
            elif yes or typer.confirm(f"{line}: right?", default=False):
                confirmed.append((r["id"], c["id"]))
                done.add(c["id"])
                if yes:
                    typer.echo(line)
    if dry_run:
        typer.echo("\n(dry run: nothing written)")
        return
    digest.record(v, confirmed, [r["id"] for r in rows])
    typer.echo(f"\nRecorded {len(confirmed)} topic touch{'es' * (len(confirmed) != 1)} "
               "(context only; a graded check is what moves a topic's level).")


@app.command("import")
@_errors
def import_cmd(path: Annotated[Path | None, typer.Argument(
        help="A chat export (claude.ai, ChatGPT, Gemini Takeout, Perplexity .md), a Google "
             "Takeout of My Activity, a notes folder or .zip (Obsidian, Notion, .md/.txt), "
             "a calendar .ics (or Google's .zip of them), a folder of documents (.pdf, "
             ".docx), a folder of your git projects, reading highlights (Kindle "
             "My Clippings.txt, a Readwise or Pocket .csv), sent mail (.mbox), a Google "
             "Timeline (location history), a photo folder, a Spotify data export, a "
             "folder of voice memos (transcribed locally), Zotero's data folder or Anki "
             "(an .apkg/.colpkg export or your Anki profile folder). With "
             "--source zotero it may be left out: Keepfold finds your Zotero library.")] = None,
        source: Annotated[str | None, typer.Option(
            help="Force the kind: claude, chatgpt, gemini, perplexity, google-activity, "
                 "obsidian, notion, notes, calendar, docs, repos, highlights, mail, "
                 "selfchat (a WhatsApp chat with yourself, Telegram Saved Messages), "
                 "location (Google Timeline), photos (a photo folder), spotify, "
                 "voice-memos (a folder of recordings), zotero (a Zotero library) or anki "
                 "(Anki decks).")] = None,
        dry_run: Annotated[bool, typer.Option(
            "--dry-run", help="Print counts only; write nothing.")] = False,
        private: Annotated[bool, typer.Option(
            "--private", help="The same as --tier private.")] = False,
        tier: Annotated[str | None, typer.Option(
            help="Who may read the imported pages (every import but chats and Google "
                 "activity): private (local models "
                 "only), normal (the AI apps you connect too) or skip (import nothing). "
                 "Without it a terminal asks once per source; the choice is remembered. "
                 "Refused for chats and Google activity.")] = None,
        vault: VaultOpt = None) -> None:
    """Import anything about you; the kind is detected from the file. Re-importing skips notes
    and events already in and updates chats. `import-chats` still imports chat exports only."""
    from keepfold import chats, importers, more_sources, sources

    if source is not None and source not in importers.SOURCES:
        raise VaultError("invalid_argument",
                         f"--source must be one of {', '.join(importers.SOURCES)}.")
    if tier is not None and tier not in sources.WORDS:
        raise VaultError("invalid_argument", f"--tier must be one of {', '.join(sources.WORDS)}.")
    if private and tier not in (None, "private"):
        raise VaultError("invalid_argument", "--private is --tier private; give only one.")
    given = tier or ("private" if private else None)
    if path is None and source != "zotero":
        raise VaultError("invalid_argument", "Give the file or folder to import (only --source "
                         "zotero finds its library by itself).")
    kinds = [importers.SOURCES[source]] if source else importers.detect(path)
    if not kinds:
        raise VaultError("invalid_argument", f"Cannot tell what {path.name} is; add --source "
                         f"{'|'.join(importers.SOURCES)}.")
    if given and {"chats", "takeout"} & set(kinds):  # G5: these have no private mode
        from keepfold.app.actions import TIERED  # the imports that take a tier
        flag = "--private" if private else "--tier"
        *some, last = ["calendars" if k == "calendar" else k for k in TIERED]
        raise VaultError("invalid_argument", f"{flag} applies to {', '.join(some)} and {last} "
                         "only. "
                         "Chats are always readable by /keepfold; Google activity follows your "
                         f"Browsing settings. Run again without {flag}.")
    dry = " (dry run: nothing written)" if dry_run else ""
    for kind in kinds:
        if kind == "chats":
            got = chats.import_export(path, source if source in chats.EXPORT_DIRS else None,
                                      dry_run)
            name = chats.GUIDES[got[0]]["name"]
            typer.echo(f"{name}: {got[1]} new conversations, {got[2]} already imported{dry}"
                       if dry_run else f"{name}: {got[1]} conversations imported into "
                       f"{chats.export_root()}. Run /keepfold to learn from them.")
            typer.echo(importers.report_line(name, got.report()))
            continue
        if kind == "takeout":  # also reads the browsers, as `browse import --takeout` does
            browse_import(since=None, browser=None, profile=None, dry_run=dry_run,
                          takeout=path, vault=vault)
            continue
        v, flavor = _vault(vault), source if source in importers.LABELS else None
        src = importers.notes_source(path, flavor) if kind == "notes" else \
            importers.calendar_source(path) if kind == "calendar" else \
            more_sources.SOURCE_OF[kind](path)
        # C2: asked once per source in a real terminal, then remembered (sources.TIERS_FILE)
        ask = _ask_tier if given is None and _tty() else None
        told: list[str] = []  # RR-29: what a stricter tier did to the pages already in
        t, how = sources.pick_tier(v, src, given, ask, save=not dry_run, said=told.append)
        typer.echo(f"{src.label}: tier {sources.word(t)} (" + (
            "--tier" if given else "your answer" if how == "chosen"
            else f"{how}; change it with --tier") + ")")
        for line in told:
            typer.echo(line)
        if t is sources.Audience.SKIP:
            typer.echo(f"{src.label}: skip, so nothing was imported.")
        elif kind == "notes":
            r = importers.import_notes(v, path, flavor, dry_run, tier=t)
            typer.echo(f"{importers.LABELS[r['source']]}: {r['new']} new notes, {r['same']} "
                       f"already imported, {r['skipped']} skipped (hidden, not text or over "
                       f"5 MB){dry}")
            typer.echo(importers.report_line(importers.LABELS[r["source"]], r))
        elif kind in more_sources.SOURCE_OF:
            typer.echo(importers.report_line(src.label, sources.run(v, src, t, dry_run)) + dry)
            if kind == "anki" and not dry_run:  # parent decks and concepts, once pages exist
                from keepfold import anki
                anki.link(v)
        else:
            r = importers.import_calendar(v, path, dry_run, tier=t)
            typer.echo(f"calendar: {r['events']} events in {r['months']} months, {r['new']} "
                       f"new events{dry}")
            typer.echo(importers.report_line("calendar", r))


watch_app = typer.Typer(no_args_is_help=True, help="Folders (and Zotero) that come in by "
                        "themselves: the app reads their new and changed files every 10 "
                        "minutes while it runs.")
app.add_typer(watch_app, name="watch")


@watch_app.command("add")
@_errors
def watch_add_cmd(path: Annotated[Path | None, typer.Argument(
        help="The folder to watch (its full path); for --source zotero, Zotero's data folder, "
             "or leave it out to find your library.")] = None,
        source: Annotated[str, typer.Option(
            help="What it holds: docs (PDFs, Word, text), notes (.md/.txt), repos (your git "
                 "projects), calendar (.ics files; or give one .ics file) or zotero.")] = "docs",
        tier: Annotated[str | None, typer.Option(
            help="Who may read its pages: private, normal or skip. Without it, the source's "
                 "last choice (or normal).")] = None,
        vault: VaultOpt = None) -> None:
    """Watch a folder: the Keepfold app imports its new and changed files by itself."""
    from keepfold import sources, watch

    if tier is not None and tier not in sources.WORDS:
        raise VaultError("invalid_argument", f"--tier must be one of {', '.join(sources.WORDS)}.")
    told: list[str] = []
    w = watch.add(_vault(vault), path, source, tier, said=told.append)
    typer.echo(render.term(f"Watching {w['path']} as {w['source']}, tier "
                           f"{sources.word(sources.Audience(w['tier']))}. The Keepfold app reads "
                           "its new and changed files every 10 minutes while it runs."))
    for line in told:
        typer.echo(line)


@watch_app.command("list")
@_errors
def watch_list_cmd(vault: VaultOpt = None) -> None:
    """The watched folders, their source and tier, and what their last pass did."""
    from keepfold import sources, watch

    rows = watch.listing(_vault(vault))["rows"]
    if not rows:
        typer.echo("No folder is watched; add one with `keepfold watch add <folder>`.")
    for w in rows:
        last = w.get("last") or {}
        typer.echo(render.term(f"{w['path']}  {w['source']}  "
                               f"{sources.word(sources.Audience(w['tier']))}"
                               + ("  (not found)" if w["missing"] else "")))
        typer.echo(render.term(f"  last {last['at']}: {last['line']}" if last.get("line")
                               else "  not read yet"))


@watch_app.command("remove")
@_errors
def watch_remove_cmd(path: Annotated[Path, typer.Argument(help="A watched folder.")],
                     vault: VaultOpt = None) -> None:
    """Stop watching a folder; the pages it brought stay."""
    from keepfold import watch

    if not watch.remove(_vault(vault), path):
        raise VaultError("invalid_argument", f"{path} is not watched; see `keepfold watch list`.")
    typer.echo(render.term(f"Stopped watching {path}; its pages stay."))


def _ask_tier(src) -> str:
    """`keepfold import`'s tier question, with the source's default; asked again until the
    answer is one of the three words."""
    from keepfold import sources
    while (a := typer.prompt(f"{src.label}: who may read these pages? private (local models "
                             "only), normal (the AI apps you connect too) or skip (import "
                             "nothing)", default=sources.word(src.default_tier))
           .strip().lower()) not in sources.WORDS:
        typer.echo("Type private, normal or skip.")
    return a


@app.command("claims")
@_errors
def claims_cmd(tier: Annotated[str | None, typer.Option(
        help="Only this tier: confirmed, agent, exposed, typed or legacy.")] = None,
        unasked: Annotated[bool, typer.Option(
            "--unasked", help="What was saved about you without your say, with undo steps.")]
        = False,
        summary: Annotated[bool, typer.Option(
            "--summary", help="How well Keepfold knows you, per me/ page.")]
        = False,
        days: Annotated[int, typer.Option(help="With --unasked: days of me/ changes.")] = 14,
        vault: VaultOpt = None) -> None:
    """List live claims on the me/ claim pages with their id and authorship tier.
    --unasked and --summary print the other two views instead."""
    from keepfold import claims

    if sum((tier is not None, unasked, summary)) > 1:
        raise VaultError("invalid_argument", "Use one of --tier, --unasked and --summary.")
    if unasked:
        return saved_cmd(days, vault)
    if summary:
        return know_cmd(vault)
    if tier is not None and tier not in claims.TIERS:
        raise VaultError("invalid_argument", f"--tier must be one of {', '.join(claims.TIERS)}.")
    _refuse_excluded("list claims")
    v = _vault(vault)
    log = EventLog(v)
    events = list(log.iter_events())
    sample = {n.id for n in v.iter_notes("me") if n.fields.get("sample") is True}
    for r in claims.tiers(v, events, log):
        if tier in (None, r["tier"]):
            typer.echo(f"^{r['id']}  {r['tier']:<9}  me/{r['page']} § "
                       f"{render.esc(r['section'], 40)}  {render.esc(r['text'], 80)}"
                       + ("  (sample)" if r["page"] in sample else ""))


@app.command("saved", hidden=True)  # now `claims --unasked`
@_errors
def saved_cmd(days: int = 14, vault: VaultOpt = None) -> None:
    """What was saved about you without your say: claim lines an assistant wrote unasked or
    from outside text, then recent me/ changes, each with its undo. Read-only."""
    from keepfold import claims

    _refuse_excluded("list saved lines")
    v = _vault(vault)
    out = claims.saved(v, EventLog(v).iter_events(), local_day(datetime.now(UTC)), days)
    typer.echo(f"Claim lines saved unasked: {len(out['claims'])}")
    for r in out["claims"]:
        label = {"exposed": "from outside text", "confirmed": "relayed yes"}.get(r["tier"],
                                                                             "unasked")
        typer.echo(f"  ^{r['id']}  {r['day'] or '-'}  {label}  me/{r['page']} § "
                   f"{render.esc(r['section'], 40)}  {render.esc(r['text'], 100)}")
        typer.echo(f"      undo: {r['undo']}")
    typer.echo(f"me/ changes in the last {days} days: {len(out['changes'])}"
               + (f" (latest {claims.SAVED_CHANGES})"
                  if len(out["changes"]) == claims.SAVED_CHANGES else ""))
    for c in out["changes"]:
        typer.echo(f"  {c['day']}  me/{render.esc(c['page'], 40)}  {c['op']} "
                   f"{render.esc(c['target'], 60)}"
                   + ("  (from outside text)" if c["exposed"] else ""))
        typer.echo(f"      undo: {c['undo']}")
    if out["claims"]:
        typer.echo("To keep an unasked line, say yes in a /keepfold reflect with no outside reads.")


@app.command("know", hidden=True)  # now `claims --summary`
@_errors
def know_cmd(vault: VaultOpt = None) -> None:
    """How well Keepfold knows you: per me/ page, live claims by tier. Read-only."""
    from keepfold import know

    def counts(d: dict) -> str:
        return ", ".join(f"{k} {n}" for k, n in d.items())

    _refuse_excluded("report")

    def split(p: dict) -> str:  # idea 11: who gave the confirmed lines their yes
        n = p["tiers"]["confirmed"]
        return (f" (confirmed: {p['clicked']} clicked, {p['reflected']} reflected, "
                f"{n - p['clicked'] - p['reflected']} relayed)") if n else ""

    v = _vault(vault)
    out = know.report(v, EventLog(v).iter_events(), local_day(datetime.now(UTC)))
    for p in out["pages"]:
        if p["private"]:
            typer.echo(f"me/{p['page']}: private")
            continue
        typer.echo(f"me/{p['page']}: {p['live']} live claims")
        typer.echo(f"  tiers: {counts(p['tiers'])}{split(p)}")
    t = out["total"]
    typer.echo(f"All claim pages: {t['live']} live claims")
    typer.echo(f"  tiers: {counts(t['tiers'])}{split(t)}")


voice_app = typer.Typer(help="Your own sent messages (voice samples).")
app.add_typer(voice_app, name="voice")


@voice_app.callback(invoke_without_command=True)
@_errors
def voice_cmd(ctx: typer.Context, vault: VaultOpt = None) -> None:
    """Count voice samples per audience and channel."""

    from keepfold import voice

    if ctx.invoked_subcommand is not None:
        return
    v = _vault(vault)
    counts = Counter((str(s.get("audience")), str(s.get("channel")))
                     for s in voice.rows(v, voice.SAMPLES))
    for (a, c), n in sorted(counts.items()):
        typer.echo(f"{render.esc(a, 60):<30} {render.esc(c, 10):<7} {n:>4}")
    if not counts:
        typer.echo("No voice samples yet; add your sent messages with `keepfold voice import`.")


@voice_app.command("import")
@_errors
def voice_import(file: Path,
                 audience: Annotated[str, typer.Option(help="A tag (professor) or people/<slug>.")],
                 act: Annotated[str, typer.Option(help="request|reply|thanks|apology|update|"
                                                       "decline|intro|other")],
                 channel: Annotated[str, typer.Option(help="email|chat|sms|social|ai|other")],
                 vault: VaultOpt = None) -> None:
    """Add your own sent messages, separated by lines of `---`, as voice samples."""
    from keepfold import voice

    v = _vault(vault)
    try:
        text = file.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise VaultError("invalid_argument", f"Cannot read {file}: {e}") from None
    n = voice.import_text(v, text, audience, act, channel, datetime.now(UTC))
    typer.echo(f"Imported {n} voice samples for {audience}.")


@voice_app.command("measure")
@_errors
def voice_measure(propose: Annotated[bool, typer.Option(
        "--propose", help="Save the lines to me/voice; each waits for your Approve.")] = False,
                  vault: VaultOpt = None) -> None:
    """Count how you write (sentence length, questions, greetings...) from your samples."""
    from keepfold import voice

    v = _vault(vault)
    m = voice.measure(v)
    if not m:
        typer.echo(f"Not enough voice samples yet (at least {voice.MIN_GROUP} per audience, "
                   "not counting ones typed to an AI).")
        return
    for g, nums in m.items():
        typer.echo(f"{render.esc(g, 60)}: " + ", ".join(
            f"{k} {render.esc(str(x), 40)}" for k, x in nums.items()))
        for line in voice.style_lines(m, g):
            typer.echo(f"  - {render.esc(line, 200)}")
    if propose:
        n = voice.propose(v, m)
        typer.echo(f"me/voice: {n.get('proposed', 0)} proposed, {n.get('kept', 0)} kept, "
                   f"{n.get('withdrawn', 0)} withdrawn; they wait for your Approve in the Keepfold "
                   "app (Review).")


# ---- brief ----------------------------------------------------------------------

FIRST_STEPS = ("To start: 1. add a test date (tell any Claude chat, or add one in assessments/); "
               "2. run /keepfold in Claude; 3. import your notes or chats with "
               "`keepfold import <file>`.")


def _today_line(it: dict) -> str:
    state = it["state"]
    head = f"{levels.GLYPH[state]} {levels.LABEL[state]}" if state else "- Plan step"
    # the id only when it says something the title doesn't ("Sorting", not "Sorting (sorting)")
    name = it["title"] if not it["id"] or page_slug(it["title"]) == it["id"] else \
        f"{it['title']} ({it['id']})"
    return f"{head}: {name} [{it['why']}]"


def _print_today(v: Vault, now: datetime, minutes: int | None = None) -> list[dict]:
    """Print Today (cut to `minutes` when given) and return the whole plan for Today.md."""
    catalog, day = Catalog.load(v), local_day(now)
    states, _ = planner.current_states(v, day, catalog)
    typer.echo("## Today")
    items = planner.today(v, catalog, states, now=now)
    shown = planner.fit(items, minutes) if minutes else items
    for it in shown:
        typer.echo(render.term(_today_line(it)))
    if minutes and shown:
        typer.echo(f"Time: {planner.budget_note(shown, minutes)}")
    if not items:  # "done" only after work today; a vault never graded gets first steps
        events = list(EventLog(v).iter_events())
        typer.echo("Nothing planned yet.")
        if not any(e.get("kind") == "grade" for e in events):
            typer.echo(FIRST_STEPS)
        elif any(e.get("day") == day for e in events):
            typer.echo("Done for today ✓")
    return items


@app.command()
@_errors
def brief(vault: VaultOpt = None) -> None:
    """Print BRIEF.md, the teaching contract verbatim, and Today."""
    _refuse_excluded("print the brief")
    v = _vault(vault)
    for rel in ("BRIEF.md", "me/teaching-contract.md"):
        p = v.root / rel
        if rel == "BRIEF.md" and not p.is_file():  # optional: only a vault you wrote one in
            continue
        text = p.read_bytes().decode("utf-8") if p.is_file() else f"({rel} is missing)\n"
        typer.echo(text.rstrip("\n") + "\n")
    _print_today(v, datetime.now(UTC))


@app.command()
@_errors
def today(minutes: Annotated[int | None, typer.Option(
               help=f"Print only what fits a session this long (1-{planner.MINUTES_MAX}; "
                    "estimates). Today.md keeps the whole plan.")] = None,
          vault: VaultOpt = None) -> None:
    """Print Today and rewrite Today.md."""
    v, now = _vault(vault), datetime.now(UTC)
    items = _print_today(v, now, planner.check_minutes(minutes))
    v.write_text(v.root / "Today.md", render.today_md(
        today=items, assessments=planner.assessments(v),
        as_of=local_day(now), cues=planner.last_questions(v),
        deadlines=planner.deadlines(v, local_day(now))))


@app.command()
@_errors
def dashboard(no_open: Annotated[bool, typer.Option(
                  "--no-open", help="Rewrite index.md only; do not open the app.")] = False,
              relayout: Annotated[bool, typer.Option(
                  "--relayout", help="Lay the brain out again from scratch.")] = False,
              vault: VaultOpt = None) -> None:
    """Rewrite index.md and open the dashboard: the Keepfold app's live home, starting the app
    as `keepfold app` does when it is not running. Export snapshot (the app's Vault page) saves
    an offline copy."""
    v, now = _vault(vault), datetime.now(UTC)
    if (old := dashboard_mod.retire_old(v)) is not None:
        typer.echo(f"Keepfold no longer writes .keepfold/dashboard.html; moved it to {old}",
                   err=True)
    if relayout:
        typer.echo("Laying the brain out again…", err=True)  # a big vault takes a while
        dashboard_mod.build(v, now=now, relayout=True)
    typer.echo(str(dashboard_mod.write_index(v, now=now)))
    if no_open:
        return
    from keepfold.app import server
    if url := server.running_url():  # the open app, not a second one
        if not server.show_window(url):  # the browser app has no window
            webbrowser.open(url)
        return
    server.run(v.root, echo=typer.echo)


@app.command()
@_errors
def void(event_id: str, reason: Annotated[str, typer.Option(help="Why it is cancelled.")],
         vault: VaultOpt = None) -> None:
    """Cancel one earlier event so it stops counting toward levels."""
    v = _vault(vault)
    void_event(EventLog(v), session="cli", event_id=event_id, reason=reason, source="external")
    typer.echo(f"voided {event_id}")


# ---- log ------------------------------------------------------------------------

def _problem(v: Vault, id: str) -> str:
    v.read("problem", id)  # raises unknown_id with a hint
    return id


@log_app.command("result")
@_errors
def log_result(concept: str, outcome: Annotated[str, typer.Argument(help="pass|partial|fail")],
               hints: Annotated[int, typer.Option(help="Hints you needed.")] = 0,
               confidence: Annotated[int | None, typer.Option(
                   help="How sure you were before checking, 1-4.")] = None,
               problem: Annotated[str | None, typer.Option(
                   help="The problem id it was about, if any.")] = None,
               vault: VaultOpt = None) -> None:
    """Log a graded result on a concept."""
    if outcome not in VERDICTS:
        raise VaultError("invalid_argument", "The outcome must be pass, partial or fail.")
    v = _vault(vault)
    obj = resolve_or_inbox(v, Catalog.load(v), concept, "keepfold log result")
    fields = {"object": obj, "verdict": VERDICTS[outcome], "hints": hints}
    if confidence is not None:
        fields["confidence"] = confidence
    if problem:
        fields["problem"] = f"problem:{_problem(v, problem)}"
    EventLog(v).append(session="cli", kind="grade", source="external", fields=fields)
    typer.echo(f"logged {outcome} on {obj}" + (" (added to inbox.md)" if
                                              obj.startswith("inbox:") else ""))


@log_app.command("solved")
@_errors
def log_solved(problem: str, hints: Annotated[int, typer.Option(help="Hints you needed.")] = 0,
               vault: VaultOpt = None) -> None:
    """Log a solved problem (a correct verdict on its primary concept)."""
    v = _vault(vault)
    _problem(v, problem)
    EventLog(v).append(session="cli", kind="log", source="external", fields={
        "object": f"problem:{problem}", "payload": {
            "kind": "problem_status", "problem": problem, "status": "solved", "hints": hints}})
    v.set_field("problem", problem, "status", "solved")  # the planner reads the note
    v.set_field("problem", problem, "solved_on", local_day(datetime.now(UTC)))
    typer.echo(f"logged solved on problem:{problem}")


@log_app.command("assessment")
@_errors
def log_assessment_cmd(id: str, outcome: Annotated[str, typer.Option(
                           help="pass|fail|pending|unknown")],
                       score: Annotated[str | None, typer.Option(
                           help="Your score as written, e.g. 7/10.")] = None,
                       company: Annotated[str | None, typer.Option(
                           help="Who set the test.")] = None,
                       kind: Annotated[str | None, typer.Option(
                           help="The kind of test, e.g. oa, interview, exam.")] = None,
                       date: Annotated[str | None, typer.Option(
                           help="The test's date (YYYY-MM-DD).")] = None,
                       weak: Annotated[str | None, typer.Option(help="a,b,c")] = None,
                       vault: VaultOpt = None) -> None:
    """Log an assessment result; creates the assessment note if it is missing."""
    v = _vault(vault)
    felt_weak = [w.strip() for w in (weak or "").split(",") if w.strip()]
    log_assessment(EventLog(v), session="cli", source="external", assessment=id,
                   outcome=outcome, score=score, company=company, kind=kind, date=date,
                   felt_weak=felt_weak)
    typer.echo(f"logged {outcome} on assessment:{id}")


@log_app.command("taught")
@_errors
def log_taught(concept: str, vault: VaultOpt = None) -> None:
    """Mark a concept as taught, not yet checked."""
    v = _vault(vault)
    obj = resolve_or_inbox(v, Catalog.load(v), concept, "keepfold log taught")
    EventLog(v).append(session="cli", kind="taught", source="external", fields={"object": obj})
    typer.echo(f"logged taught on {obj}" + (" (added to inbox.md)" if
                                           obj.startswith("inbox:") else ""))


# ---- concept --------------------------------------------------------------------

@concept_app.command("add")
@_errors
def concept_add(title: str,
                parent: Annotated[str | None, typer.Option(
                    help="The parent id (a domain for an area, an area for a concept).")] = None,
                kind: Annotated[str, typer.Option(help="domain|area|concept")] = "concept",
                alias: Annotated[list[str] | None, typer.Option(
                    help="Another name for it; repeat for more.")] = None,
                requires: Annotated[list[str] | None, typer.Option(
                    help="A concept id it builds on; repeat for more.")] = None,
                force: Annotated[bool, typer.Option(
                    help="Add it even when a similar concept exists.")] = False,
                vault: VaultOpt = None) -> None:
    """Add a domain, area or concept to the catalog and create its note."""
    v = _vault(vault)
    try:
        cat = Catalog.load(v)
        id = cat.add(kind, title, parent, alias or [], requires or [], force)
    except VaultError as e:
        if e.code == "duplicate_suggested":
            raise VaultError(e.code, f"{e} (pass --force)") from None
        raise
    typer.echo(f"{'adopted' if cat.adopted else 'added'} {kind} {id}")


@concept_app.command("alias")
@_errors
def concept_alias(id: str, alias: str, vault: VaultOpt = None) -> None:
    """Add an alias to a domain, area or concept."""
    Catalog.load(_vault(vault)).alias(id, alias)
    typer.echo(f"aliased '{alias}' -> {id}")


@concept_app.command("merge")
@_errors
def concept_merge(src: str, dst: str, vault: VaultOpt = None) -> None:
    """Fold concept SRC into DST and append a remap event."""
    v = _vault(vault)
    cat = Catalog.load(v)
    src, dst = cat.resolve(src), cat.resolve(dst)
    cat.merge(src, dst)
    EventLog(v).append(session="cli", kind="remap", source="external",
                       fields={"object": f"concept:{dst}", "ref": f"concept:{src}"})
    typer.echo(f"merged {src} into {dst}; remap logged")


@concept_app.command("duplicates")
@_errors
def concept_duplicates(limit: Annotated[int, typer.Option(help="pairs to show")] = 20,
                       vault: VaultOpt = None) -> None:
    """List concept pairs that probably mean the same thing, with evidence and the merge
    command for each. Read-only: it merges nothing."""
    from keepfold import duplicates, semantic

    v = _vault(vault)
    typer.echo(duplicates.report(*duplicates.find(v, semantic.load_embedder(), limit)))


# ---- replay, backup, undo, import -----------------------------------------------

def _why(st) -> str:
    """Short reason for a concept's new state, from the Solid criteria it misses."""
    if st is None:
        return "no events"
    if st.state == "solid":
        return "meets solid criteria"
    why = []
    if not st.depth_pass:
        why.append("no application/transfer pass")
    if (st.stability or 0) < levels.SOLID_STABILITY:
        # truncate, so 9.97 never prints as "10.0 < 10"
        why.append(f"stability {int((st.stability or 0) * 10) / 10:.1f} < "
                   f"{levels.SOLID_STABILITY:g}")
    if st.pass_days < levels.SOLID_PASS_DAYS:
        why.append(f"{st.pass_days} pass day{'s' * (st.pass_days != 1)} < {levels.SOLID_PASS_DAYS}")
    if st.solid_after:
        why.append(levels.gap_note(st))
    elif not st.gap_pass and st.pass_days:
        why.append(f"no pass {levels.SOLID_GAP_DAYS}+ days after the first pass "
                   f"with {levels.SOLID_REST_DAYS}+ days off before it")
    return "; ".join(why) or st.state


def _sync_lines(report: dict[str, list[str]]) -> list[str]:
    return [f"{what} {item}" for what, items in report.items() for item in items]


@catalog_app.command("sync")
@_errors
def catalog_sync_cmd(vault: VaultOpt = None) -> None:
    """Make catalog.yaml agree with domains/, areas/ and concepts/; never deletes."""
    typer.echo("\n".join(_sync_lines(catalog_sync(_vault(vault)))) or "catalog in sync")


@app.command()
@_errors
def replay(dry_run: Annotated[bool, typer.Option(
               help="Print a diff of what would change; write nothing.")] = False,
           all_: Annotated[bool, typer.Option(
               "--all", help="Show every changed line, not 25 per group.")] = False,
           vault: VaultOpt = None) -> None:
    """Recompute all states and rewrite keepfold_status/keepfold_next where they changed.

    --dry-run prints a diff of what would change; --all lifts its 25-line cap per group."""
    v = _vault(vault)
    if not dry_run:  # a dry run changes nothing, so it skips the catalog sync too
        for line in _sync_lines(catalog_sync(v, if_changed=True)):
            typer.echo(line)
    states, _ = planner.current_states(v, local_day(datetime.now(UTC)))
    counts = dict.fromkeys(levels.STATES, 0)
    changed = 0
    groups: dict[str, list[str]] = {}
    for note in v.iter_notes("concept"):
        st = states.get(note.id)
        status, nxt = (st.state, st.next_due) if st else ("not_started", None)
        counts[status] += 1
        old_next = note.fields.get("keepfold_next")
        old_next = None if old_next is None else str(old_next)  # YAML may parse a bare date
        old = note.fields.get("keepfold_status")
        if (old, old_next) != (status, nxt):
            changed += 1
            if dry_run:
                key = f"{old or 'unstamped'} → {status}" if old != status else "next due moved"
                groups.setdefault(key, []).append(
                    f"{note.id}  {old or 'unstamped'} → {status}  (next {old_next} → {nxt})"
                    f"  {_why(st)}")
            else:
                v.set_tool_props("concept", note.id, status, nxt)
    if dry_run:
        for key, lines in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            typer.echo(f"{key}: {len(lines)}")
        typer.echo(f"unchanged: {sum(counts.values()) - changed}")
        for key, lines in groups.items():
            typer.echo(f"\n{key}")
            shown = lines if all_ else lines[:25]
            for line in shown:
                typer.echo(line)
            if len(shown) < len(lines):
                typer.echo(f"+{len(lines) - len(shown)} more")
        typer.echo("\ncalibration: state going into a graded day, and how often that day's"
                   " first item passed")
        for s, (n, ok) in levels.calibration(EventLog(v).iter_events(),
                                             levels.problem_concepts(v)).items():
            typer.echo(f"{levels.LABEL[s]}: {n} concept-day{'s' * (n != 1)}"
                       + (f", {ok / n:.0%} passed" if n else ""))
        typer.echo("")
    for s in levels.STATES:
        typer.echo(f"{levels.GLYPH[s]} {levels.LABEL[s]}: {counts[s]}")
    typer.echo(f"{changed} note(s) {'would change' if dry_run else 'updated'}")


@app.command()
@_errors
def backup(to: Annotated[Path | None, typer.Option(
               help="Folder for the zip, outside the vault; default ~/.keepfold/backups/<vault "
                    "id>, or backup.folder in config.yaml.")] = None,
           no_private: bool = typer.Option(False, "--no-private", help=(
               "Leave out private pages, pages under a private subject, their raw sources, "
               "browse/private, the browse store and .git (its history holds private pages).")),
           vault: VaultOpt = None) -> None:
    """Zip the whole vault (with .git) to ~/.keepfold/backups/<vault id> or --to DIR."""
    v = _vault(vault)
    dest = backup_mod.backup(v.root, to, private=not no_private)
    typer.echo(f"{dest} ({dest.stat().st_size:,} bytes)")
    if to is not None and backup_mod.same_drive(dest, v.root):
        typer.echo("WARN: the backup is on the same drive as the vault; copy it elsewhere.",
                   err=True)


@app.command()
@_errors
def restore(zip_path: Annotated[Path, typer.Argument(metavar="ZIP", help="A backup zip.")],
            dest: Annotated[Path | None, typer.Argument(help=(
                "A new or empty folder; default <vault>-restored-<time> beside the vault."))]
            = None,
            vault: VaultOpt = None) -> None:
    """Unpack a backup zip into a new or empty folder, never over a vault; the restored vault
    gets its own vault id."""
    if dest is None:
        root = _vault(vault).root
        dest = root.parent / f"{root.name}-restored-{datetime.now():%Y%m%d-%H%M}"
    out = backup_mod.restore(zip_path, dest)
    typer.echo(f"Restored to {out}. To use it, open the Keepfold app, Notes folder, Use this "
               f'folder; or run: keepfold init "{out}" --default')


@app.command()
@_errors
def undo(session_id: str, vault: VaultOpt = None) -> None:
    """Revert a session's vault commit; where a later session changed the same lines, the later
    text is kept. Then restamp props and Today.md and commit."""
    if not SESSION_ID_RE.fullmatch(session_id):
        raise VaultError("invalid_argument", "The session id looks like s_<26-char ULID>.")
    v = _vault(vault)
    git_on, private_kept = session_mod.git_choice()
    if not git_on:
        raise VaultError("invalid_argument", "Undo needs the vault's git history, and git is "
                         "off in your settings, so no session was saved to go back to. Turn on "
                         "\"Keep the vault's history in git\" under Vault in the app (or set "
                         "`git: {enabled: true}` in ~/.keepfold/config.yaml); sessions closed "
                         "after that can be undone.")
    with v.lock():
        # anchored: `keepfold: other changes before session <id>` is not that session's commit;
        # `cortex:` is how 0.6.1 and older (Cortex) named the same commits
        found = _git(v.root, "log", "-E",
                     f"--grep=^(keepfold|cortex): (session|undo) {session_id}",
                     "--format=%H %s", "-n", "1")
        commit, _, subject = found.stdout.strip().partition(" ")
        if found.returncode or not commit:
            typer.echo(f"No vault commit mentions {session_id}; nothing to undo.")
            return
        if subject.partition(": ")[2].startswith("undo "):
            typer.echo(f"{session_id} is already undone ({commit[:10]}).")
            return
        if busy := session_mod.git_busy(v.root):
            raise VaultError("invalid_argument", f"The vault repo is mid-operation ({busy}); "
                             "finish or abort it in git first.")
        if dirty := [ln[3:] for ln in _git(v.root, "status", "--porcelain").stdout.splitlines()
                     if ln.strip()]:
            from keepfold import duplicates  # a path is vault data: no control chars or links
            names = ", ".join(duplicates.safe(p, 80) for p in dirty[:5])
            raise VaultError("invalid_argument", f"The vault has uncommitted changes ({names}"
                             f"{f' and {len(dirty) - 5} more' if len(dirty) > 5 else ''}); "
                             "close the open session or commit them first. If a session close "
                             "keeps leaving a file out, it looks like it holds a key: remove "
                             "the key and close again.")

        from keepfold.browse.forget import PRIVATE_IGNORE

        # C3: a private browse page the commit holds (an older vault) is never reverted,
        # committed or deleted: git may overwrite an ignored file, so what is on disk now is
        # put back, and the index left as HEAD has it, after each step that may touch it.
        # With `git.private_browsing` on they are ordinary tracked pages and revert as such.
        private = [] if private_kept else [
            p for p in _git(v.root, "diff-tree", "-r", "--name-only", "--no-commit-id",
                            "--relative", commit).stdout.splitlines()
            if p.startswith(PRIVATE_IGNORE)]
        saved = {p: (v.root / p).read_bytes() for p in private if (v.root / p).is_file()}

        def keep_private():
            for p in private:
                _git(v.root, "reset", "-q", "--", p)
                if p in saved:
                    (v.root / p).parent.mkdir(parents=True, exist_ok=True)
                    v._atomic_write(v.root / p, saved[p])
                elif (v.root / p).exists():
                    v.remove(v.root / p)

        def restore():  # the tree was clean and we hold the lock, so HEAD is the whole state
            _git(v.root, "revert", "--abort")
            _git(v.root, "reset", "-q", "--hard", "HEAD")
            keep_private()

        try:
            kept = []
            # R10: only a forget's redaction changes a session's own event file later, so the
            # session's undo still deletes it
            own = lambda p: "events" in Path(p).parts and Path(p).stem == session_id  # noqa: E731
            if _git(v.root, "revert", "--no-commit", commit).returncode:
                kept = [p for p in session_mod.unmerged_paths(v.root)  # a later commit overlapped
                        if not any(p.endswith(q) for q in private) and not own(p)]
                restore()
                r = _git(v.root, "revert", "--no-commit", "-X", "ours", commit)
                if r.returncode and not session_mod.unmerged_paths(v.root):
                    raise VaultError("invalid_argument", f"git revert failed: {r.stderr.strip()}")
            keep_private()
            for p in session_mod.unmerged_paths(v.root):  # modify/delete: keep HEAD's side
                in_head = not own(p) and not _git(v.root, "cat-file", "-e", f"HEAD:{p}").returncode
                _git(v.root, *(("checkout", "HEAD", "--") if in_head else ("rm", "-qf", "--")),
                     p)
            if left := session_mod.unmerged_paths(v.root):
                raise VaultError("invalid_argument", f"unresolved: {', '.join(left)}")
            replay(dry_run=False, vault=v)  # stamp props and Today.md as session close does
            today(vault=v)
            for args in (("add", "-A"), ("commit", "-q", "-m", f"keepfold: undo {session_id}")):
                r = _git(v.root, *args)
                if r.returncode:
                    raise VaultError("invalid_argument", f"git {args[0]} failed: "
                                     f"{r.stderr.strip() or r.stdout.strip()}")
        except BaseException:
            restore()
            raise
    for p in kept:
        typer.echo(f"kept later text: {p}")
    typer.echo(f"reverted {commit[:10]} ({session_id})")


# ---- session, hook --------------------------------------------------------------

def _closed(ids: list[str]) -> None:
    for sid in ids:
        typer.echo(f"closed {sid}")
    if not ids:
        typer.echo("No orphan sessions to close.")


@session_app.command("sweep")
@_errors
def session_sweep(vault: VaultOpt = None) -> None:
    """Close sessions with no close event whose server has exited or that have idled 2 h."""
    _closed(session_mod.sweep(_vault(vault)))


@session_app.command("close")
@_errors
def session_close(vault: VaultOpt = None) -> None:
    """Close every session with no close event now, whatever its age."""
    _closed(session_mod.sweep(_vault(vault), force=True))


@app.command()
@_errors
def mcp(vault: VaultOpt = None,
        role: Annotated[str, typer.Option(help="owner (default) or reader; fixed "
                                          "for this server's life.")] = "owner",
        private: Annotated[bool, typer.Option(
            "--private", help="Also serve keepfold_browse_search over every browse page, private "
            "ones included. For local models only; needs browse.private_mcp: true.")] = False,
        ) -> None:
    """Serve the Keepfold MCP tools over stdio."""
    from keepfold import mcp_server  # the mcp SDK is only needed here

    if role not in mcp_server.ROLES:
        raise VaultError("invalid_argument", f"--role must be one of "
                         f"{', '.join(mcp_server.ROLES)}.")
    if private:
        from keepfold.browse.privacy import load_settings
        if not load_settings().private_mcp:
            raise VaultError("invalid_argument", "--private is off: it serves your private "
                             "browsing, so turn on 'Local AI access (LM Studio only)' in "
                             "`keepfold app` (browse.private_mcp: true) first.")
    if folder := taint.excluded(*taint.session_dirs()):
        typer.echo(f"keepfold: {folder} is excluded; refusing every tool.", err=True)
    mcp_server.serve(_vault(vault), role, folder, private=private)


# ---- v5 PHASE4 V4-V5: forget, kill switch, access log ----------------------------

@app.command()
@_errors
def forget(
    id: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
    vault: VaultOpt = None,
) -> None:
    """Forget a page (people/<slug>, a page id, a chat page) or one line of a me/ page
    (me/<page>^c-xxxxx) everywhere Keepfold keeps it: the page or line, the event log (its text
    redacted, ids and grades kept), Keepfold's own pages and caches, the chats it keeps, the
    search snapshot and vectors. Prints a receipt, with what Keepfold cannot remove."""
    from keepfold import forget as forget_

    v = _vault(vault)
    if not yes:
        typer.confirm(f"Forget {forget_.check(v, id)} everywhere Keepfold keeps it? This cannot be "
                      "undone.", abort=True)
    for line in forget_.lines(forget_.forget(v, id)):
        typer.echo(line)


@app.command()
@_errors
def kill(off: Annotated[bool, typer.Option("--off", help="Turn the switch off.")] = False,
         vault: VaultOpt = None) -> None:
    """Kill switch: stop act-as-me and every reader server."""
    from keepfold import share

    share.set_kill(_vault(vault), not off)
    typer.echo("kill switch off" if off else "kill switch on: act-as-me and the reader role "
               "are refused (`keepfold kill --off` to undo)")


exclude_app = typer.Typer(no_args_is_help=True, help="Folders where Claude Code sessions get "
                          "no priming and no Keepfold tools (a client's repo, cloned code).")
app.add_typer(exclude_app, name="exclude")


@exclude_app.command("add")
@_errors
def exclude_add(path: Path) -> None:
    """Exclude a folder and everything under it."""
    p = str(path.expanduser().resolve())
    taint.check_config("adding an excluded folder")  # else excluded() says every one already is
    if f := taint.excluded(p):
        typer.echo(f"already excluded (by {f})")
        return
    taint.write_config(lambda cfg: {"exclude": [*taint.exclude_list(cfg), p]})
    typer.echo(f"excluded {p} (new Claude Code sessions there: no priming, no Keepfold tools)")


@exclude_app.command("remove")
@_errors
def exclude_remove(path: Path) -> None:
    """Stop excluding a folder (as listed by `keepfold exclude list`)."""
    p = path.expanduser().resolve()

    def merge(cfg: dict) -> dict:  # under the config lock
        folders = taint.exclude_list(cfg)
        keep = [f for f in folders if Path(f).expanduser().resolve() != p]
        if len(keep) == len(folders):
            raise VaultError("invalid_argument", f"{p} is not in the exclude list; see "
                             "`keepfold exclude list`.")
        return {"exclude": keep}
    taint.write_config(merge)
    typer.echo(f"no longer excluded: {p}")


@exclude_app.command("list")
@_errors
def exclude_list() -> None:
    """Print the excluded folders, one per line."""
    taint.check_config("`keepfold exclude list`")  # an empty list would read as "none excluded"
    for f in taint.exclude_list():
        typer.echo(f)


@app.command("access")
@_errors
def access_cmd(days: Annotated[int, typer.Option(help="How many days back to list.")] = 30,
               vault: VaultOpt = None) -> None:
    """List act-as-me reads (and an old vault's drafts) from the access log, oldest first."""
    from keepfold import share

    since = (datetime.now() - timedelta(days=days)).date().isoformat()
    rows = share.access_rows(EventLog(_vault(vault)).iter_events(), since)
    for r in rows:
        ts = parse_ts(r["ts"])
        typer.echo(f"{r['day']}  {ts.astimezone():%H:%M}  {r['client'] or '-'}  {r['access']}  "
                   f"{r['audience'] or '-'}  {render.plain(r['task'] or '', 80)}")
    if not rows:
        typer.echo(f"no access in the last {days} days")


# ---- semantic recall ------------------------------------------------


def _dir_mb(d: Path) -> float:
    return sum(p.stat().st_size for p in d.rglob("*")
               if p.is_file() and not p.is_symlink()) / 1e6


# Trying again restarts the unfinished file: huggingface_hub deletes a partial download (only
# whole files are kept), so no message promises it continues where it stopped.
_AGAIN = " Then try again: the download starts over."
_DOWNLOAD_ERRORS = (  # (words in the error, in lower case; the plain sentence), first match wins
    (("no space left", "errno 28", "os error 112", "winerror 112", "not enough space"),
     "Your disk is full: the search model needs about 150 MB free. Free up some space, then "
     "try again."),
    (("407", "proxy"),
     "Your network's proxy refused the download from huggingface.co (it may need you to sign "
     "in first). Try a phone hotspot or another network." + _AGAIN),
    (("certificate", "ssl"),
     "Your network blocked huggingface.co (common on campus Wi-Fi). Try a phone hotspot or "
     "another network." + _AGAIN),
    (("timed out", "timeout"),
     "The download from huggingface.co took too long and stopped. Try a faster or steadier "
     "network." + _AGAIN),
    (("getaddrinfo", "name or service", "nodename", "11001", "resolve", "connect",
      "unreachable", "network"),
     "Could not reach huggingface.co. Check that you are online, or try another network."
     + _AGAIN),
)


def _download_error(e: BaseException, seen: list[str]) -> str:
    """One plain sentence for a failed model download. fastembed swallows the network error and
    raises a bare ValueError, so `seen` (its logged errors) is read along with the chain."""
    if isinstance(e, OSError) and (e.errno == 28 or getattr(e, "winerror", None) == 112):
        return _DOWNLOAD_ERRORS[0][1]
    parts, x = [*seen], e
    while x is not None and len(parts) < 20:
        parts.append(f"{type(x).__name__} {x}")
        x = x.__cause__ or x.__context__
    text = " ".join(parts).lower()
    for words, sentence in _DOWNLOAD_ERRORS:
        if any(w in text for w in words):
            return sentence
    return "The search model download stopped. Try again later or on another network." + _AGAIN


def _log_download_error(e: BaseException, seen: list[str]) -> Path:
    """The full error, for whoever helps: ~/.keepfold/logs/model-download.log (never printed)."""
    import logging

    path = Path.home() / ".keepfold" / "logs" / "model-download.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    h = logging.FileHandler(path, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log = logging.getLogger("keepfold.model_download")
    log.propagate = False
    log.addHandler(h)
    try:
        log.error("model download failed; fastembed said: %s", " | ".join(seen) or "-",
                  exc_info=e)
    finally:
        log.removeHandler(h)
        h.close()
    return path


def _download_setup() -> None:
    """For this download only: the OS certificate store (campus TLS inspection) and the system
    proxy for hf_xet, which reads only the env."""
    try:
        import truststore
        truststore.inject_into_ssl()
    except Exception:  # noqa: BLE001  (certifi's certificates still work off campus)
        pass
    os.environ.update(proxy_env())
    # unless this is set, huggingface_hub fetches its list of AI agents from huggingface.co and
    # adds the agent it detects and the torch version to its User-Agent (read at its import,
    # which comes after this)
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


@model_app.command("download")
@_errors
def model_download(vault: VaultOpt = None, voice: Annotated[bool, typer.Option(
        "--voice", help="Instead: install faster-whisper and download the local speech model "
                        "for voice memos (about 150 MB).")] = False) -> None:
    """Download the embedding and rerank models once, then build the vault's vector cache."""
    from keepfold import semantic

    if voice:
        from keepfold import voice_memos
        try:
            _download_setup()
            voice_memos.install(typer.echo)
        except Exception as e:  # noqa: BLE001  (a plain sentence, never a traceback)
            typer.echo(f"The speech model install stopped ({e}). Try again later or on "
                       "another network.", err=True)
            raise typer.Exit(1) from None
        typer.echo(f"speech model ready in {semantic.models_dir()}")
        return

    d = semantic.models_dir()
    seen: list[str] = []
    try:
        from loguru import logger  # fastembed's logger, where its download errors go
        sink = logger.add(lambda m: seen.append(m.record["message"]), level="ERROR")
    except Exception:  # noqa: BLE001
        logger = sink = None
    try:
        _download_setup()
        (d / doctor_mod.MODEL_READY).unlink(missing_ok=True)  # back only once this run finishes
        typer.echo(f"model {semantic.MODEL}: downloading if missing into {d} ...")
        embed = semantic.load_embedder(download=True)
        typer.echo(f"reranker {semantic.RERANK_MODEL}: downloading if missing ...")
        semantic.load_reranker(download=True)
        (d / doctor_mod.MODEL_READY).write_text("downloaded\n", encoding="utf-8")
    except Exception as e:  # noqa: BLE001  (a plain sentence, never a traceback)
        sentence = _download_error(e, seen)
        try:
            log = _log_download_error(e, seen)
            sentence += f" Details are in the log: {log}"
        except OSError:  # a full disk may refuse the log too
            pass
        typer.echo(sentence, err=True)
        raise typer.Exit(1) from None
    finally:
        if sink is not None:
            logger.remove(sink)
    typer.echo(f"model ready: {_dir_mb(d):.0f} MB in {d}")
    try:
        v = _vault(vault)
    except VaultError as e:
        typer.echo(f"no vault indexed: {e}")
        return
    _build(v, embed)


def _build(v: Vault, embed) -> None:
    from keepfold import semantic
    from keepfold.index import Index

    t = time.perf_counter()
    shown = [t]

    def progress(done: int, total: int) -> None:
        if time.perf_counter() - shown[0] >= 30 or done == total:
            shown[0] = time.perf_counter()
            typer.echo(f"  {done}/{total} chunks, {time.perf_counter() - t:.0f}s")

    ids, _, start, _ = semantic.vectors(Index(v, Catalog.load(v)), embed,
                                        len(embed(["keepfold"])[0]), save_every=60,
                                        progress=progress)
    typer.echo(f"indexed {len(ids)} pages, {len(start)} chunks in "
               f"{time.perf_counter() - t:.1f}s -> {v.root / semantic.CACHE}")


@model_app.command("build")
@_errors
def model_build(vault: VaultOpt = None) -> None:
    """Build every chunk of the vault's vector cache (a recall builds only a second's worth).
    Saves each minute; run it again to resume. Never downloads."""
    from keepfold import semantic

    semantic.ARENA = True  # a short-lived bulk build: the arena's speed, its memory ends with us
    embed = semantic.load_embedder()
    if embed is None:
        raise VaultError("invalid_argument", semantic.NOT_SET_UP)
    _build(_vault(vault), embed)


@model_app.command("status")
@_errors
def model_status(vault: VaultOpt = None) -> None:
    """Show whether the model is downloaded and the vault's vector cache size."""
    from keepfold import semantic

    d = semantic.models_dir()
    if semantic.load_embedder() is None:
        typer.echo(f"model {semantic.MODEL}: not downloaded; run `keepfold model download`")
    else:
        typer.echo(f"model {semantic.MODEL}: ready, {_dir_mb(d):.0f} MB in {d}")
    on = semantic.rerank_on()
    typer.echo(f"reranker {semantic.RERANK_MODEL}: "
               + ("ready" if semantic.load_reranker(config=False) else "not downloaded")
               + ("" if on else " (off: rerank: false in ~/.keepfold/config.yaml)"))
    try:
        cache = _vault(vault).root / semantic.CACHE
    except VaultError:
        return
    typer.echo(f"vectors: {cache} ({cache.stat().st_size / 1e6:.1f} MB)" if cache.exists()
               else "vectors: not built yet (built on the first recall)")


@app.command("recall")
@_errors
def recall_cmd(query: str,
               limit: Annotated[int, typer.Option(help="Show at most this many hits.")] = 8,
               hops: Annotated[int, typer.Option(
                   help="Also follow links up to this many steps from each hit (0-2).")] = 1,
               vault: VaultOpt = None,
               explain: bool = typer.Option(False, help="Print each hit's score parts."),
               show_misses: bool = typer.Option(
                   False, help="Also list the next 5 pages that missed the cut, and why.")) -> None:
    """Pages similar in meaning or by keyword, plus their linked pages. Never logs a recall."""
    from keepfold import harvest, semantic
    from keepfold.index import Index

    v = _vault(vault)
    idx = Index(v, Catalog.load(v))
    ex = {} if explain or show_misses else None
    hits, notice = semantic.search(idx, query, limit, hops,
                                   quiet=harvest.quiet(EventLog(v).iter_events()), explain=ex)
    if notice:
        typer.echo(notice, err=True)
    parts, misses = semantic.explained(idx, ex, hits, 5 if show_misses else 0) if ex is not None \
        else ([], [])
    for k, h in enumerate(hits):
        typer.echo(render.term(f"{h['score']:.3f}  {h['match']:<7}  {h['id']} — {h['title']}"
                               + (f"  (via {h['via']})" if "via" in h else "")))
        if explain:
            typer.echo("         " + _parts(parts[k]))
    if not hits:
        typer.echo("no hits")
    if show_misses:
        typer.echo("missed:" if misses else "missed: none")
        for p in misses:
            typer.echo(f"{p['score']:.3f}  {p['id']}: {p['reason']}\n         " + _parts(p))


@app.command("ask")
@_errors
def ask_cmd(question: str, vault: VaultOpt = None,
            private: bool = typer.Option(False, "--private", help=(
                "Also read private notes and private browsing. The answer and its sources "
                "print here, so never use it where an AI app reads this terminal."))) -> None:
    """Answer a question from your notes, saved AI chats and browsing with the local model
    only (Ollama or LM Studio); never a cloud model. Prints the cited sources. Private notes
    and private browsing only with --private: a terminal an AI app runs must never print them."""
    from keepfold import access, ask

    if private and not access.may_see("terminal", access.Audience.LOCAL, "note",
                                      private_flag=True, tty=_tty()):
        typer.echo("Refused: private answers print only in a real terminal, never in one an AI "
                   "app reads (stdin and stdout must both be a terminal). Run `keepfold ask "
                   "--private` yourself in a terminal window, or ask without --private.",
                   err=True)
        raise typer.Exit(1)
    try:
        out = ask.ask(_vault(vault), question, private=private)
    except ValueError as e:  # localllm refuses a cloud model or a non-loopback url in config
        typer.echo(f"{e}. Fix localllm in ~/.keepfold/config.yaml: it must name a model on this "
                   "computer.", err=True)
        raise typer.Exit(1) from None
    if "error" in out:
        typer.echo(out["message"], err=True)
        raise typer.Exit(1)
    typer.echo(render.term(out["answer"]))
    for s in out["sources"]:
        typer.echo(render.term(f"[{s['n']}] {s['path']}"))
    typer.echo(f"answered by {out['model']}")


def _tty() -> bool:
    """stdin and stdout are both a real terminal (E3: a pipe or an AI app's runner is not; nor
    is NUL, which Windows calls a tty, so stdin must also be a console)."""
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
        if os.name != "nt":
            return True
        import ctypes
        import msvcrt
        return bool(ctypes.windll.kernel32.GetConsoleMode(
            msvcrt.get_osfhandle(sys.stdin.fileno()), ctypes.byref(ctypes.c_ulong())))
    except (AttributeError, ValueError, OSError):  # no stream, or a closed one
        return False


def _parts(p: dict) -> str:
    """One line of `semantic.explained` parts for `recall --explain`."""
    from keepfold import duplicates  # imported chat text is untrusted: no control chars, links
    out = [f"cos {p['cos']:.3f}" if p["cos"] is not None else "cos -",
           f"kw {p['kw']:.3f}"]
    if p["cover"] is not None:
        out.append(f"cover {p['cover']:.2f}")
    if p["weight"] != 1.0:
        out.append(f"weight {p['weight']:.3g} ("
                   + " ".join(f"{k} {v:g}" for k, v in p["factors"].items()) + ")")
    if "rerank" in p:
        out.append(f"rerank {p['rerank']:.3f}")
    if p["chunk"]:
        out.append(f"chunk@{p['at']} \"{duplicates.safe(p['chunk'], 80)}\"")
    return "  ".join(out)


chat_app = typer.Typer(help="Keepfold's Chat, from the command line.")
app.add_typer(chat_app, name="chat")


@chat_app.command("eval")
def chat_eval_cmd(
        out: Annotated[Path | None, typer.Option(
            "--out", help="Also write the full report (answers, served passages) as JSON.")]
        = None,
        keep: Annotated[Path | None, typer.Option(
            "--keep", help="Build the made-up vault in this new folder and leave it there.")]
        = None,
        no_model: Annotated[bool, typer.Option(
            "--no-model", help="Run only the questions Keepfold answers with no model.")] = False,
        served: Annotated[bool, typer.Option(
            "--served", help="List the passages each answer was given.")] = False,
        runs: Annotated[int, typer.Option(
            "--runs", min=1, help="Ask each model question this many times; a question passes "
                                  "pass^N only if every run passed.")] = 1,
        retrieval: Annotated[bool, typer.Option(
            "--retrieval", help="Search only: the search model serves each question, a stub "
                                "stands in for the chat model.")] = False,
        replay: Annotated[Path | None, typer.Option(
            "--replay", help="Re-score a report saved with --out with today's scorer, with no "
                             "model and no vault.")] = None) -> None:
    """Chat's offline test: 60 questions on a made-up vault in a temp folder, scored in code.
    Questions that need a model run only when a 2B local model is already loaded; nothing is
    loaded or downloaded, and your vault, settings and browsing are not used."""
    from keepfold import chat_eval

    if keep is not None and keep.exists():
        typer.echo(f"error: {keep} already exists; --keep needs a new folder.", err=True)
        raise typer.Exit(1)
    try:
        r = chat_eval.replay(json.loads(replay.read_text(encoding="utf-8"))) if replay else \
            chat_eval.run(use_model=not no_model, keep=keep, runs=runs, retrieval=retrieval,
                          progress=lambda d, n, qid: typer.echo(f"{d}/{n} {qid}", err=True))
    except (RuntimeError, OSError, ValueError, KeyError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None
    typer.echo(render.term(chat_eval.summary(r, served)))
    if out is not None:
        out.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
        typer.echo(f"Report written to {out}")


@eval_app.callback(invoke_without_command=True)
def eval_main(ctx: typer.Context, k: int = 8, vault: VaultOpt = None) -> None:
    """Measure ranking on logged usage; bare `keepfold eval` runs `all`."""
    if ctx.invoked_subcommand is None:
        eval_all(k, vault)
    elif vault is not None or k != 8:  # the subcommand has its own options; never drop them
        raise typer.BadParameter("put --k and --vault after the subcommand, e.g. "
                                 "`keepfold eval recall --vault X`.")


@eval_app.command("recall")
@_errors
def eval_recall(k: Annotated[int, typer.Option(help="Score the top k hits.")] = 8,
                vault: VaultOpt = None) -> None:
    """Replay logged recall queries (relevant = pages opened after them) through each ranking
    variant: recall@k and MRR."""
    from keepfold import recall_log, semantic
    from keepfold.index import Index

    v = _vault(vault)
    pairs = recall_log.eval_set(recall_log.rows(v))
    embed, ce = semantic.load_embedder(), semantic.load_reranker(config=False)
    typer.echo(f"queries: {len(pairs)}" + (
        f" (too few to trust; need about {recall_log.MIN_TRUST})"
        if len(pairs) < recall_log.MIN_TRUST else ""))
    if not embed:
        typer.echo(semantic.NOT_SET_UP + " (variants run keyword-only)", err=True)
    typer.echo(f"{'variant':<12} {'recall@' + str(k):>9} {'MRR':>6}")
    for name, m in recall_log.evaluate(Index(v, Catalog.load(v)), pairs, embed, ce, k).items():
        typer.echo(f"{name:<12} " + (f"{m[0]:>9.3f} {m[1]:>6.3f}" if m
                                     else "skipped: reranker not downloaded"))


@eval_app.command("learning")
@_errors
def eval_learning(since: Annotated[str | None, typer.Option(
                      help="Only items graded on or after this local day (YYYY-MM-DD).")] = None,
                  json_out: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
                  vault: VaultOpt = None) -> None:
    """Read-only: how well the logged predicted_p forecasts graded items (pass = correct at
    hints <= 1): Brier, skill, log loss, reliability bins, split by mode, grader, source,
    item type and confidence."""
    from keepfold import learning_eval

    if since is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        raise VaultError("invalid_argument", "--since must be a day like 2026-11-01.")
    v = _vault(vault)
    r = learning_eval.report(EventLog(v).iter_events(), levels.problem_concepts(v), since)
    typer.echo(json.dumps(r, indent=1) if json_out else learning_eval.text(r))


@eval_app.command("harvest-facts")
@_errors
def eval_harvest_facts(file: Annotated[Path | None, typer.Option(
        "--file", help="A labelled eval file (default: the repo's "
                       "tests/fixtures/harvest_eval.json).")] = None) -> None:
    """R6d: run the no-model fact finder (Tier 0) over labelled chat turns: precision,
    recall, hypothetical false positives and polarity flips against their targets. Reads no
    vault and writes nothing."""
    from keepfold import fact_harvest as fh

    p = file or Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "harvest_eval.json"
    try:
        items = json.loads(p.read_text(encoding="utf-8"))["items"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise VaultError("invalid_argument", f"Cannot read the eval file {p}: {e}") from None
    r = fh.evaluate(items)
    t = fh.TARGETS
    typer.echo(f"turns: {r['turns']}  facts extracted: {r['extracted']}  right: {r['right']}")
    typer.echo(f"precision: {r['precision']:.1%} (target >= {t['precision']:.0%})")
    typer.echo(f"recall: {r['recall']:.1%} ({r['found']} of {r['expected']} expected facts)")
    typer.echo(f"hypothetical false positives: {r['hypothetical_fp']:.1%} ({r['unreal_hit']} of "
               f"{r['unreal']} hypothetical or roleplay turns; target <= "
               f"{t['hypothetical_fp']:.0%})")
    typer.echo(f"reactions saved as facts: {r['reaction_fp']:.1%} ({r['reaction_hit']} of "
               f"{r['reaction']} reactions to the chat's own output; target <= "
               f"{t['reaction_fp']:.0%})")
    typer.echo(f"polarity flips: {r['flips']} (target {t['flips']})")
    typer.echo(f"promotion facts not held for the Review: {r['held_missed']}")
    typer.echo("targets: met" if fh.meets(r) else "targets: NOT met")


@eval_app.command("agent")
@_errors
def eval_agent(model: Annotated[bool, typer.Option(
                   "--model", help="Also ask the local model you already loaded (Layer B, "
                                   "three replies per case); never loads one.")] = False,
               json_out: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False
               ) -> None:
    """R12: does each AI app get the right lines about you and none it must not? Builds ten
    made-up vaults and checks what each app is served (Layer A, no model); --model also
    checks a loaded local model's replies. Reads no vault of yours."""
    from keepfold import agent_eval

    r = agent_eval.run(model=model)
    typer.echo(json.dumps(r, indent=1) if json_out else agent_eval.summary(r))
    if r["a_fail"]:
        raise typer.Exit(1)


@eval_app.command("loop")
@_errors
def eval_loop(json_out: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False
              ) -> None:
    """Does what you tell your AI apps come back to you? Pushes made-up chats (26 things a
    student says) through Keepfold's no-model paths in a temporary home and a temporary
    notes folder, then counts what was kept, what the next chat is served and what retires
    unseen. Reads no vault of yours, never touches ~/.keepfold, loads no model. Run it from a
    repo checkout (it reads tests/fixtures/loop/)."""
    from keepfold import loop_eval

    try:
        r = loop_eval.run()
    except loop_eval.LoopRefused as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(r, indent=1) if json_out else loop_eval.summary(r))


@eval_app.command("all")
@_errors
def eval_all(k: Annotated[int, typer.Option(help="Score the top k recall hits.")] = 8,
             vault: VaultOpt = None) -> None:
    """Recall and learning evals in one report; appends its numbers (never queries or text) to
    .keepfold/eval-history.jsonl and prints deltas against an earlier run from another commit on
    the same day and index. Run it before and after a tuning change."""
    from keepfold import eval_history, learning_eval, recall_log, semantic
    from keepfold.index import Index

    v = _vault(vault)
    idx = Index(v, Catalog.load(v))
    pairs = recall_log.eval_set(recall_log.rows(v))
    embed, ce = semantic.load_embedder(), semantic.load_reranker(config=False)
    if pairs and not embed:
        typer.echo(semantic.NOT_SET_UP + " (recall variants run keyword-only)", err=True)
    recall = recall_log.evaluate(idx, pairs, embed, ce, k) if pairs else {}
    learning = learning_eval.report(EventLog(v).iter_events(), levels.problem_concepts(v))
    run = eval_history.rows(recall, len(pairs), k, learning, date=local_day(datetime.now(UTC)),
                            sha=eval_history.git_sha(),
                            index=eval_history.fingerprint(idx, embed is not None))
    typer.echo(eval_history.text(run, eval_history.before(eval_history.read(v), run)))
    if run:
        eval_history.append(v, run)


def _topics(vault: Path | None, embed: bool = False):
    """(vault, index, embedder or None) for the topics commands; `embed` requires the model."""
    from keepfold import semantic
    from keepfold.index import Index

    model = semantic.load_embedder() if embed else None
    if embed and model is None:
        raise VaultError("invalid_argument", semantic.NOT_SET_UP)
    v = _vault(vault)
    return v, Index(v, Catalog.load(v)), model


DryRun = Annotated[bool, typer.Option("--dry-run", help="Report what would change; write "
                                      "nothing.")]


@topics_app.command("status")
@_errors
def topics_status(vault: VaultOpt = None) -> None:
    """Counts: hubs per depth, hubs over the split size, unfiled pages."""
    from keepfold import topics

    _, idx, _ = _topics(vault)
    s = topics.status(idx, topics.tree(idx))
    typer.echo(f"hubs: {s['hubs']} (" + ", ".join(f"depth {d}: {n}" for d, n in
                                                  s["by_depth"].items()) + ")")
    typer.echo(f"hubs over {topics.SPLIT_N} direct pages: {s['over_split']}")
    typer.echo(f"pages (resources and notes): {s['pages']}; unfiled: {s['unfiled']}")


@topics_app.command("attach")
@_errors
def topics_attach(dry_run: DryRun = False,
                  margin: Annotated[float | None, typer.Option(
                      help="How far the best hub must beat the next one (default 0.02).")]
                  = None,
                  floor: Annotated[float | None, typer.Option(
                      help="The lowest similarity that still files a page (default 0.80).")]
                  = None,
                  limit: Annotated[int | None, typer.Option(
                      help="Try a seeded sample of N unfiled pages.")] = None,
                  vault: VaultOpt = None) -> None:
    """File unfiled resources and notes under their nearest topic hub (backing off to the
    parent when the top hubs are close); the rest stay unfiled."""
    from keepfold import topics

    margin = topics.MARGIN if margin is None else margin  # None default: topics imports numpy
    floor = topics.FLOOR if floor is None else floor
    v, idx, embed = _topics(vault, embed=True)
    n, results = topics.attach_unfiled(idx, embed, margin, floor, limit)
    tree = topics.tree(idx)
    done = [(p, a) for p, a in results if a.hub]
    by = Counter(min(tree.depth(a.hub), 3) for _, a in done)
    typer.echo(f"attached {len(done)} of {n} (area {by[1]}, subtopic {by[2]}, leaf {by[3]}); "
               f"left unfiled {n - len(done)}")
    for p, a in done[:max(10, limit or 0)]:
        typer.echo(f"  {p} -> {a.hub} ({a.score:.3f}): {render.term(idx._notes[p]['title'])} -> "
                   f"{render.term(tree.title[a.hub])}")
    if not dry_run:
        for p, a in done:
            topics.place(v, p, [a.hub])
    typer.echo("dry run: nothing written" if dry_run else "written")


@topics_app.command("build")
@_errors
def topics_build(dry_run: DryRun = False, vault: VaultOpt = None) -> None:
    """Regenerate the generated hub sections (Part of line, Sub-topics, Conversations, Areas)
    and the month pages' Links in place; hand-written text is never rewritten."""
    from keepfold import topics

    v, idx, _ = _topics(vault)
    c = topics.build(v, idx, dry_run)
    typer.echo(f"{'would change' if dry_run else 'changed'}: hubs {c['hubs']}, month pages "
               f"{c['months']}; unchanged {c['unchanged']}"
               + (f"; skipped {c['unsafe']} (hand-written text would change)"
                  if c["unsafe"] else ""))


@topics_app.command("split")
@_errors
def topics_split(hub: Annotated[str | None, typer.Argument(help="The hub to split.")] = None,
                 propose: Annotated[bool, typer.Option("--propose", help="Write and print the "
                                                       "proposal.")] = False,
                 apply: Annotated[bool, typer.Option("--apply", help="Apply the proposal "
                                                     "(needs --auto-names).")] = False,
                 auto_names: Annotated[bool, typer.Option(
                     "--auto-names", help="Name the children from their title terms and mark "
                     "them names: provisional (no chat needed).")] = False,
                 vault: VaultOpt = None) -> None:
    """Split proposals for big hubs: no HUB lists the candidates; --propose writes and prints
    one; --apply --auto-names applies it offline."""
    from keepfold import topics

    if hub is None:
        _, idx, _ = _topics(vault)
        rows = topics.split_candidates(idx, topics.tree(idx))
        typer.echo(f"split candidates (over {topics.SPLIT_N} direct pages): {len(rows)}")
        for h, n in rows:
            typer.echo(f"  {h} ({n} pages)")
        return
    if propose == apply:
        raise VaultError("invalid_argument", "Pass --propose or --apply --auto-names.")
    if apply and not auto_names:
        raise VaultError("invalid_argument", "--apply needs --auto-names; to name the children "
                         "yourself, use keepfold_reflect (pack, then action=split) in a chat.")
    v, idx, embed = _topics(vault, embed=True)
    t = topics.tree(idx)
    prop = topics.propose_split(idx, t, topics.head_stack(idx, embed), hub)
    if prop is None:
        raise VaultError("invalid_argument", f"No split proposal for '{hub}': not a hub, too "
                         f"deep, under {3 * topics.MIN_CHILD} pages, or no clear clusters.")
    if propose:
        topics.write_proposal(v, prop)
        typer.echo(f"{hub}: k={prop['k']} silhouette {prop['silhouette']:.3f} hash "
                   f"{prop['hash']}" + (f" ({prop['note']})" if prop.get("note") else ""))
        for k, c in enumerate(prop["clusters"], 1):
            typer.echo(f"  cluster {k} ({len(c['members'])} pages): {', '.join(c['terms'])}")
            for s in c["sample_titles"]:
                typer.echo(f"    - {s}")
        typer.echo(f"  stay on the parent: {len(prop['stay'])}")
        typer.echo(f"written {topics.proposal_path(v, hub)}")
        return
    res = topics.apply_split(v, idx, embed, hub, prop["hash"], topics.auto_children(prop),
                             provisional=True)
    for c in res["children"]:
        typer.echo(f"  {c['id']} ({c['n']} pages): {render.term(c['title'])}")
    typer.echo(f"created {len(res['children'])} provisional sub-topics; {res['stayed']} pages "
               "stay on the parent")


@topics_app.command("eval")
@_errors
def topics_eval(margins: Annotated[str, typer.Option(
                    help="Comma-separated margins to try.")] = "0.01,0.02,0.03",
                floor: Annotated[float | None, typer.Option(
                    help="The similarity floor (default 0.80).")] = None,
                splits: Annotated[bool, typer.Option(
                    "--splits", help="Score the hand-made splits against automatic ones "
                    "instead.")] = False,
                vault: VaultOpt = None) -> None:
    """Leave-one-out on the pages that have hubs: attached share, precision (the page's hub or
    an ancestor) and exact share per margin. --splits: silhouette, browse cost, cohesion and ARI
    of the hand-made splits against automatic ones. Read-only."""
    from keepfold import topics

    floor = topics.FLOOR if floor is None else floor
    try:
        ms = [float(m) for m in margins.split(",") if m.strip()]
    except ValueError:
        raise VaultError("invalid_argument", "margins must be numbers like 0.01,0.02.") from None
    _, idx, embed = _topics(vault, embed=True)
    if splits:
        r = topics.evaluate_splits(idx, topics.tree(idx), topics.head_stack(idx, embed))
        typer.echo(f"split hubs: {r['hubs']}; compared with an automatic split: {r['compared']}")
        typer.echo(f"manual silhouette: p25 {r['manual_sil_p25']:.3f} (SIL_MIN candidate), "
                   f"median {r['manual_sil_median']:.3f}; SIL_MIN now {topics.SIL_MIN}")
        typer.echo(f"browse reduction (median): manual {r['manual_browse']:.3f}, automatic "
                   f"{r['auto_browse']:.3f}, ratio {r['browse_ratio']:.2f} (gate >= 0.80)")
        typer.echo(f"cohesion (median): manual {r['manual_cohesion']:.3f}, automatic "
                   f"{r['auto_cohesion']:.3f}; ARI median {r['ari']:.3f}")
        return
    rows = topics.evaluate(idx, topics.tree(idx), topics.head_stack(idx, embed), ms, floor)
    typer.echo(f"pages with hubs: {rows[0]['pages'] if rows else 0}; floor {floor}")
    typer.echo(f"{'margin':>7} {'attached':>9} {'precision':>10} {'exact':>6}  depth 1/2/3+")
    for r in rows:
        d: Counter = Counter()
        for k, x in r["depth"].items():
            d[min(k, 3)] += x
        typer.echo(f"{r['margin']:>7.3f} {r['attached']:>9.3f} {r['precision']:>10.3f} "
                   f"{r['exact']:>6.3f}  {d[1]}/{d[2]}/{d[3]}")


def _share(x) -> str:
    return "-" if x is None else f"{x:.2f}"


harvest_app = typer.Typer(no_args_is_help=True, help="The imported-chat harvest.")
app.add_typer(harvest_app, name="harvest")


@harvest_app.command("status")
@eval_app.command("harvest", hidden=True)  # now `harvest status`
@_errors
def harvest_status(since: Annotated[str | None, typer.Option(
                     help="Only harvests on or after this local day (YYYY-MM-DD).")] = None,
                 json_out: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
                 vault: VaultOpt = None) -> None:
    """Read-only: how the chat harvest is going: placement coverage,
    bridged hubs, the yes-rate, the waiting backlog, the 7-day keep share, the empty rate per
    app, and each run's pages with its `keepfold undo` command."""
    from keepfold import duplicates, harvest, semantic, topics
    from keepfold.index import Index

    if since is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        raise VaultError("invalid_argument", "--since must be a day like 2026-11-01.")
    v = _vault(vault)
    idx = Index(v, Catalog.load(v))
    embed = semantic.load_embedder()
    r = harvest.status(idx, topics.tree(idx), topics.head_stack(idx, embed) if embed else None,
                       EventLog(v).iter_events(), local_day(datetime.now(UTC)), since)
    if json_out:
        typer.echo(json.dumps(r, indent=1))
        return
    if not embed:
        typer.echo(semantic.NOT_SET_UP + " (no placement coverage)", err=True)
    p, y, k = r["placement"], r["yes_rate"], r["keep"]
    typer.echo(f"chats: {r['chats']}, harvested {r['harvested']}")
    if p:
        typer.echo(f"placement coverage: {p['coverage']:.2f} ({p['harvested']} of {p['chats']} "
                   f"chats in {p['hubs']} hubs; target 0.40)")
    typer.echo(f"bridged hubs: {r['bridged_hubs']} of {r['hubs']}"
               + (f"; placement hubs {p['bridged']} of {p['hubs']} (target 30)" if p else ""))
    typer.echo(f"yes-rate: {_share(y['rate'])} ({y['yes']} yes, {y['no']} no; guard 0.50 "
               f"over 20+)")
    typer.echo(f"waiting about-me lines: {r['waiting']} (guard 12 or fewer)")
    typer.echo(f"7-day keep share: {_share(k['share'])} ({k['kept']} of {k['pages']} pages; "
               "guard 0.85)")
    for app_, e in r["empty"].items():  # app names and page ids are page/event text: escape
        typer.echo(f"empty rate {duplicates.safe(app_, 40)}: {e['rate']:.2f} ({e['empty']} of "
                   f"{e['chats']} chats)")
    for run in r["runs"]:
        pages = ", ".join(duplicates.safe(p, 80) for p in run["pages"])
        typer.echo(f"run {run['day']} {run['session']}: {run['bundles']} bundles, "
                   f"{run['chats']} chats, pages: {pages or 'none'}")
        typer.echo(f"  undo: {run['undo']}")


# ---- v6 agenda 7: portable context for other assistants ---------------------------

def _print_mcp_config(vault: Path | None) -> None:
    """The mcpServers JSON snippet for clients `setup` does not write (Cursor, Gemini CLI)."""
    from keepfold import clients

    entry = clients.server_entry(clients.mcp_command(), _vault(vault).root.resolve())
    typer.echo(json.dumps({"mcpServers": {"keepfold": entry}}, indent=2))


export_app = typer.Typer(no_args_is_help=True, help="Snapshots for other assistants.")
app.add_typer(export_app, name="export")


@export_app.command("context")
@_errors
def export_context_cmd(out: Annotated[Path | None, typer.Argument(
                           help="File to write (outside the vault and the Keepfold repo); "
                                "else stdout.")] = None,
                       mcp_config: Annotated[bool, typer.Option(  # now setup --print-mcp-config
                           "--mcp-config", hidden=True)] = False,
                       vault: VaultOpt = None) -> None:
    """Your profile, preferences, contract rules and current goals as plain Markdown to paste
    into other assistants: confirmed lines only, at most 6,000 characters, a dated snapshot."""
    from keepfold import export_context as ec

    _refuse_excluded("export")
    if mcp_config:
        return _print_mcp_config(vault)
    v = _vault(vault)
    target = ec.check_target(out, v) if out else None
    text, skipped = ec.build(v, list(EventLog(v).iter_events()), datetime.now(UTC))
    if target:
        target.write_text(text, encoding="utf-8")
        typer.echo(f"wrote {target} ({len(text)} characters)", err=True)
    else:
        typer.echo(text, nl=False)
    if skipped:
        typer.echo(f"{skipped} lines are not confirmed, so they were left out; confirm them in "
                   "/keepfold reflect to export them.", err=True)


# ---- the Keepfold app ----------------------------------------------

@app.command("app")
def app_cmd(vault: VaultOpt = None,
            no_open: Annotated[bool, typer.Option("--no-open", help="Print the link only.")]
            = False,
            port: Annotated[int, typer.Option(
                help="Port on 127.0.0.1 (0: 47615 if free, else any free one).")] = 0,
            window: Annotated[bool, typer.Option(
                "--window", help="Its own window and a tray icon (Windows), as the Start menu's "
                "Keepfold opens it; runs until Quit in the tray." if sys.platform == "win32" else
                "Its own window and a tray icon, as the Keepfold app opens it; runs until Quit "
                "in the tray.")] = False) -> None:
    """Open the Keepfold app in your browser: setup, AI apps, local model, browsing, vault.
    Runs on 127.0.0.1 only and stops after 30 minutes idle."""
    from keepfold.app import server

    if window:
        from keepfold.app import desktop
        desktop.run(vault or taint._vault_root(), hidden=no_open, port=port, echo=typer.echo)
        return
    server.run(vault or taint._vault_root(), open_browser=not no_open, port=port,
               echo=typer.echo)


# ---- browse brain: browsing, processed on this machine only ----

browse_app = typer.Typer(no_args_is_help=True, help="Your browser history as Keepfold pages, "
                         "read and filtered on this machine only.")
app.add_typer(browse_app, name="browse")
llm_app = typer.Typer(no_args_is_help=True, help="The optional local LLM (loopback only).")
app.add_typer(llm_app, name="llm")
BrowserOpt = Annotated[str | None, typer.Option(
    help="chrome, edge, brave, comet or firefox.")]
ProfileOpt = Annotated[str | None, typer.Option(
    help="A profile folder name, such as Default or 'Profile 1'.")]


def _browse_sources(browser: str | None, profile: str | None) -> list:
    from keepfold.browse import sources

    if browser is not None and browser.lower() not in sources.BROWSERS:
        raise VaultError("invalid_argument",
                         f"--browser must be one of {', '.join(sources.BROWSERS)}.")
    from dataclasses import replace

    from keepfold.browse.privacy import load_settings

    # an explicit --browser wins; otherwise `browse.browsers` in config (default: every one).
    # A named --profile is read even when unticked (`browse.skip_profiles`), but only when the
    # name is that one profile: "Default" never also reads another browser's unticked Default
    s = load_settings()
    if browser:
        return sources.pick(sources.discover(), browser, profile)
    got = sources.pick(sources.chosen(s), None, profile)
    if profile and not got:
        named = sources.pick(sources.chosen(replace(s, skip_profiles=())), None, profile)
        got = named if len(named) == 1 else []
    return got


def _local_llm(config: dict | None = None, background: bool = False):
    """The loopback runner, or None (no runner, or a bad localllm.url, said on stderr)."""
    try:
        from keepfold import localllm
        return localllm.find(config, background=background)
    except ImportError:
        return None
    except ValueError as e:  # a non-loopback localllm.url is refused
        typer.echo(f"local LLM not used: {e}", err=True)
        return None


NEXT_SCAN = ("next: `keepfold browse selftest` to see the import work on made-up data, then "
             "`keepfold browse import --dry-run`, then `keepfold browse import`")
NO_BROWSERS = ("no browser history found (looked for Chrome, Edge, Brave and Comet under "
               "%LOCALAPPDATA% and Firefox under %APPDATA%; on macOS and Linux for Chrome, "
               "Edge, Brave and Firefox in their usual folders, and on Linux also for Chromium "
               "and the snap and Flatpak folders)")
NO_MODEL = ("embedding model not downloaded: every page goes to the private page under "
            "\"Other\" sections, none under your topic hubs. To fix: keepfold model download")


@browse_app.command("selftest")
def browse_selftest() -> None:
    """The whole import on made-up history in a temp folder (Chrome, Comet, Firefox and a
    throwaway vault): never your vault or your browsers. Prints PASS/FAIL per check; exits 1
    on any failure."""
    from keepfold.browse import selftest

    if not selftest.run(typer.echo):
        raise typer.Exit(1)


@browse_app.command("scan")
@_errors
def browse_scan(browser: BrowserOpt = None, profile: ProfileOpt = None) -> None:
    """Dry run: per browser and profile, the visit and search counts, the date range, and how
    many visits the privacy filter would drop, keep private or keep. Counts only; never a
    title or URL; writes nothing."""
    from keepfold.browse import load_settings, sources

    srcs = _browse_sources(browser, profile)
    s = load_settings()
    typer.echo(f"settings: not saved {', '.join(s.skip_categories) or 'none'}; hidden from "
               f"Claude {', '.join(s.hidden_from_claude) or 'none'}; {len(s.deny)} denied, "
               f"{len(s.allow)} allowed, {len(s.private)} private domains")
    typer.echo(f"Comet found: {'yes' if any(x.browser == 'comet' for x in srcs) else 'no'}")
    if not srcs:
        typer.echo(NO_BROWSERS)
        return
    rows = sources.scan(srcs, s)
    for r in rows:
        if "error" in r:
            typer.echo(f"{r['source']}: not read: {r['why']}")
            continue
        typer.echo(f"{r['source']}: {r['visits']} visits, {r['searches']} searches, "
                   f"{r['first'] or '-'} to {r['last'] or '-'}; would drop {r['dropped']}, "
                   f"keep private {r['private']}, keep {r['kept']} ({r['maybe']} unsure)")
    if any(r.get("maybe") for r in rows):
        typer.echo("unsure: a sensitive word only; kept private like the rest")
    days = [d for r in rows for d in (r.get("first"), r.get("last")) if d]
    typer.echo(f"date range: {min(days)} to {max(days)}" if days else "date range: no visits")
    typer.echo(NEXT_SCAN)


@browse_app.command("import")
@_errors
def browse_import(since: Annotated[str | None, typer.Option(
                      help="Rebuild the days from the Monday of this local day (YYYY-MM-DD) "
                           "on, not just the days with new visits.")] = None,
                  browser: BrowserOpt = None, profile: ProfileOpt = None,
                  dry_run: Annotated[bool, typer.Option(
                      "--dry-run", help="Do everything except writing; print counts only.")]
                  = False,
                  takeout: Annotated[Path | None, typer.Option(
                      "--takeout", help="Also read a Google Takeout My Activity export (.zip "
                      "or folder): Search, YouTube and Chrome, through the same privacy "
                      "filter; rows on days your browsers already hold are skipped.")] = None,
                  vault: VaultOpt = None) -> None:
    """Write or update the browse day pages, the index, interest journal lines and "New
    interest?" inbox lines. Idempotent: each day page is rebuilt from all of that day's visits
    (from the chosen browsers only, with --browser or --profile). Prints counts only.
    The public pages and journal lines need `browse.visible_to_claude: true`; by default only
    the private pages are written. Ctrl+C is safe: pages stay whole and the next run redoes
    the same days."""
    from keepfold import semantic
    from keepfold.browse import build, load_settings, sources

    if since is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        raise VaultError("invalid_argument", "--since must be a day like 2026-11-01.")
    t0 = time.perf_counter()
    v = _vault(vault)
    google = None
    if takeout is not None:
        from keepfold.browse import takeout as takeout_mod

        google = takeout_mod.records(takeout)
        typer.echo(f"takeout: {len(google)} activity rows read"
                   + (f"; {google.skipped} row{'s' * (google.skipped != 1)} skipped "
                      "(unreadable date)" if google.skipped else ""))
    srcs = _browse_sources(browser, profile)
    if not srcs:
        typer.echo(NO_BROWSERS + "; only visits the old capture extension saved, if any, "
                   "are read")
    embed = semantic.load_embedder()
    if embed is None:
        typer.echo(NO_MODEL)
    from keepfold import localllm

    runner, budget = None, None
    if localllm.enabled():
        try:  # the model only when it is already loaded: an import never loads one
            runner = localllm.find_loaded()
        except ValueError as e:  # a non-loopback localllm.url is refused
            typer.echo(f"local LLM not used: {e}", err=True)
    if runner is not None:
        cfg = taint.config().get("localllm")
        secs = cfg.get("budget_s", 300) if isinstance(cfg, dict) else 300
        budget = localllm.Budget(secs if isinstance(secs, (int, float)) else 300)

    def progress(key, n, new, err):
        typer.echo(f"{key}: not read: {sources.why(err)}; the other sources go on" if err
                   else f"{key}: {n} visits read, {new} new")

    try:
        r = build.run(v, srcs, load_settings(), embed=embed, runner=runner, budget=budget,
                      since=since, dry_run=dry_run, progress=progress, say=typer.echo,
                      takeout=google)
    except KeyboardInterrupt:
        typer.echo("stopped: every page on disk is whole and what was read is saved; run the "
                   "import again to finish", err=True)
        raise typer.Exit(130) from None
    took = time.perf_counter() - t0
    if google is not None:
        typer.echo(f"takeout: {r.get('takeout', 0)} rows used from days your browsers do not "
                   "hold; the rest the browsers already have")
    if "days_from" not in r:
        typer.echo("nothing new since the last import"
                   + (" (dry run: nothing written)" if dry_run else ""))
        return
    typer.echo(f"visits from {r['days_from']}: kept {r.get('kept', 0)}, private "
               f"{r.get('private', 0)}, dropped by the privacy filter {r.get('dropped', 0)}")
    if dry_run:
        typer.echo(f"dry run, nothing written: would write {r.get('private_would', 0)} private "
                   f"and {r.get('public_would', 0)} public day pages, and remove up to "
                   f"{r.get('empty_would', 0)} emptied ones ({took:.1f} s)")
        return
    typer.echo(f"days written: {r.get('private_written', 0)} private pages, "
               f"{r.get('public_written', 0)} public; unchanged "
               f"{r.get('public_same', 0) + r.get('private_same', 0)}, removed "
               f"{r.get('public_removed', 0) + r.get('private_removed', 0)}")
    if refused := r.get("public_failed", 0) + r.get("private_failed", 0):
        typer.echo(f"pages the vault refused: {refused} (skipped)")
    typer.echo(f"interest lines {r.get('signals', 0)}; local model used: "
               f"{'yes' if runner is not None else 'no'}; time taken {took:.1f} s")
    if r.get("sources_failed"):
        typer.echo(f"{r['sources_failed']} source(s) not read: they keep their place, and the "
                   "next import rebuilds these days with them")
    typer.echo("visible to Claude: yes — public day pages, index and interest lines written"
               if r.get("visible_to_claude") else
               "visible to Claude: no — only private pages written (browse.visible_to_claude)")
    from keepfold.browse import sites

    sites.update_vectors(v, typer.echo)


@browse_app.command("deepen")
@_errors
def browse_deepen(budget_s: Annotated[float | None, typer.Option(
                      "--budget-s", help="Seconds the local model may spend (default: "
                                         "localllm.budget_s in config, else 300).")] = None,
                  vault: VaultOpt = None) -> None:
    """The local model's long pass over your site pages, most visited first: a category for
    sites the built-in table does not know, a one-line description, related sites and topic
    hubs. Resumable: answers are cached as they come, so the next run goes on where this one
    stopped. Needs `localllm: {enabled: true}`. Prints counts only."""
    from keepfold import browse_serve

    v = _vault(vault)
    try:
        r = browse_serve.run_deepen(v, say=typer.echo, budget_s=budget_s)
    except KeyboardInterrupt:
        typer.echo("stopped: the answers so far are saved; run deepen again to go on", err=True)
        raise typer.Exit(130) from None
    if r is None:
        typer.echo("no local model: add `localllm: {enabled: true}` to ~/.keepfold/config.yaml "
                   "and start one (see `keepfold llm status`)")


@browse_app.command("status")
@_errors
def browse_status(vault: VaultOpt = None) -> None:
    """The mode, the last import, the last read and cursor per browser and profile, and the
    local LLM it used."""
    from keepfold.browse import build, load_settings

    for line in build.status(_vault(vault), visible=load_settings().visible_to_claude):
        typer.echo(line)


@browse_app.command("forget")
@_errors
def browse_forget(site: Annotated[str | None, typer.Option(
                      "--site", help="A domain, such as example.com (its subdomains too).")]
                  = None,
                  category: Annotated[str | None, typer.Option(
                      "--category", help="A category, such as health or shopping.")] = None,
                  vault: VaultOpt = None) -> None:
    """Remove everything stored about one site or one category: the private day page lines,
    the site counts and pages, the old capture extension's saved visits, the local model's site
    notes and the search vectors. Prints counts only. Your browsers still hold recent visits:
    add the site to deny_domains (or skip the category) so the next import leaves them out.
    Old versions in the vault's git history stay (see `keepfold doctor`)."""
    from keepfold.browse import forget, sites

    if (site is None) == (category is None):
        raise VaultError("invalid_argument", "give exactly one of --site or --category.")
    if category is not None:
        category = category.strip().lower()
        if category not in sites.LABELS:
            raise VaultError("invalid_argument",
                             f"--category must be one of {', '.join(sites.LABELS)}.")
        rule, what = forget.Rule(cats=frozenset({category})), f"category {category}"
    else:
        host = (site or "").strip().lower().removeprefix("https://").removeprefix("http://")
        host = host.split("/", 1)[0].removeprefix("www.").rstrip(".")
        if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", host):
            raise VaultError("invalid_argument", "--site must be a domain like example.com.")
        rule, what = forget.Rule(hosts=(host,)), host
    from keepfold import forget as forget_

    v = _vault(vault)
    r = forget_.browse(v, rule, what)
    c = Counter(r["removed"])
    typer.echo(f"forgot {what}: {c['day_lines']} lines on {c['day_pages']} private day pages, "
               f"{c['site_keys']} site counts and {c['site_rows']} titles or searches, "
               f"{c['sites_removed']} site pages, {c['inbox_records']} saved extension visits, "
               f"{c['cache_sites'] + c['cache_descriptions']} local-model notes, "
               f"{c['vectors']} search vectors, {c['compacted']} databases compacted")
    for line in forget_.lines(r)[1:]:
        typer.echo(line)


@browse_app.command("serve")
@_errors
def browse_serve_cmd(port: Annotated[int | None, typer.Option(
                         hidden=True, help="Ignored: the receiver opens no port since 0.3.2.")]
                     = None,
                     vault: VaultOpt = None) -> None:
    """Import new browser history in the background, every browse.auto_import_hours (as Start
    at login runs it), until Ctrl+C. Opens no network port."""
    from keepfold import browse_serve

    v = _vault(vault)
    typer.echo("Keepfold browse receiver: imports new browser history in the background; "
               "Ctrl+C stops it.")
    try:
        browse_serve.serve(v)
    except KeyboardInterrupt:
        pass


@llm_app.command("status")
def llm_status() -> None:
    """Which local runner (Ollama, llama.cpp or LM Studio on loopback) answers: runner, model,
    port, whether LM Studio has it loaded, and a one-token latency test. Read-only: it never
    starts LM Studio's server or loads a model (`keepfold llm start` does). Never downloads."""
    from keepfold import localllm

    conf = taint.config()
    cfg = conf.get("localllm")
    cfg = cfg if isinstance(cfg, dict) else {}
    typer.echo(f"config: runner {cfg.get('runner') or 'auto'}, model "
               f"{cfg.get('model') or 'any'}, {'on' if localllm.enabled() else 'off'}")
    got = localllm.status(conf)  # read-only: never starts LM Studio or loads a model
    if (st := got["lmstudio"]) is not None:
        ids = ", ".join(str(r.get("identifier")) for r in st["loaded"]) or "none"
        srv = f"on port {st['port']}" if st["port"] else "off"
        typer.echo(f"LM Studio: server {srv}, loaded: {ids}")
    if got["error"]:  # a non-loopback localllm.url or a cloud model is refused
        typer.echo(f"local LLM not used: {got['error']}", err=True)
    if (runner := got["runner"]) is None:
        typer.echo("no local LLM found (Ollama on 127.0.0.1:11434, LM Studio with "
                   "localllm.runner: lmstudio, or llama.cpp / LM Studio at localllm.url, holding "
                   "localllm.model if set). Optional; to add one, install Ollama and run: "
                   "ollama pull qwen3.5:4b (qwen3.5:2b under 6 GB of memory). Installed but "
                   "stopped? Run `keepfold llm start`.")
        return
    typer.echo(f"found: {runner.describe()}")
    if not localllm.enabled():
        typer.echo("not used by `keepfold browse import` until you add `localllm: {enabled: true}` "
                   "to ~/.keepfold/config.yaml")
    if st is not None and st["port"]:
        on = any(runner.model.lower() == str(r.get("identifier", "")).lower()
                 for r in st["loaded"])
        typer.echo(f"loaded in LM Studio: {'yes' if on else 'no'}")
        if not on:  # a ping would make LM Studio load it
            typer.echo("latency: not tested; run `keepfold llm start` to load it")
            return
    lat = runner.ping()
    typer.echo(f"latency (1 token): {lat:.2f} s" if lat is not None else "latency: no answer")


@llm_app.command("start")
@_errors
def llm_start(runner: Annotated[str | None, typer.Option(
        help="ollama (recommended) or lmstudio; default: localllm.runner, else ollama.")]
        = None) -> None:
    """Start an installed local runner: Ollama (its app, else `ollama serve`) or LM Studio
    (`lms server start`), and wait until it answers. Never installs or downloads."""
    from keepfold.app import wizard

    if runner is None:
        cfg = taint.config().get("localllm")
        runner = cfg.get("runner") if isinstance(cfg, dict) else None
        runner = runner if runner in wizard.RUNNERS else "ollama"
    try:
        out = wizard.start(runner)
    except ValueError as e:
        raise VaultError("invalid_argument", str(e)) from None
    for line in out["lines"]:
        typer.echo(line)
    if not out["ok"]:
        raise typer.Exit(1)


@llm_app.command("test")
def llm_test() -> None:
    """Send the runner one synthetic prompt (never your data) and print its answer and time."""
    runner = _local_llm()
    if runner is None:
        typer.echo("no local LLM found; see `keepfold llm status`")
        raise typer.Exit(1)
    typer.echo(f"runner: {runner.describe()}")
    text, labels = "Dijkstra shortest path tutorial", ["programming", "cooking", "sports"]
    typer.echo(f"classify: {text!r} as one of {', '.join(labels)}")
    t = time.perf_counter()
    got = runner.classify(text, labels)
    took = time.perf_counter() - t
    typer.echo(f"answer: {got[0]} (confidence {got[1]:.2f})" if got else "answer: none valid")
    typer.echo(f"time: {took:.2f} s")
