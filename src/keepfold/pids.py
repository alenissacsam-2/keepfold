"""Whether a process id names a live process. Stdlib only: the app, the vault lock and (later)
the hook's brain client all ask it."""

from __future__ import annotations

import os


def alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if not 0 < pid < 2**31:  # 0 and -n name process groups on POSIX, Idle on Windows
        return False
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
