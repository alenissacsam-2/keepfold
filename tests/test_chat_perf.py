"""Chat's data answers stay fast on a heavy vault: scripts/bench_chat_data.py's synthetic vault,
scaled down (10 days x 300 site keys), each answer timed after a first call under a generous
bound, and the fast paths of `sites._sides` (lean day counts, page URLs read only for a listed
subdomain) decide every key as reading every URL did. Made-up data only."""

import importlib.util
import time
from datetime import timedelta
from pathlib import Path

import pytest

from keepfold.browse import load_settings, sites
from keepfold.vault import Vault

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bench_chat_data",
                                              ROOT / "scripts" / "bench_chat_data.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)
BOUND_S = 1.5  # the target on a 90-day vault; this one is ~50x smaller


@pytest.fixture(scope="module")
def heavy(tmp_path_factory):
    root = tmp_path_factory.mktemp("heavy") / "vault"
    bench.build(root, days=10, keys=300, urls=20, sites=1000, notes=300, n_events=2000)
    return Vault(root)


@pytest.fixture
def lists(home):
    (home / ".keepfold").mkdir()
    (home / ".keepfold" / "config.yaml").write_text(
        "browse:\n  visible_to_claude: true\n  private_domains: [s1.example, docs.s2.example]\n"
        "  deny_domains: [docs.s4.example]\n", encoding="utf-8")


@pytest.mark.slow
@pytest.mark.perf  # a time budget: run alone with -m perf, never under a busy -n run
def test_every_data_answer_is_quick(heavy, lists):
    for name, fn in bench.calls(heavy).items():
        fn()  # the first call fills the parse caches and the digest's index
        t = time.perf_counter()
        fn()
        assert time.perf_counter() - t < BOUND_S, name


def test_lean_counts_decide_every_key_as_every_url_did(heavy, lists, monkeypatch):
    settings, llm = load_settings(), sites.load_cache(heavy)["llm"]
    full, lean = sites.load_days(heavy)[0], sites.lean_days(heavy)
    assert lean is sites.lean_days(heavy)  # parsed once per version of the file
    fast = sites._sides(settings, llm, full)[0]
    leaned = sites._sides(settings, llm, lean)[0]
    monkeypatch.setattr(sites, "_deep", lambda s, k: True)  # the old rule: every URL, every key
    slow = sites._sides(settings, llm, full)[0]
    got = {(d, k): slow(d, k) for d, keys in full.items() for k in keys}
    assert {x: fast(*x) for x in got} == got == {x: leaned(*x) for x in got}
    assert any(h is None for (_, k), h in got.items() if k.startswith("s4.example|"))
    assert any(h for (_, k), h in got.items() if k.startswith("s2.example|"))  # a docs. URL
    assert any(h is False for h in got.values())
    assert lean[bench.TODAY.isoformat()]  # a day's keys with their counts
    day = (bench.TODAY - timedelta(days=1)).isoformat()
    assert all(set(a) <= {"n", "w", "h"} for a in lean[day].values())


def test_url_host_is_urlsplits():
    from urllib.parse import urlsplit

    for url in ("https://WWW.A.example./p?q=1", "https://a.example?q=x/y", "http://u:p@b.ex:8/",
                "file:///C:/x", "about:blank", "https://[::1]:80/x", "http:/x/y", ""):
        assert sites.url_host(url) == (urlsplit(url).hostname or "").rstrip("."), url
