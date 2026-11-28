"""The vault format stamp, `<vault>/.keepfold/format.json` (D7 §1.3, §2.1, §2.6), and the write
gate every vault lock passes (`Vault._take`). Stdlib only at import: `vault` imports this, and
the hook loads every `Vault`.

States: `current` (a stamp this code may write), `needs_upgrade` (no stamp yet: still
writable, the stamp comes from `keepfold init` or the app), `too_new` (a newer Keepfold's
format: read-only here) and `unreadable` (a stamp that will not parse: read-only, run doctor).
There is no migration engine yet, so `upgrading` never occurs."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

CODE_FORMAT = 1  # the highest vault format this code may write
STAMP = ".keepfold/format.json"
# init and the app stamp a vault that has none (tests turn it off: conftest)
AUTO_STAMP = True
READ_TRIES, READ_GAP_S = 3, 0.25  # an unparseable stamp is read 3 times over 0.5 s (sync, AV)
DIR_RE = re.compile(r"[a-z0-9_][a-z0-9_.-]*(/[a-z0-9_][a-z0-9_.-]*)*")


@dataclass(frozen=True)
class State:
    status: str  # current, needs_upgrade, too_new or unreadable
    vault_format: int | None
    detail: str  # one plain line for Status, doctor and MCP
    stamp: dict = field(default_factory=dict, compare=False)


_CACHE: dict[str, tuple[tuple | None, State]] = {}
_CACHE_LOCK = threading.Lock()


def _version() -> str:
    from importlib.metadata import version
    return version("keepfold")


def _key(path: Path) -> tuple | None:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _parse(path: Path) -> dict | None:
    """The stamp as a dict with an int `format` >= 1; None when it will not parse as one."""
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return None
    f = data.get("format") if isinstance(data, dict) else None
    return data if isinstance(f, int) and not isinstance(f, bool) and f >= 1 else None


def _read_state(path: Path, key: tuple | None) -> State:
    if key is None:
        return State("needs_upgrade", None, "This vault has no format stamp yet; Keepfold adds "
                     "one the next time the app or `keepfold init` opens it.")
    for attempt in range(READ_TRIES):
        if (data := _parse(path)) is not None:
            break
        if attempt < READ_TRIES - 1:
            time.sleep(READ_GAP_S)
    else:
        return State("unreadable", None, f"The vault's format file ({STAMP}) cannot be read, "
                     "so Keepfold will not change these notes.")
    n = data["format"]
    if n > CODE_FORMAT:
        return State("too_new", n, f"These notes were changed by a newer Keepfold (vault format "
                     f"{n}). This Keepfold ({_version()}) can read them but will not change them.",
                     data)
    return State("current", n, f"Vault format {n}.", data)


def state(root) -> State:
    """The vault's format state, cached by the stamp's (mtime_ns, size): one stat per call."""
    path = Path(root) / STAMP
    key = _key(path)
    hit = _CACHE.get(str(path))
    if hit is not None and hit[0] == key:
        return hit[1]
    st = _read_state(path, key)
    with _CACHE_LOCK:
        _CACHE[str(path)] = (key, st)
    return st


def check_writable(root) -> None:
    """VaultError `vault_too_new` or `format_unreadable` when this code must not write `root`."""
    st = state(root)
    if st.status in ("current", "needs_upgrade"):
        return
    from keepfold.vault import VaultError  # here: vault imports this module
    raise VaultError("vault_too_new" if st.status == "too_new" else "format_unreadable",
                     st.detail)


def declared_dirs(root) -> list[str]:
    """Every page folder the stamp declares (`dirs`, any kind, even one this code does not
    know), vault-relative; folders that are not plain relative names are dropped."""
    dirs = state(root).stamp.get("dirs")
    if not isinstance(dirs, dict):
        return []
    return [d for ds in dirs.values() if isinstance(ds, list) for d in ds
            if isinstance(d, str) and DIR_RE.fullmatch(d) and ".." not in d.split("/")]


def write_stamp(vault, *, created: bool = False) -> bool:
    """Stamp a vault that has no format.json: a `change` event with payload note "format" in the
    log first, then the file (D7 §1.3). When the file is gone but such an event exists, the file
    is rebuilt from the newest one (same vault_id, no new event). `created`: `keepfold init`
    made this vault, so it records this version; else "pre-1.0". False when a stamp exists.
    The vault lock runs check_writable, so a too_new or unreadable vault raises VaultError."""
    from ulid import ULID

    from keepfold import atomic
    from keepfold.events import EventLog

    path = vault.root / STAMP
    with vault.lock():
        if path.exists():
            return False
        log = EventLog(vault)
        old = next((e["payload"] for e in reversed(list(log.iter_events("cli")))
                    if e.get("kind") == "change" and isinstance(e.get("payload"), dict)
                    and e["payload"].get("note") == "format"), None)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        by = _version()
        stamp = {"format": CODE_FORMAT,
                 "vault_id": (old or {}).get("vault_id") or str(ULID()),
                 "created_by": by if created else "pre-1.0",
                 "created_at": now if created else None,
                 "last_migrated_by": None, "last_migrated_at": None,
                 "applied": list(old.get("applied") or []) if old else [],
                 "dirs": {"resource": ["archive/chats"]},
                 "restored_from": None}
        if old is None:
            log.append(session="cli", kind="change", source="tutor", fields={"payload": {
                "note": "format", "format": CODE_FORMAT, "vault_id": stamp["vault_id"],
                "applied": stamp["applied"], "by": by}})
        atomic.replace(path, json.dumps(stamp, indent=2) + "\n")
        vault._track(path)
    return True
