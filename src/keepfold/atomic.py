"""One temp-file-then-os.replace write, for every data file Keepfold keeps. Stdlib only: the
vault, the config, the AI apps' rule files and the app's state files all go through it."""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

REPLACE_TRIES = 8  # PermissionError (Windows: another program holds the file) is retried this often


def replace(path: Path, data: str | bytes, *, fsync: bool = True, encoding: str = "utf-8",
            newline: str | None = "", guard: Callable[[], bool] | None = None) -> bool:
    """Write `data` to a temp file beside `path` (`.<name>.*.tmp`), flush, fsync (unless
    `fsync=False`: for hot paths that never synced), then os.replace it over `path`, retrying
    a PermissionError (sleeps 0.05 s doubling; the last one is raised). The temp is removed on
    any failure. `encoding` and `newline` apply to str data (the open() meanings; "" writes
    \\n as is). `guard`, called after the temp is written and before the replace, returns
    False to leave `path` alone: the function then returns False (else True)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with (os.fdopen(fd, "wb") if isinstance(data, bytes)
              else os.fdopen(fd, "w", encoding=encoding, newline=newline)) as f:
            f.write(data)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        if guard is not None and not guard():
            return False
        for attempt in range(REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:
                if attempt == REPLACE_TRIES - 1:
                    raise
                time.sleep(0.05 * 2**attempt)
        return True  # unreachable
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
