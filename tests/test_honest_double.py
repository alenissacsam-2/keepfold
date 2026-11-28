"""v5 PHASE2.md G1: the Honest Double check on keepfold_recall."""

import re

import numpy as np
import pytest
from conftest import run

from keepfold import semantic
from keepfold.events import EventLog
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

# The fake embedder: words on the same axis mean the same thing; other words count for nothing.
AXES = {"pointer": 0, "pointers": 0, "array": 1, "arrays": 1}


def fake_embed(texts):
    out = np.zeros((len(texts), 3), dtype=np.float32)
    for k, t in enumerate(texts):
        for w in re.findall(r"\w+", t.lower()):
            if w in AXES:
                out[k, AXES[w]] += 1
    return out


@pytest.fixture(autouse=True)
def embed(monkeypatch):
    monkeypatch.setattr(semantic, "_embedder", fake_embed)


TEXT = ("I am strong at two pointers. Arrays are easy for me.\n"
        "- I enjoy cooking. My secret diary is private.")


def verdicts(out):
    return {c["concept"]: c["verdict"] for c in out["checks"]}


def test_check_gives_a_verdict_per_concept_sentence(root):
    async def body(call):
        await call("keepfold_log", {"kind": "self_report",
                                  "payload": {"concept": "two-pointers", "verdict": "correct"}})
        out = await call("keepfold_recall", {"query": TEXT, "check": True})
        # cooking matches no concept; the private concept is not in the index
        assert [c["sentence"] for c in out["checks"]] == [
            "I am strong at two pointers.", "Arrays are easy for me."]
        assert verdicts(out) == {"two-pointers": "hedge", "arrays-basics": "unverified"}
        assert out["checks"][0]["state"] in ("learning", "needs_refresh")
        assert out["checks"][0]["title"] == "Two pointers"
        assert "hedge Learning" in out["next"] and "hits" in out
        plain = await call("keepfold_recall", {"query": TEXT})
        assert "checks" not in plain

        await call("keepfold_add", {"kind": "project", "title": "Pair finder", "fields": {
            "status": "active", "applies": ["[[two-pointers]]"]}})
        out = await call("keepfold_recall", {"query": TEXT, "check": True})
        tp = out["checks"][0]
        assert tp["verdict"] == "ok" and tp["applied"] is True

    run(root, body)


def test_a_shipped_project_counts_as_applied(root):
    """Audit F3: `shipped` is a real project status (`done` is not)."""
    async def body(call):
        await call("keepfold_add", {"kind": "project", "title": "Pair finder", "fields": {
            "status": "shipped", "applies": ["[[two-pointers]]"]}})
        out = await call("keepfold_recall", {"query": TEXT, "check": True})
        assert verdicts(out)["two-pointers"] == "ok" and out["checks"][0]["applied"] is True

    run(root, body)


def test_an_old_claim_flag_no_longer_dismisses(root):
    """claim_flag was removed on 20 Nov 2026: an old vault's unfair flag is ignored."""
    EventLog(Vault(root)).append(session="cli", kind="log", source="tutor", fields={
        "payload": {"kind": "claim_flag", "flagged": "arrays-basics",
                    "sentence": "Arrays are easy for me.", "fair": False}})

    async def body(call):
        check = await call("keepfold_recall", {"query": "Arrays are easy for me.", "check": True})
        assert verdicts(check) == {"arrays-basics": "unverified"}
        assert "claim_check" not in await call("keepfold_reflect")

    run(root, body)


def test_check_limits(root):
    async def body(call):
        for text in ("word. " * 21, "x" * 4001):
            out = await call("keepfold_recall", {"query": text, "check": True})
            assert out["error"] == "invalid_argument"
        ok = await call("keepfold_recall", {"query": "word. " * 20, "check": True})
        assert ok["checks"] == []
        # a keyword-only hit ("technique" in Two pointers' alias, no embedding match) is no
        # claim; not "two": a question word (semantic.QUESTION_WORDS) leaves the keyword cover
        kw = await call("keepfold_recall", {"query": "Technique friends.", "check": True})
        assert kw["hits"] and kw["checks"] == []

    run(root, body)


def test_the_reranker_never_touches_the_check(root, monkeypatch):
    """Rerank reorders plain recall only; the per-sentence check keeps recall's own order."""
    from keepfold.mcp_server import Keepfold
    from keepfold.vault import Vault

    before = Keepfold(Vault(root)).keepfold_recall(TEXT, check=True)["checks"]
    seen = []

    def reverse(query, texts):
        seen.append(query)
        return list(range(len(texts)))  # the last hit first

    monkeypatch.setattr(semantic, "_reranker", reverse)
    after = Keepfold(Vault(root)).keepfold_recall(TEXT, check=True)
    assert after["checks"] == before and before
    assert seen == [TEXT]  # the main search only, never a checked sentence
