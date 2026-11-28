"""Claude desktop config discovery per OS and the shared writer of the keepfold entry."""

import json
from pathlib import Path

import pytest

from keepfold import atomic, clients, doctor
from keepfold.app import targets

pytestmark = pytest.mark.usefixtures("home")

CFG = clients.CONFIG


def test_windows_probes_all_three_layouts(home):
    assert clients.claude_desktop_config_paths("win32") == []
    classic = home / "APPDATA" / "Claude"
    profile = home / "LOCALAPPDATA" / "Claude-Data" / "p1"
    store = home / "LOCALAPPDATA" / "Packages" / "Claude_abc123" / "LocalCache/Roaming/Claude"
    for d in (classic, profile, store):
        d.mkdir(parents=True)
    (profile / CFG).write_text("{}")
    (home / "LOCALAPPDATA" / "Claude-Data" / "empty").mkdir()  # no config: not a profile
    # the Store app's profiles come first, so a first setup writes where that app reads
    assert clients.claude_desktop_config_paths("win32") == [profile / CFG, classic / CFG,
                                                            store / CFG]
    fresh = home / "LOCALAPPDATA" / "Claude-Data" / "claude_abc123"  # a profile, no config yet
    fresh.mkdir()
    paths = clients.claude_desktop_config_paths("win32")
    assert paths == [fresh / CFG, profile / CFG, classic / CFG, store / CFG]
    (profile / CFG).unlink()
    assert clients.setup_targets(clients.claude_desktop_config_paths("win32")) == [fresh / CFG]


def test_macos_and_linux(home, monkeypatch):
    mac = home / "Library" / "Application Support" / "Claude"
    assert clients.claude_desktop_config_paths("darwin") == []
    mac.mkdir(parents=True)
    assert clients.claude_desktop_config_paths("darwin") == [mac / CFG]
    lin = home / ".config" / "Claude"
    lin.mkdir(parents=True)
    assert clients.claude_desktop_config_paths("linux") == [lin / CFG]
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    assert clients.claude_desktop_config_paths("linux") == []


def test_desktop_writer_keeps_keys_backs_up_and_is_idempotent(tmp_path):
    """Setup and the app share one writer (app.targets): timestamped backups, and a `.bak`
    the user already has is never overwritten."""
    path = tmp_path / CFG
    old = {"theme": "dark", "mcpServers": {"other": {"command": "x"}}}
    path.write_text(json.dumps(old), encoding="utf-8")
    (tmp_path / (CFG + ".bak")).write_text("mine", encoding="utf-8")
    vault = tmp_path / "My Vault"
    entry = clients.server_entry(clients.keepfold_command(), vault)
    assert Path(entry["command"]).is_absolute()
    assert entry["args"] == ["mcp", "--vault", str(vault)]

    assert targets.edit_json(path, "mcpServers", entry, dry_run=True) == "add"
    assert json.loads(path.read_text(encoding="utf-8")) == old  # dry run wrote nothing
    assert targets.edit_json(path, "mcpServers", entry) == "add"
    got = json.loads(path.read_text(encoding="utf-8"))
    assert got["theme"] == "dark" and got["mcpServers"]["other"] == {"command": "x"}
    assert got["mcpServers"]["keepfold"] == entry
    assert (tmp_path / (CFG + ".bak")).read_text(encoding="utf-8") == "mine"
    baks = list(tmp_path.glob(CFG + ".keepfold-*.bak"))
    assert [json.loads(b.read_text(encoding="utf-8")) for b in baks] == [old]
    before = path.read_bytes()
    assert targets.edit_json(path, "mcpServers", entry) == "none"
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))
    # doctor sees the merged entry as a Keepfold server
    assert doctor._is_keepfold_server(got["mcpServers"]["keepfold"])


def test_setup_targets_prefers_existing_configs(tmp_path):
    a, b, c = (tmp_path / n / CFG for n in "abc")
    for p in (a, b, c):
        p.parent.mkdir()
    assert clients.setup_targets([a, b, c]) == [a]  # none exists: only the first folder
    b.write_text("{}", encoding="utf-8")
    c.write_text("{}", encoding="utf-8")
    assert clients.setup_targets([a, b, c]) == [b, c]
    assert clients.setup_targets([]) == []


def test_desktop_writer_accepts_a_bom(tmp_path):  # Notepad / PowerShell 5.1 "UTF-8"
    path = tmp_path / CFG
    path.write_text('{"mcpServers": {"other": {"command": "x"}}}', encoding="utf-8-sig")
    entry = clients.server_entry("/abs/keepfold", tmp_path)
    assert targets.edit_json(path, "mcpServers", entry) == "add"
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")
    servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
    assert servers.keys() == {"other", "keepfold"}
    assert doctor._read_json(path)["mcpServers"]["keepfold"] == entry


def _busy_replace(monkeypatch, fails: int | None):
    """os.replace in targets raising PermissionError `fails` times (None: always); no sleeps."""
    real, calls = targets.os.replace, []

    def replace(src, dst):
        calls.append(dst)
        if fails is None or len(calls) <= fails:
            raise PermissionError(13, "in use by another program")
        real(src, dst)

    monkeypatch.setattr(targets.os, "replace", replace)
    monkeypatch.setattr(atomic.time, "sleep", lambda s: None)
    return calls


def test_write_retries_a_busy_file(tmp_path, monkeypatch):  # S1: an editor or Defender holds it
    path = tmp_path / "rules.md"
    path.write_text("old", encoding="utf-8")
    calls = _busy_replace(monkeypatch, 2)
    targets.write(path, "new")
    assert len(calls) == 3 and path.read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob("*.tmp"))


def test_write_gives_up_on_a_file_that_stays_busy(tmp_path, monkeypatch):
    path = tmp_path / "rules.md"
    path.write_text("old", encoding="utf-8")
    calls = _busy_replace(monkeypatch, None)
    with pytest.raises(PermissionError):
        targets.write(path, "new")
    assert len(calls) == atomic.REPLACE_TRIES and path.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob("*.tmp"))
