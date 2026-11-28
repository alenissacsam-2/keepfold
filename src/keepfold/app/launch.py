"""The no-console entry points of the Windows install (gui-scripts in pyproject.toml).

The installer's shortcut runs `Keepfold.exe -m keepfold.app.launch` (installer/app_exe.py: a copy
of pythonw named and drawn as Keepfold, so Windows shows Keepfold, not Python); `keepfold-app` runs
the same, handing over to that Keepfold.exe when it sits beside its Python. It is the desktop
app (desktop.py: its own window and a tray icon; `--hidden`, as Start with Windows runs it,
starts in the tray), or, when one already runs (~/.keepfold/app.json), that app's window
brought up. `keepfold-receiver` is Start at login's browse receiver (the background history
import), as `keepfold browse serve` runs it. With no console (a GUI script's sys.stdout and
sys.stderr are None) their output goes to ~/.keepfold/logs/, and a failed app start says so in a
message box with the log's path."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

LOG_MAX = 1_000_000  # bytes; a bigger log starts over
APP_EXE = "Keepfold.exe"  # installer/app_exe.py NAME


def installed_app() -> Path | None:
    """The install's Keepfold.exe (the tool venv's app\\, installer/app_exe.py), else None (a
    dev install)."""
    exe = Path(sys.prefix) / "app" / APP_EXE
    return exe if exe.is_file() else None


_KEY = re.compile(r"\?t=[\w-]+")


def _tty() -> bool:  # tests replace this
    return sys.stderr is not None and sys.stderr.isatty()


def _log_to(name: str) -> Path:
    log = Path.home() / ".keepfold" / "logs" / name
    # off Windows a dock or app-menu start has streams, but nobody reads them
    if sys.stdout is None or sys.stderr is None or (sys.platform != "win32" and not _tty()):
        log.parent.mkdir(parents=True, exist_ok=True)
        big = log.is_file() and log.stat().st_size > LOG_MAX
        sys.stdout = sys.stderr = open(log, "w" if big else "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    return log


def _quote_applescript(s: str) -> str:
    """`s` as an AppleScript string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run_alert(argv: list[str]) -> None:  # tests replace this
    from keepfold._proc import NO_WINDOW
    subprocess.run(argv, check=False, creationflags=NO_WINDOW)  # an argv list: no shell


def _alert(text: str) -> None:  # tests replace this
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "Keepfold", 0x10)  # MB_ICONERROR
        return
    argv = None
    if sys.platform == "darwin":
        argv = ["osascript", "-e",
                f'display alert "Keepfold" message {_quote_applescript(text)} as critical']
    elif sys.platform.startswith("linux"):
        if shutil.which("zenity"):
            argv = ["zenity", "--error", "--text", text]
        elif shutil.which("notify-send"):
            argv = ["notify-send", "Keepfold", text]
    if argv:
        try:
            _run_alert(argv)
            return
        except OSError:
            pass
    print(text, file=sys.stderr)


def main() -> None:
    from keepfold import taint
    taint.cap_blas()
    log = None
    hidden = "--hidden" in sys.argv[1:]
    from keepfold import migrate  # before the log: ~/.cortex moves while ~/.keepfold is missing
    if why := migrate.launch():
        _alert(f"Keepfold could not start yet.\n\n{why}")
        raise SystemExit(2)
    try:
        log = _log_to("app.log")
        exe = installed_app()
        if exe and Path(sys.executable).resolve() != exe.resolve():
            # an older shortcut or Start with Windows value: the window must belong to Keepfold.exe
            from keepfold._proc import NO_WINDOW
            subprocess.Popen([str(exe), "-m", "keepfold.app.launch", *sys.argv[1:]],
                             creationflags=NO_WINDOW)
            return
        from keepfold.app import server
        if url := server.running_url():  # double-clicked again: the open app, not a second one
            if hidden:  # Start with Windows while it runs already
                return
            print("Keepfold app: already running; showing it")
            if sys.platform == "win32":  # the running app may bring its window to the front
                import ctypes
                ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
            if not server.show_window(url):  # the browser app has no window
                webbrowser.open(url)
            return
        # clients.keepfold_command() finds `keepfold` on PATH for the AI-app configs; a shortcut
        # can start before PATH has the installer's bin folder (with `keepfold.exe`), next to the
        # keepfold-app.exe that started us in a dev install
        from keepfold import update
        d = update.install_dir()
        here = str(d / "bin") if d else str(Path(sys.argv[0]).resolve().parent)
        os.environ["PATH"] = here + os.pathsep + os.environ.get("PATH", "")
        from keepfold.app import desktop
        # the log keeps the address, never the key (?t=): the page holds it, the log may be shared
        desktop.run(taint._vault_root(), hidden=hidden, echo=lambda s: print(_KEY.sub("", s)),
                    smoke="--smoke" in sys.argv[1:])  # hidden: release-apps.yml's GUI check
    except Exception:  # noqa: BLE001  (a double-click must never fail silently)
        from keepfold.secret_rules import log_exc  # here: a crash while importing still logs
        log_exc()
        _alert("Keepfold could not start.\n\nWhat went wrong is written in "
               f"{log or Path.home() / '.keepfold' / 'logs' / 'app.log'}")
        raise SystemExit(1) from None


def receiver() -> None:
    """`keepfold-receiver [--vault V]`: `keepfold browse serve` (the background import) with no
    console."""
    from keepfold import taint
    taint.cap_blas()
    from keepfold import migrate
    if migrate.launch():  # an old Cortex still holds the files: the next login tries again
        raise SystemExit(2)
    _log_to("receiver.log")
    from keepfold import cli
    cli.app(["browse", "serve", *sys.argv[1:]], prog_name="keepfold")


if __name__ == "__main__":  # Keepfold.exe -m keepfold.app.launch [--hidden]
    main()
