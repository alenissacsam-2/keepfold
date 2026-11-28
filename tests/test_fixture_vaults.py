"""GT3: vaults made by old releases' own code (tests/fixtures/vaults/<tag>.zip, built by
scripts/make_fixture_vault.py) open in this Keepfold, and a vault from a future Keepfold
(format 99) refuses writes but reads. Synthetic vaults only, unzipped into tmp_path."""

import json
import zipfile
from pathlib import Path

import pytest
from conftest import run
from typer.testing import CliRunner

from keepfold import migrate, vault_format
from keepfold.cli import app
from keepfold.vault import Vault, VaultError

pytestmark = pytest.mark.usefixtures("home")

VAULTS = Path(__file__).parent / "fixtures" / "vaults"
TAGS = ("v0.6.1", "v0.7.0")
NOTE = "fixture-note-on-stacks"  # each zip's note, concept and //pref line
CONCEPT = "fixture-stacks"
PREF = "Synthetic fixture: show a short worked example before the rule."


def unzip(tag: str, dest: Path) -> Path:
    with zipfile.ZipFile(VAULTS / f"{tag}.zip") as z:
        z.extractall(dest)
    return dest


@pytest.fixture
def future_vault(tmp_path) -> Path:
    """A current (v0.7.0 layout) vault whose .keepfold/format.json says format 99."""
    root = unzip("v0.7.0", tmp_path / "vault")
    (root / vault_format.STAMP).write_text(json.dumps(
        {"format": 99, "vault_id": "01FUTURE0000000000000000000", "created_by": "99.0.0",
         "dirs": {"resource": ["archive/chats"]}}), encoding="utf-8")
    return root


@pytest.mark.parametrize("tag", TAGS)
def test_zip_is_small_and_synthetic(tag):
    data = (VAULTS / f"{tag}.zip").read_bytes()
    assert len(data) < 200_000
    with zipfile.ZipFile(VAULTS / f"{tag}.zip") as z:
        names = z.namelist()
    assert f"notes/{NOTE}.md" in names and f"concepts/{CONCEPT}.md" in names
    old = ".cortex/" if tag == "v0.6.1" else ".keepfold/"
    assert any(n.startswith(old) for n in names)
    assert not any(n.startswith(("home/", ".git/hooks/")) for n in names)


@pytest.mark.parametrize("tag", TAGS)
def test_old_vault_opens_as_needs_upgrade_and_replays(tag, tmp_path, monkeypatch):
    root = unzip(tag, tmp_path / "vault")
    if tag == "v0.6.1":  # the last .cortex layout: migrate.py moves it first
        monkeypatch.setattr(migrate, "_dev", False)
        monkeypatch.setattr(migrate, "_SEEN", set())
        assert migrate.vault(root)["state"] == "done"
        assert not (root / ".cortex").exists() and (root / ".keepfold" / "migrated").is_file()
        text = (root / "concepts" / f"{CONCEPT}.md").read_text(encoding="utf-8")
        assert "cortex_" not in text
    assert vault_format.state(root).status == "needs_upgrade"
    r = CliRunner().invoke(app, ["replay", "--vault", str(root)])
    assert r.exit_code == 0, r.output
    assert "Learning: 1" in r.output  # the fixture's one graded ask, folded again here
    v = Vault(root)
    assert v.read("concept", CONCEPT).fields.get("keepfold_status") == "learning"
    assert PREF in (root / "me" / "preferences.md").read_text(encoding="utf-8")
    v.create_note("note", "after-upgrade", {"title": "After upgrade"}, "body")  # still writable


def test_future_vault_refuses_writes_but_reads(future_vault):
    assert vault_format.state(future_vault).status == "too_new"
    v = Vault(future_vault)
    with pytest.raises(VaultError) as e:
        v.create_note("note", "fresh", {"title": "Fresh"}, "body")
    assert e.value.code == "vault_too_new"
    r = CliRunner().invoke(app, ["replay", "--vault", str(future_vault)])
    assert r.exit_code != 0 and "newer Keepfold" in r.output, r.output
    got = {}

    async def body(call):
        got["find"] = await call("keepfold_find", {"query": "stack"})
        got["read"] = await call("keepfold_read", {"id": NOTE})
        got["add"] = await call("keepfold_add", {"kind": "note", "title": "Fresh"})

    run(future_vault, body)
    assert NOTE in json.dumps(got["find"])
    assert "last in, first out" in json.dumps(got["read"])
    assert got["add"]["error"] == "vault_too_new"
    assert not (future_vault / "notes" / "fresh.md").exists()
