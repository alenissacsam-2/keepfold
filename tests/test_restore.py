"""`keepfold restore ZIP [DEST]` (D7 §6, A8): a backup comes back into a new folder with its own
vault_id; a zip that could write outside DEST, unpack into a bomb, or comes from a newer
Keepfold is refused, and a non-empty DEST is never written."""

import json
import shutil
import stat
import zipfile
from pathlib import Path

import pytest
from conftest import git
from typer.testing import CliRunner

from keepfold import backup, vault_format
from keepfold.cli import app
from keepfold.vault import VaultError

FIXTURE = Path(__file__).parent / "fixtures" / "vault_mini"


def _tree(root: Path) -> dict[str, bytes]:
    """The notes and the event log: what a restore must bring back byte for byte."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and (p.suffix == ".md" or "events" in p.parts)}


@pytest.mark.gate("G2")
def test_backup_restore_round_trip(tmp_path, home):
    assert Path.home() == home and home.is_relative_to(tmp_path)  # never the real ~/.keepfold
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE, root)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "first")
    zp = backup.backup(root)
    old_id = vault_format.state(root).stamp["vault_id"]
    before = {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}

    dest = tmp_path / "restored"
    r = CliRunner().invoke(app, ["restore", str(zp), str(dest)])
    assert r.exit_code == 0, r.output
    assert f"Restored to {dest.resolve()}" in r.output
    restored = _tree(dest)
    assert {k: v for k, v in restored.items() if not k.startswith("events/")} == \
        {k: v for k, v in _tree(root).items() if not k.startswith("events/")}
    for k, v in _tree(root).items():  # the log only grows: the restore's format event follows
        assert restored[k].startswith(v), k
    assert git(dest, "log", "--format=%s").split() == ["first"]

    stamp = json.loads((dest / vault_format.STAMP).read_text(encoding="utf-8"))
    assert stamp["vault_id"] != old_id and backup.ID_RE.fullmatch(stamp["vault_id"])
    assert stamp["restored_from"]["vault_id"] == old_id
    assert stamp["restored_from"]["zip"] == zp.name
    assert vault_format.state(dest).status == "current"
    assert not list(tmp_path.glob("restored.partial-*"))  # renamed into place
    # the vault it came from is untouched, and its zip still names the old id
    assert {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()} == before
    assert vault_format.state(root).stamp["vault_id"] == old_id

    empty = tmp_path / "empty"
    empty.mkdir()
    assert backup.restore(zp, empty) == empty.resolve() and (empty / "concepts").is_dir()


def _zip(path: Path, members: list[tuple]) -> Path:
    """A zip of (name, data) or (ZipInfo, data) members."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("concepts/ok.md", "# ok\n")
        for name, data in members:
            z.writestr(name, data)
    return path


def _link() -> zipfile.ZipInfo:
    i = zipfile.ZipInfo("notes/link.md")
    i.external_attr = (stat.S_IFLNK | 0o777) << 16
    return i


@pytest.mark.gate("G2")
@pytest.mark.parametrize("member,why", [
    (("/etc/evil.md", "x"), "absolute path"),
    (("C:/evil.md", "x"), "drive letter"),
    (("c:evil.md", "x"), "drive letter"),
    (("notes/../../evil.md", "x"), "'..'"),
    (("..\\evil.md", "x"), "'..'"),
    ((_link(), "/etc/passwd"), "symlink"),
    (("notes/big.md", "x" * (5 << 20)), "restore unpacks for one file"),
    (("notes/bomb.bin", b"\0" * (2 << 20)), "zip bomb"),
    ((vault_format.STAMP, json.dumps({"format": vault_format.CODE_FORMAT + 1})), "newer Keepfold"),
    ((vault_format.STAMP, "not json"), "cannot be read"),
], ids=["absolute", "drive", "drive-relative", "dotdot", "backslash", "symlink", "oversize",
        "ratio", "newer-stamp", "bad-stamp"])
def test_restore_refuses_bad_members(tmp_path, home, monkeypatch, member, why):
    assert Path.home() == home and home.is_relative_to(tmp_path)
    monkeypatch.setattr(backup, "MAX_MEMBER", 4 << 20)
    zp = _zip(tmp_path / "bad.zip", [member])
    dest = tmp_path / "out"
    with pytest.raises(VaultError, match=why):
        backup.restore(zp, dest)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bad.zip", "home"]  # nothing written


@pytest.mark.gate("G2")
def test_restore_refuses_a_total_too_large_and_a_non_empty_dest(tmp_path, home, monkeypatch):
    zp = _zip(tmp_path / "ok.zip", [("notes/a.md", "a" * 600), ("notes/b.md", "b" * 600)])
    monkeypatch.setattr(backup, "MAX_TOTAL", 1000)
    with pytest.raises(VaultError, match="unpacks to"):
        backup.restore(zp, tmp_path / "out")
    monkeypatch.setattr(backup, "MAX_TOTAL", 64 << 30)  # never undo(): it holds the temp HOME
    full = tmp_path / "vault"
    full.mkdir()
    (full / "keep.md").write_text("mine", encoding="utf-8")
    for dest in (full, full / "keep.md"):
        with pytest.raises(VaultError, match="not an empty folder"):
            backup.restore(zp, dest)
    assert [p.name for p in full.iterdir()] == ["keep.md"]
    assert (full / "keep.md").read_text(encoding="utf-8") == "mine"
    r = CliRunner().invoke(app, ["restore", str(zp), str(full)])
    assert r.exit_code == 1 and "not an empty folder" in r.output and "Traceback" not in r.output
    with pytest.raises(VaultError, match="cannot be read as a zip"):
        backup.restore(full / "keep.md", tmp_path / "out2")
