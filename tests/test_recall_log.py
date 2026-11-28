"""NEXT.md item 1: the cross-encoder rerank, the recall usage log and `keepfold eval recall`."""

import json

import pytest
from conftest import FakeEmbed
from test_semantic import ids, idx, root  # noqa: F401  (test_semantic's seeded vault)
from typer.testing import CliRunner

from keepfold import recall_log, semantic
from keepfold.cli import app
from keepfold.mcp_server import Keepfold
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")
REAL_LOAD = semantic.load_reranker  # conftest swaps it per test; this is the original


@pytest.fixture(autouse=True)
def always_rerank(monkeypatch):
    """These tests check the rerank's blend and its log on a vault whose top hit is clear, so the
    margin gate (`semantic._close`, tested in test_semantic) is opened wide."""
    monkeypatch.setattr(semantic, "RERANK_MARGIN", 1.0)


def pasta_first(query, texts):  # a cross-encoder sure of pasta (logit +5) and of the rest (-5)
    return [10.0 * min(t.count("Pasta"), 1) - 5 for t in texts]


def test_rerank_reorders_and_keeps_score_and_match(idx):  # noqa: F811
    pool = {h["id"]: h for h in semantic.recall(idx, "vehicle", FakeEmbed(), 20)}
    base = semantic.recall(idx, "vehicle", FakeEmbed())
    rr = semantic.recall(idx, "vehicle", FakeEmbed(), rerank_with=pasta_first)
    assert ids(base)[:3] == ["engines", "gearbox", "pasta"]
    # R3d: the first stage's top page stays; a clearly surer cross-encoder lifts pasta over #2
    assert ids(rr)[:3] == ["engines", "pasta", "gearbox"]
    assert all(h == pool[h["id"]] for h in rr)  # score, match and via as recall gave them
    assert len(semantic.recall(idx, "vehicle", FakeEmbed(), 2, rerank_with=pasta_first)) == 2
    unsure = semantic.recall(idx, "vehicle", FakeEmbed(), rerank_with=lambda q, texts: [
        0.5 * t.count("Pasta") for t in texts])  # a slight preference moves nothing up top
    assert ids(unsure)[:3] == ids(base)[:3]


def test_no_reranker_or_a_broken_one_keeps_the_current_order(idx, home, monkeypatch):  # noqa: F811
    base = semantic.recall(idx, "vehicle", FakeEmbed())
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    assert semantic.search(idx, "vehicle")[0] == base  # none injected: current ranking

    def broken(query, texts):
        raise RuntimeError("onnx")

    assert semantic.recall(idx, "vehicle", FakeEmbed(), rerank_with=broken) == base
    assert REAL_LOAD() is None and not (home / ".keepfold/models").exists()  # never downloads

    monkeypatch.setattr(semantic, "_reranker", pasta_first)
    assert ids(semantic.search(idx, "vehicle")[0])[:2] == ["engines", "pasta"]
    (home / ".keepfold").mkdir()
    (home / ".keepfold/config.yaml").write_text("vault: x\nrerank: false\n", "utf-8")
    assert REAL_LOAD() is None and REAL_LOAD(config=False) is pasta_first
    assert semantic.search(idx, "vehicle")[0] == base  # switched off


def test_two_threads_load_the_reranker_once(home, monkeypatch):
    """Warm-up and a first Chat turn racing: one TextCrossEncoder is built, both get it."""
    import threading
    import time

    from fastembed.rerank import cross_encoder

    built = []

    class Fake:
        def __init__(self, *a, **kw):
            built.append(1)
            time.sleep(0.2)  # a slow load: the other thread arrives meanwhile

        def rerank(self, query, texts):
            return [0.0 for _ in texts]

    monkeypatch.setattr(cross_encoder, "TextCrossEncoder", Fake)
    (home / ".keepfold/models" / f"models--{semantic.RERANK_MODEL.replace('/', '--')}").mkdir(
        parents=True)
    got = []
    ts = [threading.Thread(target=lambda: got.append(REAL_LOAD())) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert built == [1] and len(got) == 2 and got[0] is got[1] is not None


def test_an_idle_reranker_is_dropped_and_loads_again(home, monkeypatch):
    import time

    from fastembed.rerank import cross_encoder

    built = []

    class Fake:
        def __init__(self, *a, enable_cpu_mem_arena=True, **kw):
            built.append(enable_cpu_mem_arena)

        def rerank(self, query, texts):
            return [1.0 for _ in texts]

    monkeypatch.setattr(cross_encoder, "TextCrossEncoder", Fake)
    (home / ".keepfold/models" / f"models--{semantic.RERANK_MODEL.replace('/', '--')}").mkdir(
        parents=True)
    score = REAL_LOAD()
    assert score("q", ["a", "b"]) == [1.0, 1.0]
    assert semantic.drop_idle(time.monotonic() + semantic.IDLE_UNLOAD + 1) == ["reranker"]
    assert REAL_LOAD() is score and score("q", ["a"]) == [1.0]
    assert built == [False, False]  # loaded again; never with onnxruntime's CPU arena


def logged(root):  # noqa: F811
    return [json.loads(x) for x in (root / recall_log.FILE).read_text("utf-8").splitlines()]


def test_recall_and_context_are_logged_and_reads_link_to_them(root, monkeypatch):  # noqa: F811
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    c = Keepfold(Vault(root))
    got = ids(c.keepfold_recall("vehicle")["hits"])
    c.keepfold_context(topic="automobile")
    c.keepfold_read("pasta")  # returned by the recall only
    c.keepfold_read("engines")  # returned by both: the latest recall
    c.keepfold_recall("I like cars.", check=True)  # a check is not logged
    rows = logged(root)
    assert [r.get("tool") for r in rows] == ["keepfold_recall", "keepfold_context", None, None]
    assert rows[0]["query"] == "vehicle" and rows[0]["ids"] == got and rows[0]["ts"]
    assert "pasta" in got and "pasta" not in rows[1]["ids"]
    assert rows[2] == {"ts": rows[2]["ts"], "opened": rows[0]["rid"], "id": "pasta"}
    assert rows[3]["opened"] == rows[1]["rid"] and rows[3]["id"] == "engines"
    assert not list(root.joinpath("events").rglob("*.jsonl"))  # never in the event log

    c._recalls = [(t - recall_log.WINDOW_S - 1, rid, i) for t, rid, i in c._recalls]
    c.keepfold_read("gearbox")  # too late: no row
    assert len(logged(root)) == 4

    monkeypatch.setattr(recall_log, "CAP", 3)
    c.keepfold_recall("motor")
    assert [r.get("query") for r in logged(root)] == [None, None, "motor"]


def test_eval_set_and_metrics(idx):  # noqa: F811
    rows = [{"rid": "a", "query": "vehicle", "ids": ["engines"]},
            {"rid": "b", "query": "nothing opened", "ids": ["pasta"]},
            {"rid": "c", "query": "opened elsewhere", "ids": ["pasta"]},
            {"opened": "a", "id": "engines"}, {"opened": "a", "id": "gearbox"},
            {"opened": "c", "id": "engines"}]
    assert recall_log.eval_set(rows) == [("vehicle", {"engines", "gearbox"})]
    m = recall_log.evaluate(idx, [("vehicle", {"pasta"})], FakeEmbed(), pasta_first, k=3)
    assert m["current"] == (1.0, pytest.approx(1 / 3)) and m["rerank"] == (1.0, 0.5)
    assert set(m) == set(recall_log.VARIANTS) and m["rrf"][0] == 1.0
    assert recall_log.evaluate(idx, [], None, None)["rerank"] is None


def test_eval_cli_prints_each_variant(root, monkeypatch):  # noqa: F811
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    c = Keepfold(Vault(root))
    c.keepfold_recall("vehicle")
    c.keepfold_read("engines")
    run = CliRunner().invoke
    out = run(app, ["eval", "recall", "--vault", str(root), "--k", "3"])
    assert out.exit_code == 0, out.output
    assert "queries: 1 (too few to trust" in out.output
    assert "current" in out.output and "skipped: reranker not downloaded" in out.output
    monkeypatch.setattr(semantic, "_reranker", pasta_first)
    out = run(app, ["eval", "recall", "--vault", str(root)])
    rows = {ln.split()[0]: ln.split()[1:] for ln in out.output.splitlines()[2:]}
    # R3d: the reranker keeps the first stage's top page (the opened one) on top
    assert rows["rerank"] == ["1.000", "1.000"] and rows["current"] == ["1.000", "1.000"]
    assert rows["no-downrank"] == ["1.000", "1.000"]  # no chat pages: same as current
