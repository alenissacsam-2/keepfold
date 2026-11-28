"""CI workflows: every action is pinned by commit SHA, and scripts/smoke_install.py (which writes
HKCU, shortcuts and PATH) refuses to run outside CI. Nothing here installs anything."""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)\s*(#.*)?$")
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
VERSION_COMMENT = re.compile(r"^#\s*v\d+(\.\d+)*")


def test_ci_pins_actions_by_sha():
    assert {p.name for p in WORKFLOWS} >= {"ci.yml", "installer.yml", "publish.yml",
                                           "release-apps.yml", "slow.yml"}
    seen = 0
    for path in WORKFLOWS:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if m := USES.match(line):
                seen += 1
                where = f"{path.name}:{n}"
                assert PINNED.match(m.group(1)), f"{where}: {m.group(1)} is not pinned by SHA"
                assert m.group(2) and VERSION_COMMENT.match(m.group(2)), (
                    f"{where}: the SHA needs a '# vX.Y.Z' comment")
    assert seen >= 10


def test_workflows_parse_with_least_privilege():
    for path in WORKFLOWS:
        doc = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
        assert doc["jobs"], path.name
        if path.name in ("ci.yml", "installer.yml", "slow.yml"):
            assert doc["permissions"] == {"contents": "read"}, path.name


def test_pytest_commands_keep_loadgroup():  # real-browser tests share an xdist group
    for name in ("ci.yml", "slow.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        cmds = [ln for ln in text.splitlines() if "uv run pytest" in ln]
        assert cmds and all("--dist loadgroup" in c for c in cmds), name


def test_installer_job_triggers_and_steps():
    doc = YAML(typ="safe").load((ROOT / ".github" / "workflows" / "installer.yml")
                                .read_text(encoding="utf-8"))
    on = doc.get("on", doc.get(True))  # YAML 1.1 reads a bare `on` as True
    assert on["push"]["branches"] == ["main"] and "workflow_dispatch" in on
    assert "installer/**" in on["pull_request"]["paths"]
    runs = [s["run"] for s in doc["jobs"]["installer"]["steps"] if "run" in s]
    assert runs == ["uv sync --locked", "uv run python installer/build.py",
                    "uv run python scripts/smoke_install.py"]


@pytest.fixture
def smoke_install():
    spec = importlib.util.spec_from_file_location("smoke_install",
                                                  ROOT / "scripts" / "smoke_install.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def no_process(monkeypatch):
    def boom(*a, **k):
        raise AssertionError(f"smoke_install started a process: {a!r}")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


def test_smoke_install_refuses_without_ci(smoke_install, monkeypatch, capsys):
    monkeypatch.delenv("CI", raising=False)
    no_process(monkeypatch)
    assert smoke_install.main() == 2
    assert "refusing to run" in capsys.readouterr().err


def test_smoke_install_script_exits_nonzero_without_ci():
    import os
    env = {k: v for k, v in os.environ.items() if k != "CI"}
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "smoke_install.py")], env=env,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 2 and "refusing to run" in p.stderr


def test_smoke_install_is_windows_only(smoke_install, monkeypatch):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(sys, "platform", "linux")
    no_process(monkeypatch)
    assert smoke_install.main() == 2


def test_smoke_install_arguments(smoke_install):
    exe, d = Path("dist/KeepfoldSetup.exe"), Path(r"C:\ci\Keepfold Tést")
    assert smoke_install.setup_cmd(exe, d) == [str(exe), "--quiet", "--dir", str(d)]
    assert smoke_install.setup_cmd(exe, d, uninstall=True) == [
        str(exe), "--uninstall", "--quiet", "--dir", str(d)]
    assert smoke_install.INSTALL_DIR.name == "Keepfold Tést"  # spaces and a non-ASCII letter
