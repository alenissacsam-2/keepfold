"""Build dist/KeepfoldSetup.exe (Windows only): `uv run python installer/build.py`.

uv build --wheel, then stage the wheel, uv.lock's versions as constraints.txt and this machine's
uv.exe as the exe's payload, then PyInstaller (fetched by uvx at build time, never a project
dependency) packs installer/setup_app.py into one windowed exe, with its checksum beside it
(KeepfoldSetup.exe.sha256, which the app's updater checks). It also writes
dist/keepfold-skill.zip (the Chat skill) and its keepfold-skill.zip copy for the release. Staging
lives in dist/ (git-ignored)."""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYINSTALLER = "pyinstaller==6.22.3"
# the icon app_exe stamps on Keepfold.exe (the wheel's keepfold/assets/keepfold.ico): the
# setup's too
ICON = ROOT / "src" / "keepfold" / "assets" / "keepfold.ico"


def data(payload: Path) -> list[str]:
    """PyInstaller's payload and icon options: the payload, Keepfold's icon beside it for the
    window (setup_app.icon_path), and the same icon on KeepfoldSetup.exe itself (Explorer)."""
    return ["--add-data", f"{payload}{os.pathsep}payload", "--add-data", f"{ICON}{os.pathsep}.",
            "--icon", str(ICON)]


def is_x64(exe: Path) -> bool:
    """The PE header's machine field is AMD64 (0x8664): the setup runs this uv on every PC."""
    b = exe.read_bytes()[:4096]
    pe = int.from_bytes(b[0x3C:0x40], "little")
    return b[:2] == b"MZ" and b[pe:pe + 4] == b"PE\0\0" and b[pe + 4:pe + 6] == b"\x64\x86"


def main() -> None:
    if sys.platform != "win32":
        sys.exit("KeepfoldSetup.exe builds on Windows only.")
    uv = shutil.which("uv") or sys.exit("uv is not on PATH.")
    if not is_x64(Path(uv)):
        sys.exit(f"{uv} is not an x64 build; the setup must bundle x64 uv.exe (it also runs "
                 "on Windows on ARM). Install x64 uv and build again.")
    stage = ROOT / "dist" / "installer-build"
    shutil.rmtree(stage, ignore_errors=True)
    payload = stage / "payload"
    subprocess.run([uv, "build", "--wheel", "--out-dir", str(payload)], cwd=ROOT, check=True)
    # uv.lock's versions, so every install of this exe gets the same dependencies (-c at install);
    # the extras' too, so `keepfold model download --voice` pins av and ctranslate2 (a constraint
    # never installs anything by itself)
    subprocess.run([uv, "export", "--frozen", "--no-dev", "--all-extras", "--no-emit-project",
                    "--no-hashes", "--quiet", "-o", str(payload / "constraints.txt")],
                   cwd=ROOT, check=True)
    shutil.copy2(uv, payload / "uv.exe")
    # the Chat skill, beside the exe: the release attaches it as keepfold-skill.zip
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_skills
    _, zpath = build_skills.build(ROOT / "skills" / "keepfold" / "SKILL.md", stage / "skill")
    shutil.copy2(zpath, ROOT / "dist" / zpath.name)
    # the apps of 0.6.1 and older link releases/latest/download/keepfold-skill.zip
    shutil.copy2(zpath, ROOT / "dist" / "keepfold-skill.zip")
    subprocess.run([uv, "tool", "run", "--python", "3.14", "--from", PYINSTALLER, "pyinstaller",
                    "--noconfirm", "--clean", "--onefile", "--windowed", "--name", "KeepfoldSetup",
                    *data(payload),
                    "--distpath", str(ROOT / "dist"), "--workpath", str(stage / "work"),
                    "--specpath", str(stage), str(ROOT / "installer" / "setup_app.py")],
                   cwd=ROOT, check=True)
    exe = ROOT / "dist" / "KeepfoldSetup.exe"
    write_sha256(exe)
    print(f"built {exe} ({exe.stat().st_size / 1e6:.1f} MB), {exe.name}.sha256 and "
          f"{zpath.name} beside it")


def write_sha256(exe: Path) -> Path:
    """`<exe>.sha256` in sha256sum's form; the updater (keepfold.update) reads its first 64 hex."""
    out = exe.with_name(exe.name + ".sha256")
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    out.write_text(f"{digest}  {exe.name}\n", encoding="ascii", newline="\n")
    return out


if __name__ == "__main__":
    main()
