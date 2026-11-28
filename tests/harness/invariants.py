"""The vault invariant checker (D21 §2.3): what must hold after any run, a crashed one included.
Only `keepfold` and the stdlib, so the soak and VM evidence scripts can reuse it.

check(root, receipts, since) -> a list of problems, empty when the vault is sound:
- every receipt's file (a writer logged it as acknowledged: path, sha256 after the write) is
  there with that sha256, and parses (a note's front matter, JSONL lines, JSON);
- every events/**/*.jsonl line is whole: newline-terminated JSON objects;
- no `.*.tmp` (vault._atomic_write's temp file) older than `since` (the run's start) is left;
- the vault lock is free: no lock file, or one Keepfold would take as stale (a dead holder);
- `keepfold replay` is stable across two runs: the second changes nothing.

The vault itself is only read: the lock and replay checks run on a copy, so a held lock or a
replay that stamps notes never changes the tree a test goes on to inspect. The replay runs in a
child process with this process's environment, so the caller isolates HOME first (conftest's
`home` fixture)."""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from keepfold import levels
from keepfold.vault import Vault, VaultError

REPLAY = [sys.executable, "-I", "-c", "from keepfold.cli import app; app()", "replay", "--vault"]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jsonl_problems(path: Path, rel: str) -> list[str]:
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        return [f"{rel}: torn last line (no newline)"]
    out = []
    for i, line in enumerate(data.splitlines(), 1):
        try:
            if not isinstance(json.loads(line), dict):
                raise ValueError("not an object")
        except ValueError as e:
            out.append(f"{rel}:{i}: not a whole JSON object ({e})")
    return out


def _parse_problems(vault: Vault, path: Path, rel: str) -> list[str]:
    try:
        if path.suffix == ".md":
            vault.read_path(path)
        elif path.suffix == ".jsonl":
            return _jsonl_problems(path, rel)
        elif path.suffix == ".json":
            json.loads(path.read_bytes())
    except (VaultError, ValueError) as e:
        return [f"{rel}: does not parse ({e})"]
    return []


def _replay(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run([*REPLAY, str(root)], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)


def check(root: Path, receipts: list[tuple[str, str]] = (),
          since: float | None = None) -> list[str]:
    """`receipts`: (path, sha256) pairs, the path vault-relative or absolute. `since`: a
    time.time() from the run's start; None means any temp file is a leftover."""
    root = Path(root).resolve()
    problems: list[str] = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        copy = Path(tmp) / "vault"
        shutil.copytree(root, copy, symlinks=True)
        vault = Vault(copy)

        for path, digest in receipts:
            rel = Path(path).resolve().relative_to(root) if Path(path).is_absolute() else path
            p = copy / rel
            if not p.is_file():
                problems.append(f"{Path(rel).as_posix()}: receipt's file is missing")
                continue
            if sha256(p) != digest:
                problems.append(f"{Path(rel).as_posix()}: sha256 differs from the receipt")
            problems += _parse_problems(vault, p, Path(rel).as_posix())

        for p in sorted((root / "events").rglob("*.jsonl")):
            problems += _jsonl_problems(p, p.relative_to(root).as_posix())

        for p in sorted(root.rglob(".*.tmp")):
            if since is None or p.stat().st_mtime < since:
                problems.append(f"{p.relative_to(root).as_posix()}: temp file left behind")

        try:
            with vault.lock(timeout=1.0):  # takes a dead holder's lock as Keepfold would
                pass
        except VaultError:
            problems.append(".keepfold/lock: held by a live process")
            return problems  # replay would wait on the same lock

        # replay prints catalog-sync lines, a count per state, then "N note(s) updated"; the
        # second run must sync nothing and update nothing, with the same counts
        first, second = _replay(copy), _replay(copy)
        if first.returncode or second.returncode:
            bad = first if first.returncode else second
            problems.append(f"keepfold replay failed: {bad.stdout}{bad.stderr}")
        elif second.stdout.splitlines() != [
                *first.stdout.splitlines()[-len(levels.STATES) - 1:-1], "0 note(s) updated"]:
            problems.append(f"keepfold replay is not stable: the first run said\n{first.stdout}"
                            f"the second\n{second.stdout}")
    return problems
