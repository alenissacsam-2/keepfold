"""v5 PHASE4 V4: people pages are private, read by exact id, and forgotten on request."""

import json
from datetime import UTC, datetime

import pytest
from conftest import run
from typer.testing import CliRunner

from keepfold import people
from keepfold.catalog import Catalog
from keepfold.cli import app
from keepfold.dashboard import build_data
from keepfold.index import Index
from keepfold.lint import lint_report
from keepfold.vault import Vault, VaultError, is_private

pytestmark = pytest.mark.usefixtures("home")


def _add(call, title="Dr Rao", **fields):
    return call("keepfold_add", {"kind": "person", "title": title,
                               "fields": {"relation": "professor", **fields}})


def test_private_person_refused_in_an_exposed_session(root):
    (root / "raw").mkdir(exist_ok=True)
    (root / "raw" / "src.md").write_text("outside text\n", encoding="utf-8")

    async def body(call):
        await _add(call)
        await _add(call, title="Ravi", private=False)
        assert "text" in await call("keepfold_read", {"id": "people/dr-rao"})  # unexposed
        await call("keepfold_read", {"id": "raw/src.md"})  # now exposed
        r = await call("keepfold_read", {"id": "people/dr-rao"})
        assert r["error"] == "role_refused" and "Do not retry" in r["next"]
        assert "professor" not in str(r)
        assert "[[people/ravi]]" in (await call("keepfold_read", {"id": "people/ravi"}))["next"]

    run(root, body)


def test_add_read_by_exact_id_only(root):
    async def body(call):
        out = await _add(call)
        assert out["id"] == "people/dr-rao" and out["path"] == "people/dr-rao.md"
        await call("keepfold_patch", {"id": "people/dr-rao", "op": "append_to_section",
                                    "target": "Who they are to me", "text": "- My advisor"})
        r = await call("keepfold_read", {"id": "people/dr-rao"})
        assert r["kind"] == "person" and r["fields"] == (
            '<untrusted_note id="people/dr-rao">\nrelation: professor\ntier: work\n'
            '</untrusted_note>')
        assert r["text"].startswith('<untrusted_note id="people/dr-rao">')
        assert "My advisor" in r["text"] and r["next"].startswith("Private")
        await _add(call, title="Ravi", private=False)
        pub = await call("keepfold_read", {"id": "people/ravi"})
        assert "[[people/ravi]]" in pub["next"]
        assert (await call("keepfold_read", {"id": "dr-rao"}))["error"] == "unknown_id"
        assert (await call("keepfold_read", {"id": "people/nobody"}))["error"] == "unknown_id"
        assert (await call("keepfold_find", {"query": "advisor"}))["hits"] == []
        assert (await call("keepfold_find", {"query": "Rao"}))["hits"] == []
        end = await call("keepfold_end", {})
        assert "rao" not in end["display"].lower()

    run(root, body)
    text = (root / "people" / "dr-rao.md").read_text(encoding="utf-8")
    assert "private: true" in text and "## How I write to them" in text
    for note in (root / "sessions").glob("*.md"):  # session notes are indexed
        assert "rao" not in note.read_text(encoding="utf-8").lower()


def test_add_validation(root):
    async def body(call):
        assert (await _add(call, tier="boss"))["error"] == "invalid_argument"
        bad = await call("keepfold_add", {"kind": "person", "title": "X",
                                        "fields": {"relation": "Best Friend!"}})
        assert bad["error"] == "invalid_argument"
        extra = await _add(call, summary="hi")
        assert extra["error"] == "invalid_argument" and "summary" in extra["message"]
        assert "id" in await _add(call)
        assert (await _add(call))["error"] == "invalid_argument"  # already exists

    run(root, body)


def test_private_unless_frontmatter_says_false(root):
    v = Vault(root)
    pid = people.add(v, "Asha", {"relation": "friend", "tier": "close"})
    path = root / "people" / "asha.md"
    path.write_bytes(path.read_bytes().replace(b"private: true", b"private: unset"))
    note = v.read("person", "asha")
    assert is_private(note)  # anything but an explicit false stays private
    idx = Index(v, Catalog.load(v))
    assert idx.find("Asha") == [] and "asha" not in idx._notes
    people.add(v, "Ravi", {"relation": "friend", "private": False})
    assert not is_private(v.read("person", "ravi"))
    assert [h["id"] for h in Index(v, Catalog.load(v)).find("Ravi")] == ["ravi", "people"]  # hub
    with pytest.raises(VaultError):
        people.add(v, "Mo", {"relation": "friend", "private": "no"})
    assert all(p["id"] != "asha" for p in build_data(v, now=datetime.now(UTC))["pages"])
    assert all(f["id"] != "asha" for f in lint_report(v)[0])
    assert people.get(v, pid) == {"id": "people/asha", "title": "Asha", "relation": "friend",
                                  "tier": "close", "body": "# Asha\n\nUp: [[people]]\n\n"
                                  "## Who they are to me\n\n## How I write to them"}
    hub = (root / "notes" / "people.md").read_text(encoding="utf-8")
    assert "[[people/ravi]]" in hub and "asha" not in hub  # private pages stay off the hub
    for bad in ("asha", "people/../me/profile", "people/", "people/Asha", None):
        assert people.get(v, bad) is None


def _voice(root, rel, rows):
    p = root / rel
    p.parent.mkdir(exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_forget_cli(root):
    v = Vault(root)
    people.add(v, "Asha", {"relation": "friend"})
    _voice(root, "voice/samples.jsonl", [{"id": "1", "audience": "people/asha", "text": "a"},
                                         {"id": "2", "audience": "friend", "text": "b"}])
    _voice(root, "voice/drafts.jsonl", [{"id": "3", "audience": "people/asha", "text": "c"}])
    cli = CliRunner()
    r = cli.invoke(app, ["forget", "people/asha", "--vault", str(root)], input="n\n")
    assert r.exit_code == 1 and (root / "people" / "asha.md").is_file()
    r = cli.invoke(app, ["forget", "people/asha", "--vault", str(root)], input="y\n")
    assert r.exit_code == 0, r.output
    assert "removed the page; 1 voice samples; 1 voice drafts" in r.output, r.output
    assert "git history" in r.output and "too short" not in r.output.lower()
    assert 'Every mention of "Asha" was redacted, including other people' in r.output
    assert not (root / "people" / "asha.md").exists()
    assert [json.loads(x)["id"] for x in
            (root / "voice/samples.jsonl").read_text(encoding="utf-8").splitlines()] == ["2"]
    assert (root / "voice/drafts.jsonl").read_text(encoding="utf-8") == ""
    r = cli.invoke(app, ["forget", "people/asha", "--vault", str(root)])
    assert r.exit_code == 1 and "unknown_id" in r.output
    with pytest.raises(VaultError, match="No people page"):
        people.forget(v, "people/asha")
