"""Install smoke for the built KeepfoldSetup.exe (the `installer` job of installer.yml).

`uv run python scripts/smoke_install.py` installs dist\\KeepfoldSetup.exe quietly into a folder with
a non-ASCII, spaced name, runs the installed `keepfold --version`, `init` on a temp vault and the
MCP handshake of packaging/smoke_mcp.py, then uninstalls and checks that the folder, the Run
values, the Apps & Features entry and the PATH entry are gone.

It refuses to run unless the CI env var is set: setup writes HKCU, shortcuts and the user PATH,
which on a developer's PC would change a live install. Windows only."""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETUP = ROOT / "dist" / "KeepfoldSetup.exe"
INSTALL_DIR = Path(r"C:\ci") / "Keepfold Tést"
LOG = Path(tempfile.gettempdir()) / "keepfold-setup.log"  # installer/setup_app.py LOG
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Keepfold"
RUN_VALUES = ("Keepfold", "Keepfold browse receiver")
GONE_WAIT_S = 60  # the uninstaller deletes its own exe and the folder after it exits (<= 30 s)


def setup_cmd(exe: Path, d: Path, *, uninstall: bool = False) -> list[str]:
    return [str(exe), *(["--uninstall"] if uninstall else []), "--quiet", "--dir", str(d)]


def run_setup(cmd: list[str]) -> None:
    rc = subprocess.run(cmd).returncode  # a windowed exe: it has no stdout, its log is LOG
    if LOG.is_file():
        print(LOG.read_text(encoding="utf-8", errors="replace"))
    if rc:
        raise SystemExit(f"{cmd[0]} exited {rc}")


def registry_has(path: str, name: str | None = None) -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            if name is not None:
                winreg.QueryValueEx(k, name)
        return True
    except FileNotFoundError:
        return False


def user_path() -> str:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, "Path")[0]
    except FileNotFoundError:
        return ""


def smoke(setup: Path = SETUP, d: Path = INSTALL_DIR) -> None:
    if not setup.is_file():
        raise SystemExit(f"{setup} is missing: run `uv run python installer/build.py` first")
    # the installed program and the MCP smoke get their own HOME, so nothing lands in the runner's
    with tempfile.TemporaryDirectory(prefix="keepfold-smoke-") as tmp:
        env = {**os.environ, "USERPROFILE": tmp, "HOME": tmp, "PYTHONIOENCODING": "utf-8"}
        vault, cli = Path(tmp) / "vault", d / "bin" / "keepfold.exe"
        run_setup(setup_cmd(setup, d))
        if not cli.is_file():
            raise SystemExit(f"{cli} was not installed")
        if str(d / "bin") not in user_path():
            raise SystemExit("the install folder's bin is not on the user PATH")
        for cmd in ([cli, "--version"], [cli, "init", vault],
                    [sys.executable, ROOT / "packaging" / "smoke_mcp.py", cli, vault]):
            subprocess.run([str(c) for c in cmd], env=env, check=True, timeout=300)
        run_setup(setup_cmd(d / "KeepfoldSetup.exe", d, uninstall=True))
    deadline = time.monotonic() + GONE_WAIT_S
    while d.exists() and time.monotonic() < deadline:
        time.sleep(1)
    left = [f"folder {d}"] * d.exists() + [f"Run value {n}" for n in RUN_VALUES
                                           if registry_has(RUN_KEY, n)]
    left += ["Apps & Features entry"] * registry_has(UNINSTALL_KEY)
    left += ["PATH entry"] * (str(d / "bin") in user_path())
    if left:
        raise SystemExit("uninstall left behind: " + ", ".join(left))
    print("install smoke OK")


def main() -> int:
    if not os.environ.get("CI"):
        print("refusing to run: this installs Keepfold for the current user (HKCU, shortcuts, "
              "PATH). It is for CI runners only.", file=sys.stderr)
        return 2
    if sys.platform != "win32":
        print("smoke_install.py is Windows only", file=sys.stderr)
        return 2
    smoke()
    return 0


if __name__ == "__main__":
    sys.exit(main())
