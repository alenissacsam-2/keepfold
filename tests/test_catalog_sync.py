import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from keepfold import planner
from keepfold.catalog import Catalog, sync
from keepfold.cli import app
from keepfold.dashboard import build_data
from keepfold.index import Index
from keepfold.mcp_server import Keepfold
from keepfold.vault import Vault, VaultError

FIXTURE = Path(__file__).parent / "fixtures" / "vault_mini"
EMPTY = {"adopted": [], "updated": [], "materialised": [], "skipped": []}

HAND_AREA = "---\ntitle: Dynamic programming\nkind: area\nparent: \"[[dsa]]\"\n---\n# DP\n"
HAND_CONCEPT = ("---\ntitle: Knapsack\nparent: \"[[dsa.dp]]\"\n"
                "areas: [\"[[dsa.arrays]]\", \"[[dsa.nowhere]]\"]\naliases: [0-1 knapsack]\n"
                "requires: [\"[[arrays-basics]]\"]\n---\n# Knapsack\n\nMine.\n")


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE, root)
    return Vault(root)


def hand_notes(vault):
    (vault.root / "areas/dsa.dp.md").write_bytes(HAND_AREA.encode())
    (vault.root / "concepts/knapsack.md").write_bytes(HAND_CONCEPT.encode())


def test_fixture_is_in_sync(vault):
    before = (vault.root / "catalog.yaml").read_bytes()
    assert sync(vault) == EMPTY
    assert (vault.root / "catalog.yaml").read_bytes() == before


def test_sync_adopts_hand_made_area_and_concept(vault):
    hand_notes(vault)
    r = sync(vault)
    assert r == {**EMPTY, "adopted": ["dsa.dp", "knapsack"]}
    cat = Catalog.load(vault)
    area, k = cat.entries["dsa.dp"], cat.entries["knapsack"]
    assert (area.kind, area.parent, area.title) == ("area", "dsa", "Dynamic programming")
    assert (k.parent, k.domain, k.title, k.aliases, k.requires, k.areas) == \
        ("dsa.dp", "dsa", "Knapsack", ["0-1 knapsack"], ["arrays-basics"], ["dsa.arrays"])
    assert (vault.root / "concepts/knapsack.md").read_bytes() == HAND_CONCEPT.encode()
    assert sync(vault) == EMPTY  # idempotent


def test_sync_materialises_missing_note_without_touching_others(vault):
    (vault.root / "concepts/arrays-basics.md").unlink()
    two = (vault.root / "concepts/two-pointers.md").read_bytes()
    assert sync(vault) == {**EMPTY, "materialised": ["arrays-basics"]}
    f = vault.read("concept", "arrays-basics").fields
    assert (f["title"], f["parent"], f["domain"]) == ("Array basics", "[[dsa.arrays]]", "dsa")
    assert (vault.root / "concepts/two-pointers.md").read_bytes() == two
    assert "arrays-basics" in Catalog.load(vault).entries  # reported, never removed
    assert sync(vault) == EMPTY


def test_sync_takes_obsidian_title_and_parent_edits(vault):
    hand_notes(vault)
    sync(vault)
    vault._edit_fields("concept", "two-pointers", {"title": "Two-pointer sweep",
                                                   "parent": "[[dsa.dp]]"})
    assert sync(vault) == {**EMPTY, "updated": ["two-pointers"]}
    e = Catalog.load(vault).entries["two-pointers"]
    assert (e.title, e.parent) == ("Two-pointer sweep", "dsa.dp")
    assert "two-pointers" in Catalog.load(vault).children("dsa.dp")
    assert sync(vault) == EMPTY


def test_sync_skips_unplaceable_and_merged_notes(vault):
    (vault.root / "concepts/orphan.md").write_bytes(b"---\ntitle: Orphan\n---\n# Orphan\n")
    (vault.root / "concepts/gone.md").write_bytes(
        b"---\ntitle: Gone\nparent: \"[[dsa.arrays]]\"\nmerged_into: \"[[two-pointers]]\"\n---\n")
    r = sync(vault)
    assert r["skipped"] == ["orphan: parent (none) is not a known area"] and not r["adopted"]
    assert {"orphan", "gone"}.isdisjoint(Catalog.load(vault).entries)


def test_if_changed_is_a_stat_scan_only(vault, monkeypatch):
    sync(vault)
    monkeypatch.setattr(Catalog, "_sync", lambda self: pytest.fail("full sync ran"))
    assert sync(vault, if_changed=True) == EMPTY
    monkeypatch.undo()
    hand_notes(vault)
    assert sync(vault, if_changed=True)["adopted"] == ["dsa.dp", "knapsack"]


def test_polyhierarchy_in_index_and_exam_expansion(vault):
    hand_notes(vault)
    sync(vault)
    cat = Catalog.load(vault)
    idx = Index(vault, cat)
    edges = set(idx.db.execute("SELECT dst, type FROM edges WHERE src = 'knapsack'"))
    assert {("dsa.dp", "parent"), ("dsa.arrays", "areas")} <= edges
    for area in ("area:dsa.dp", "area:dsa.arrays"):
        assert "knapsack" in planner._weights(cat, {area: 1})
    assert "knapsack" in planner._weights(cat, {"domain:dsa": 1})
    links = build_data(vault, now=datetime(2026, 11, 15, 12, tzinfo=UTC))["graph"]["links"]
    assert {"s": "dsa.arrays", "t": "knapsack", "type": "area"} in links
    assert {"s": "dsa.dp", "t": "knapsack", "type": "hier"} in links


def test_add_concept_with_areas(vault):
    cat = Catalog.load(vault)
    dp = cat.add("area", "DP", parent="dsa")
    id = cat.add("concept", "Knapsack", parent=dp, areas=["Arrays", dp])
    assert Catalog.load(vault).entries[id].areas == ["dsa.arrays"]  # primary parent dropped
    assert vault.read("concept", id).fields["areas"] == ["[[dsa.arrays]]"]
    with pytest.raises(VaultError):
        cat.add("area", "Graphs", parent="dsa", areas=["dsa.arrays"])
    assert sync(vault) == EMPTY


def test_keepfold_add_areas_over_mcp(vault):
    core = Keepfold(vault)
    core._call("keepfold_start", core.keepfold_start, {})
    core._call("keepfold_add", core.keepfold_add, {"kind": "area", "title": "DP", "parent": "dsa"})
    out = core._call("keepfold_add", core.keepfold_add, {
        "kind": "concept", "title": "Knapsack", "parent": "dsa.dp", "areas": ["dsa.arrays"]})
    assert out["id"] == "knapsack", out
    assert Catalog.load(vault).entries["knapsack"].areas == ["dsa.arrays"]
    bad = core._call("keepfold_add", core.keepfold_add, {
        "kind": "problem", "title": "LC 9", "areas": ["dsa.arrays"]})
    assert bad["error"] == "invalid_argument"


def test_cli_catalog_sync_and_replay(vault, monkeypatch):
    monkeypatch.setenv("KEEPFOLD_VAULT", str(vault.root))
    runner = CliRunner()
    assert "catalog in sync" in runner.invoke(app, ["catalog", "sync"]).output
    hand_notes(vault)
    out = runner.invoke(app, ["replay", "--dry-run"]).output
    assert "adopted" not in out and "knapsack" not in Catalog.load(vault).entries
    r = runner.invoke(app, ["replay"])
    assert r.exit_code == 0, r.output
    assert "adopted dsa.dp" in r.output and "adopted knapsack" in r.output
    assert "catalog in sync" in runner.invoke(app, ["catalog", "sync"]).output
