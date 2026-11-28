"""Keepfold's big data paths at scale, on synthetic data only (a temp folder and a temp HOME).

  vectors  a vault of --notes pages of ~10 chunks each, embedded once by a fast fake embedder
           (384 dimensions); then the time to add one page, a query's vector part (the cache
           brought up to date and every chunk scored) and a cold start (that query with no
           in-memory copy, as a new process's first one; the OS file cache is warm).
  browse   a Chromium History of --visits visits over a year imported once (no local model);
           then a live read with 50 new visits, as the app's import every 10 minutes, per step.
  vault    a vault of --pages synthetic notes and resources; the index build, a find, a read.

    uv run python scripts/bench_scale.py vectors [--notes 10000]
    uv run python scripts/bench_scale.py browse [--visits 200000]
    uv run python scripts/bench_scale.py vault [--pages 10000] [--keep]

To compare with an older checkout: `git archive <rev> src | tar -x -C build/old`, then run
this with PYTHONPATH=build/old/src.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from collections import Counter
from datetime import UTC, timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
TESTS = HERE.parent / "tests"
DIM = 384


def _home(tmp: Path) -> None:
    """A temp HOME, so the real ~/.keepfold is never read or written."""
    h = tmp / "home"
    h.mkdir(parents=True)
    for var in ("HOME", "USERPROFILE"):
        os.environ[var] = str(h)
    for var in ("APPDATA", "LOCALAPPDATA"):
        os.environ[var] = str(h / var)
    Path.home = classmethod(lambda cls: h)  # type: ignore[method-assign]


def _vault(tmp: Path) -> Path:
    r = tmp / "vault"
    shutil.copytree(TESTS / "fixtures" / "vault_mini", r)
    return r


def timed(label: str, fn, repeat: int = 1):
    """Best of `repeat` runs."""
    best, out = float("inf"), None
    for _ in range(repeat):
        t = time.perf_counter()
        out = fn()
        best = min(best, time.perf_counter() - t)
    print(f"  {label:<34}{best:8.3f} s")
    return out


# ---- vectors ------------------------------------------------------------------------------

class FastEmbed:
    """Random unit-ish vectors: the store's cost, not the model's."""

    def __init__(self):
        self.rng, self.texts = np.random.default_rng(0), 0

    def __call__(self, texts):
        self.texts += len(texts)
        return self.rng.standard_normal((len(texts), DIM), dtype=np.float32)


def _page(rng, n_chars: int) -> str:
    words = "array graph tree heap stack queue sort cache memory thread index model vector".split()
    out, size = [], 0
    while size < n_chars:
        out.append(" ".join(rng.choice(words, 12)) + "\n")
        size += len(out[-1])
    return "".join(out)


def bench_vectors(tmp: Path, notes: int) -> None:
    from keepfold import semantic
    from keepfold.catalog import Catalog
    from keepfold.index import Index
    from keepfold.vault import Vault

    root = _vault(tmp)
    rng = np.random.default_rng(1)
    (root / "resources").mkdir(exist_ok=True)
    for i in range(notes):
        (root / "resources" / f"bench-{i}.md").write_text(
            f'---\ntitle: "bench {i}"\nkind: resource\n---\n# bench {i}\n\n{_page(rng, 8300)}',
            encoding="utf-8")
    v = Vault(root)
    index = timed("index build", lambda: Index(v, Catalog.load(v)))
    embed = FastEmbed()
    ids, page, start, m = timed("first build (all chunks)",
                                lambda: semantic.vectors(index, embed, DIM))
    print(f"  {len(ids)} pages, {len(m)} chunks; on disk "
          + ", ".join(f"{p.name} {p.stat().st_size / 1e6:.0f} MB"
                      for p in sorted((root / ".keepfold").glob("vectors*")) if p.is_file()))
    q = np.asarray(embed(["query"])[0], np.float32)

    def query():
        ids, page, start, m = semantic.vectors(index, embed, DIM, semantic.RECALL_BUDGET,
                                               heads=semantic.HEAD_BUDGET)
        return semantic.best_chunks(page, start, m @ q, len(ids))
    timed("query (vector part, warm)", query, repeat=3)
    (root / "resources" / "bench-new.md").write_text(
        f'---\ntitle: "bench new"\nkind: resource\n---\n# new\n\n{_page(rng, 8300)}',
        encoding="utf-8")
    index = Index(v, Catalog.load(v))
    before = embed.texts
    timed("add one page", lambda: semantic.vectors(index, embed, DIM))
    print(f"  ({embed.texts - before} chunks embedded for it)")
    semantic._MEM.clear()
    timed("cold start (first query)", query)


# ---- vault --------------------------------------------------------------------------------

def bench_vault(tmp: Path, pages: int) -> None:
    """A --pages vault of synthetic notes and resources (~1.5 KB each, linked in chains of
    ten); then the index build, a warm find and a read. --keep leaves it for other runs."""
    from keepfold.catalog import Catalog
    from keepfold.index import Index
    from keepfold.vault import Vault

    root = _vault(tmp)
    rng = np.random.default_rng(2)

    def write():
        for d in ("notes", "resources"):
            (root / d).mkdir(exist_ok=True)
        for i in range(pages):
            kind = "note" if i % 2 else "resource"
            link = f'related: ["[[page-{i - 1}]]"]\n' if i % 10 else ""
            (root / f"{kind}s" / f"page-{i}.md").write_text(
                f'---\nid: page-{i}\ntitle: "Page {i}"\nkind: {kind}\n{link}---\n'
                f"# Page {i}\n\n{_page(rng, 1500)}", encoding="utf-8")
    timed(f"write {pages} pages", write)
    v = Vault(root)
    index = timed("index build", lambda: Index(v, Catalog.load(v)))
    hits = timed("find 'heap cache' (warm)", lambda: index.find("heap cache"), repeat=3)
    assert hits, "the synthetic pages hold every query word"
    timed("read one page", lambda: index.read(f"page-{pages // 2}"), repeat=3)
    print(f"  vault at {root}")


# ---- browse -------------------------------------------------------------------------------

def bench_browse(tmp: Path, visits: int) -> None:
    sys.path.insert(0, str(TESTS))
    from test_browse_perf import NOW, STEPS, Embed, heavy_history

    from keepfold.browse import Settings, build, sources, store
    from keepfold.vault import Vault

    root = _vault(tmp)
    for d in ("notes", "resources"):
        (root / d).mkdir(exist_ok=True)
    for hub, word in (("chess", "chess"), ("cooking", "cooking recipe")):
        (root / "notes" / f"{hub}.md").write_text(
            f'---\ntitle: "{hub.title()} hub"\nkind: note\nnote_type: structure\n'
            f'tags: ["topic"]\nrelated: []\n---\n# {hub.title()}\n', encoding="utf-8")
        for k in range(3):
            (root / "resources" / f"{hub}-{k}.md").write_text(
                f'---\ntitle: "{hub} page {k}"\nkind: resource\nrelated: ["[[{hub}]]"]\n---\n'
                f"# {hub} page\n\n{word} {word}\n", encoding="utf-8")
    local = tmp / "local"
    hist = heavy_history(local / "Google" / "Chrome" / "User Data" / "Default" / "History",
                         n=visits, n_sites=5000, days=365)
    settings = Settings(visible_to_claude=True, hidden_from_claude=("adult",))
    v = Vault(root)
    srcs = sources.discover(local, tmp / "roaming")
    embed = Embed(per_text=0)
    store.migrate(v, settings=settings)  # as the app's job: the store holds the site pages

    def run(now):
        return build.run(v, srcs, settings, embed=embed, runner=None, tz=UTC, now=now,
                         summaries=False, keep_local=False, ask=False)
    rep = timed(f"first import ({visits} visits)", lambda: run(NOW))
    print(f"  {rep.get('visits_new')} new, sites {rep.get('sites_visible')} visible "
          f"{rep.get('sites_hidden')} hidden")
    timed("nothing new", lambda: run(NOW))
    heavy_history(hist, n=50, start_id=10 * visits, days=9 / 1440,
                  end=NOW + timedelta(minutes=10))
    took: Counter = Counter()
    for mod, name in STEPS:
        fn = getattr(mod, name)

        def wrapped(*a, _fn=fn, _name=name, **k):
            t = time.perf_counter()
            try:
                return _fn(*a, **k)
            finally:
                took[_name] += time.perf_counter() - t
        setattr(mod, name, wrapped)
    rep = timed("live read, 50 new visits", lambda: run(NOW + timedelta(minutes=10)))
    assert rep["visits_new"], rep  # not == 50: "new" counts changed rows, random URLs can drop
    for k, s in took.most_common():
        if s >= 0.005:
            print(f"    {k:<32}{s:8.3f} s")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=("vectors", "browse", "vault"))
    ap.add_argument("--notes", type=int, default=10_000)
    ap.add_argument("--visits", type=int, default=200_000)
    ap.add_argument("--pages", type=int, default=10_000)
    ap.add_argument("--keep", action="store_true", help="keep the temp folder")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="keepfold-bench-scale-"))
    _home(tmp)
    try:
        import keepfold
        print(f"{args.what} with {Path(keepfold.__file__).parent}")
        if args.what == "vectors":
            bench_vectors(tmp, args.notes)
        elif args.what == "vault":
            bench_vault(tmp, args.pages)
        else:
            bench_browse(tmp, args.visits)
    finally:
        if args.keep:
            print(f"kept at {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
