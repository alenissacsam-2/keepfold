"""`keepfold taint`: the plugin's PostToolUse hook records that Claude read outside text with a
tool that is not Keepfold (web, browser, mail, other MCP servers). The MCP server treats a fresh
marker as exposure (session.SessionManager._check_taint), so claim tiers see it. R7: Codex
CLI's PostToolUse and Gemini CLI's AfterTool hooks (keepfold.app.targets) run it the same way.

This module is also the `keepfold` console entry: `keepfold taint` runs here without importing
keepfold.cli (typer and every core module), since the hook fires on each matched tool call.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

MARKER = ".keepfold/taint"
# Keepfold's own servers, the Claude desktop app's one-word `ccd_*` servers (not ccd_session_mgmt:
# it returns other chats' text), widgets, schedules and the savvy-progress bar, whose tools
# return no outside text (review r4 M4).
# Desktop Commander, Windows-MCP, browsers and connectors still count. plugin/hooks/hooks.json's
# matcher skips the same names (tests/test_taint.py checks they agree); this is the backstop.
EXCLUDED = re.compile(
    r"^mcp__(keepfold|plugin_keepfold_keepfold|ccd_[^_]*|visualize|scheduled-tasks|"
    r"savvy-progress)__")
# R7: Keepfold's own tools as Gemini CLI names them (`mcp_<server>_<tool>`); its AfterTool hook's
# matcher skips them too. Codex CLI names MCP tools as Claude Code does (EXCLUDED covers it).
GEMINI_OWN = re.compile(r"^mcp_keepfold_")


def _config_path() -> Path:
    return Path.home() / ".keepfold" / "config.yaml"


# a config pinned for one thread's work (keepfold.chat_eval): config() returns it there instead of
# the file, and write_config refuses, so a made-up run never reads or changes the owner's settings
_PINNED: ContextVar[dict | None] = ContextVar("keepfold_pinned_config", default=None)


@contextmanager
def pinned(data: dict):
    """Within the block, in this thread (and context) only, config() is a copy of `data`."""
    token = _PINNED.set(data)
    try:
        yield
    finally:
        _PINNED.reset(token)


CONFIG_FIX = ("Fix that line (the file is YAML: `key: value` lines), or delete the file to start "
              "from the defaults. Deleting forgets every setting in it: the vault folder, the "
              "excluded folders, the browsing choices and the chat import switch; then set "
              "Keepfold up again (the app's Setup, or `keepfold setup`), pick the same vault "
              "folder and add your exclusions back. Until then every folder counts as excluded "
              "and the chat and browsing imports are off.")


def _unreadable(path: Path, e: Exception) -> str:
    """Why `path` cannot be read: the error's type and line only, since its text quotes the
    line, which could hold anything."""
    mark = getattr(e, "problem_mark", None) or getattr(e, "context_mark", None)
    at = f" at line {mark.line + 1}" if mark is not None else ""
    return f"{path} cannot be read: {type(e).__name__}{at}"


def _read_text(path: Path) -> str:
    """The file's text. A PermissionError (Windows, while write_config's os.replace holds the
    file) is retried twice before it counts as unreadable."""
    import time
    for wait in (0.05, 0.2):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            time.sleep(wait)
    return path.read_text(encoding="utf-8")


def _read() -> tuple[dict, str | None]:
    """(config, None); ({}, why) when the file is there but cannot be read; ({}, None) when it
    is missing. `pinned`'s dict within that block."""
    if (p := _PINNED.get()) is not None:
        import copy
        return copy.deepcopy(p), None
    from ruamel.yaml import YAML, YAMLError  # keepfold.vault loads it anyway
    path = _config_path()
    try:
        data = YAML(typ="safe").load(_read_text(path)) or {}
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError, YAMLError) as e:
        return {}, _unreadable(path, e)
    if not isinstance(data, dict):
        return {}, f"{path} cannot be read: it is not `key: value` lines"
    return data, None


def config() -> dict:
    """~/.keepfold/config.yaml as a dict ({} when missing or broken: `config_error` tells them
    apart); `pinned`'s dict within it."""
    return _read()[0]


def config_error() -> str | None:
    """Why ~/.keepfold/config.yaml is there but cannot be read (path, error type, line), else
    None. While it is set, privacy choices fail closed: `excluded` is every folder, and the
    chat and browsing imports refuse (`check_config`)."""
    return _read()[1]


def check_config(what: str) -> None:
    """Refuse `what` while the config cannot be read: on the defaults it would take in what the
    user excluded."""
    if err := config_error():
        from keepfold.vault import VaultError
        raise VaultError("invalid_argument", f"{err}, so {what} is off until it is fixed; the "
                         "app's Status page and `keepfold doctor` say how.")


def write_config(data: dict | Callable[[dict], dict]) -> Path:
    """Set `data`'s keys in ~/.keepfold/config.yaml, keeping its other keys. `data` may be a
    function: it gets the config as read under the lock and returns the keys to set, so a
    read-merge-write of a sub-dict (`browse:`, `app:`) cannot lose a concurrent writer's change.
    Refuses to overwrite a config that exists, is not empty and does not parse to a dict (a merge
    with {} would wipe `vault:` and `exclude:`). Under ~/.keepfold/config.lock: temp file, then
    os.replace with retry (atomic.replace); an unchanged config is not rewritten."""
    import copy
    import io

    from ruamel.yaml import YAML, YAMLError

    from keepfold import atomic
    from keepfold.vault import LOCK_STALE_S, VaultError

    if _PINNED.get() is not None:
        raise VaultError("invalid_argument", "Settings cannot change during a pinned run.")
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name("config.lock")
    deadline = time.monotonic() + 5
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except (FileExistsError, PermissionError) as e:
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE_S:  # its writer died
                    lock.unlink()
                    continue
            except OSError:
                pass  # gone, or never creatable (R2-5): wait out the deadline, never spin
            if time.monotonic() >= deadline:
                why = ("is held by another Keepfold process" if lock.exists()
                       else f"cannot be created ({e.strerror or e})")
                raise VaultError("vault_locked", f"{lock} {why}; try again in a moment.") from None
            time.sleep(0.05)
    try:
        try:
            old = YAML(typ="safe").load(path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            old = {}
        except (OSError, ValueError, YAMLError) as e:
            raise VaultError("invalid_argument", f"{_unreadable(path, e)}; fix it (UTF-8 YAML "
                             "with key: value lines) or delete it, then try again.") from e
        if not isinstance(old, dict):
            raise VaultError("invalid_argument", f"{path} is not a key: value mapping; fix it "
                             "or delete it, then try again.")
        # a copy: a function that edits a sub-dict in place must not make `new == old`
        new = {**old, **(data(copy.deepcopy(old)) if callable(data) else data)}
        if new == old:
            return path
        buf = io.StringIO()
        YAML(typ="safe").dump(new, buf)
        atomic.replace(path, buf.getvalue(), newline=None)  # newline=None: as the text-mode open
    finally:
        lock.unlink(missing_ok=True)
    return path


def _same_dir(a, b) -> bool:
    def norm(p):
        return os.path.normcase(str(Path(p).expanduser().resolve()))
    return norm(a) == norm(b)


def _vault_root() -> Path | None:
    """The vault the hooks write to: the configured one. KEEPFOLD_VAULT counts only when it names
    that vault or none is configured, so a project's env cannot point the hooks at its own
    folder (review RR-16)."""
    v = config().get("vault")
    env = os.environ.get("KEEPFOLD_VAULT")
    if env and (not v or _same_dir(env, v)):
        return Path(env)
    return Path(v) if v else None


def exclude_list(cfg: dict | None = None) -> list[str]:
    ex = (config() if cfg is None else cfg).get("exclude") or []
    return [str(e) for e in ([ex] if isinstance(ex, str) else ex) if e]


def excluded(*dirs) -> str | None:
    """The `exclude:` folder of ~/.keepfold/config.yaml that holds one of `dirs` (resolved, by
    whole path components; case-insensitive on Windows), or None. While the config cannot be
    read, every folder is excluded: the first of `dirs`, with the reason."""
    cfg, err = _read()
    if err:  # fail closed: the unreadable file may list exclusions
        d = next((d for d in dirs if d), None)
        return f"{Path(d).resolve()} (every folder is, while {err})" if d else None
    folders = [Path(e).expanduser().resolve() for e in exclude_list(cfg)]
    for d in dirs:
        if d:
            p = Path(d).resolve()
            for f in folders:
                if p == f or f in p.parents:
                    return str(f)
    return None


def session_dirs(cwd=None) -> tuple:
    """The folders a Claude Code session runs in: `cwd` (the hook payload's, else the process's)
    and CLAUDE_PROJECT_DIR when Claude Code sets it."""
    return cwd or os.getcwd(), os.environ.get("CLAUDE_PROJECT_DIR")


def run(raw: str) -> bool:
    """Write the marker for one hook payload; True if written. Never raises: a hook must not
    fail the tool call it follows."""
    try:
        hook = json.loads(raw)
        tool = hook.get("tool_name")
        if not isinstance(tool, str) or not tool or EXCLUDED.match(tool) or GEMINI_OWN.match(
                tool):
            return False
        root = _vault_root()
        if root is None or not root.is_dir():
            return False
        from keepfold.vault import Vault
        sid = hook.get("session_id")
        Vault(root).write_text(root / MARKER, json.dumps(
            {"ts": time.time(), "tool_name": tool[:200],
             "session_id": sid[:200] if isinstance(sid, str) else None}) + "\n")
        return True
    except Exception:  # noqa: BLE001  (garbage stdin, a busy vault lock, a secret-scan refusal)
        return False


def read_stdin() -> str:
    try:
        return sys.stdin.buffer.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001  (no stdin, or a text-only stream)
        return ""


BLAS_THREADS = "4"  # numpy's OpenBLAS commits ~32 MB per thread (docs/benchmarks/memory.md)


def cap_blas() -> None:
    """Every Keepfold process's entry point, before numpy loads: OpenBLAS's threads capped at
    BLAS_THREADS, unless the user set OPENBLAS_NUM_THREADS or OMP_NUM_THREADS (OpenBLAS reads
    either). Processes Keepfold starts inherit it."""
    if not (os.environ.get("OPENBLAS_NUM_THREADS") or os.environ.get("OMP_NUM_THREADS")):
        os.environ["OPENBLAS_NUM_THREADS"] = BLAS_THREADS


def main() -> None:
    """The `keepfold` console script."""
    cap_blas()
    from keepfold import migrate  # first: ~/.cortex moves before anything reads the home
    if (code := migrate.at_start(sys.argv)) is not None:
        raise SystemExit(code)
    # R7: `--client codex|gemini` (keepfold.app.targets writes it into those apps' hooks)
    client = sys.argv[3] if sys.argv[2:3] == ["--client"] and len(sys.argv) > 3 else None
    if sys.argv[1:2] == ["taint"]:
        run(read_stdin())
        return  # exit 0, nothing on stdout
    if sys.argv[1:2] == ["prime-hook"]:  # fires on every prompt: skip the CLI import too
        from keepfold import prime_hook
        if out := prime_hook.run(read_stdin(), *([client] if client else [])):
            sys.stdout.write(out)
        if prime_hook.pending is not None and prime_hook.pending.is_alive():
            sys.stdout.flush()  # a prime still running past its budget must not hold the exit
            os._exit(0)
        return
    from keepfold.cli import app
    app()
