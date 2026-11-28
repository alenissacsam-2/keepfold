"""Semantic recall (SPEC §0 U7): vector cosine + FTS BM25, then graph neighbours.

The embedder is injected as `embed(texts) -> (n, d) array`, so tests use a fake one and never
download the model. The real one is fastembed `BAAI/bge-small-en-v1.5` in ~/.keepfold/models,
fetched only by `keepfold model download`. A page is embedded as chunks (`_chunks`: its head,
then overlapping windows over the rest of the body) and scores by its best chunk. Vectors are a
disposable per-vault cache, the SQLite store `.keepfold/vectors.db` (`keepfold.vecstore`: a row per
page with its manifest (path, mtime_ns, size), chunk starts and float16 vectors; a save writes
the changed pages only); only pages whose file changed are re-embedded. A process reads the
store on its first query into one float32 matrix and then only the rows other processes saved
since. A damaged store is moved aside and started again (keepfold.quarantine). The older
`.keepfold/vectors-v3.npz` is read into it once and never written again; a prune that finds
rows of pages that went or changed in it deletes it (the store holds every row). A recall
embeds missing heads for at most HEAD_BUDGET seconds and
other missing chunks for at most RECALL_BUDGET; `keepfold model build` builds the rest.
Private pages and raw/ never enter the index, so they are never embedded. Background work (the
browse import) runs inside `background()`: a copy of the model capped at half the cores, at
below-normal priority; a query never waits on the cap. Each model (the embedder, the capped
copy, the reranker) is dropped after IDLE_UNLOAD seconds unused and loaded again on its next
use, and runs without onnxruntime's CPU arena (ARENA), so an idle process gives its memory back
(docs/benchmarks/memory.md). Only a person's use keeps a model: background work loads nothing
until it embeds, drops the capped copy when it ends and never restarts the main model's idle
clock (`_Held.use(touch=False)`, `loaded_embedder`). When the last model goes (an idle drop or
a background run's end), the process empties its working set (TRIM, `_trim_if_none`).

An optional cross-encoder (`RERANK_MODEL`, same cache dir, same download rule) reorders the top
`RERANK_POOL` hits; it never changes a hit's `score` or `match`. `rerank: false` in
~/.keepfold/config.yaml turns it off. `fuse="rrf"` is an eval-only alternative to the weighted sum.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import os
import re
import sqlite3
import sys
import threading
import time
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np

from keepfold import quarantine, vecstore
from keepfold.events import local_day
from keepfold.index import BM25, STOPWORDS, Index, _one_line, aliases
from keepfold.topics import is_hub, page_date, page_source
from keepfold.vault import VaultError

log = logging.getLogger(__name__)

MODEL = "BAAI/bge-small-en-v1.5"
RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
RERANK_POOL = 30
# PLAN-0.7 §5.0 fix 3: the rerank costs ~2 s a search; it runs only when the third fused score
# is within this share of the top one; the blend (`_blend`) rarely moves a clear top hit
RERANK_MARGIN = 0.2
RRF_K = 60
NOT_SET_UP = "semantic search not set up; run `keepfold model download`"
CACHE = ".keepfold/vectors.db"  # the vector store (keepfold.vecstore)
# the cache before the store: read into it once; `prune` deletes it
NPZ = ".keepfold/vectors-v3.npz"
BODY_CHARS = 1500
CHUNK_CHARS, CHUNK_OVERLAP = 1000, 200  # ~250-300 tokens: well under bge-small's 512
TEXT_VERSION = 5  # bump when _text or _chunks change, so cached vectors are rebuilt
CHUNKING = [BODY_CHARS, CHUNK_CHARS, CHUNK_OVERLAP]  # in the cache meta: a change rebuilds
RECALL_BUDGET = 1.0  # seconds of extra-chunk embedding per recall; `keepfold model build` the rest
HEAD_BUDGET = 1.0  # seconds of missing chunk-0 embedding per recall (a lost cache, an import)
TAIL_BATCH = 8  # chunks per embed call under a budget, so it overshoots by about one second
W_COS, W_KW = 0.7, 0.3
# With the query prefix, bge-small scores unrelated pages ~0.45-0.56 (median ~0.5) and real
# matches >= 0.7 (measured on a real vault): below this a page is not similar at all.
# One constant on purpose: tune it on real queries (RETR-1), never per call.
# A page's best of ~6 chunks sits higher than one head vector, so the floor went from 0.6 to
# 0.62: the quantile where the same share of (page, query) pairs passes as heads did at 0.6
# (3.6%, measured over 30 queries on a 5,214-page vault; labels unused).
MIN_COS = 0.62
MIN_KW_COVER = 0.5  # under MIN_COS, a keyword hit must match this share of the query's terms
# PLAN-0.7 §5.0 fix 2: a question's frame ("how many", "did I", "remember") is in almost no
# answer page, so it held the right page's keyword cover under MIN_KW_COVER; these words leave
# the cover and the BM25 (`_terms`) unless the query is nothing else (D20 F2's frame list).
QUESTION_WORDS = frozenset(
    "what which who whom whose when where why how did do does done was were have has had ever "
    "remember recall know knew tell show find found give about any anything something "
    "everything please many much long one two three four five six seven eight nine ten first "
    "second third last different named called say says said kind type vault note notes "
    "mine".split())
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "  # bge, queries only
DECAY = 0.5  # score factor per graph hop
MAX_LIMIT = 20
CHAT_WEIGHT = 0.6  # imported chat transcripts are long and noisy: the user's own pages win ties
# a page whose status says it no longer holds, so an old decision never outranks its successor
STALE_WEIGHT = 0.5
STALE = {("decision", "superseded"), ("decision", "retired"), ("project", "archived"),
         ("loop", "dropped")}
RECENT_BOOST, RECENT_DAYS = 0.12, 180  # a dated journal or chat page: up to +12%, fading
# a query asking how things are now: any dated page, up to +30%, fading faster
CURRENT_BOOST, CURRENT_DAYS = 0.3, 90
_CURRENT = re.compile(r"\b(?:current|currently|now|still|latest|nowadays|anymore|these days"
                      r"|at the moment)\b", re.I)


def is_current(query: str) -> bool:
    """Whether `query` asks how things stand now (`_factors`' stronger recency)."""
    return bool(_CURRENT.search(query or ""))
BATCH = 64
PROGRESS_S = 5.0  # a long embedding pass says how far it got
# vault root -> (the store's stamp as last seen, cache, stacked rows or None)
_MEM: dict[Path, tuple] = {}
_embedder = None
_EMBED_LOCK = threading.Lock()
_reranker = None
_RERANK_LOCK = threading.Lock()  # not _EMBED_LOCK: a query embedding never waits for this load


def models_dir() -> Path:
    """Beside ~/.keepfold/config.yaml (the vault pointer, cli._pointer)."""
    return Path.home() / ".keepfold" / "models"


def load_embedder(download: bool = False):
    """The model as `embed(texts) -> array`, or None when it is not downloaded. Only
    `download=True` (the `keepfold model download` command) may fetch it. The function stays;
    the model behind it is dropped when idle and loaded again by its next call (`_Held`).
    Inside `background()` it loads nothing: the function, when the download is whole
    (doctor.search_model_ready), and its first embed loads the capped copy; files that do
    not load raise `NotLoaded` there. `_embedder` (what prime and review take as "this
    process has it") is set only by a load outside `background()`."""
    global _embedder
    with _EMBED_LOCK:  # the server's warm-up thread and a first tool call may race
        if _embedder is not None:
            return _embedder
        if not download and not models_dir().is_dir():
            return None
        if not download and getattr(_bg, "on", False):
            from keepfold import doctor  # here: its imports cost 0.8 s (the app has it already)

            return _embed_real if doctor.search_model_ready() else None
        try:
            model = _text_embedding(download)
        except Exception:
            if download:
                raise
            return None
        _main.put(model)
        _embedder = _embed_real
        return _embedder


def _text_embedding(download: bool = False, threads: int | None = None):
    from fastembed import TextEmbedding  # slow import: keep it off CLI and server start

    return TextEmbedding(MODEL, cache_dir=str(models_dir()), local_files_only=not download,
                         threads=threads, enable_cpu_mem_arena=ARENA)


class NotLoaded(VaultError):
    """Background work's first embed found the model's files whole but not loading (corrupt):
    the caller goes on without the model, as when load_embedder returns None."""

    def __init__(self):
        super().__init__("not_set_up", NOT_SET_UP)


def _embed_real(texts) -> np.ndarray:
    """The real model's embed: in `background()` the capped copy, when it loads."""
    bg = getattr(_bg, "on", False)
    if bg:
        with _capped.use() as m:
            if m:
                return np.asarray(list(m.embed(list(texts))), dtype=np.float32)
    try:
        with _main.use(touch=not bg) as m:
            return np.asarray(list(m.embed(list(texts))), dtype=np.float32)
    except Exception as e:
        if bg and _main.model is None:  # its load failed, not an embed
            raise NotLoaded() from e
        raise


def _embed_quiet(texts) -> np.ndarray:
    """The main model for background work that found it in memory (`loaded_embedder`): its use
    does not restart the idle clock, so only a person's own use keeps the model loaded."""
    with _main.use(touch=False) as m:
        return np.asarray(list(m.embed(list(texts))), dtype=np.float32)


def loaded_embedder():
    """For work that embeds only with a model already in memory (the safety net, review's
    scorer): `_embed_quiet` while the main model is loaded, else None; never a load. An
    `_embedder` other than the real model's (a test's) is returned as it is.
    shortcut: a drop between this check and the embed loads it again for that call (dropped at
    the next idle check, as its clock was not restarted)."""
    if _embedder is not None and _embedder is not _embed_real:
        return _embedder
    return _embed_quiet if _main.model else None


# ---- idle unload: a model unused for IDLE_UNLOAD seconds is dropped ---------------------------

IDLE_UNLOAD = 600.0  # seconds unused before a model is dropped; its next use loads it again
IDLE_CHECK = 60.0  # seconds between the idle thread's checks
# onnxruntime's CPU arena keeps the largest batch's buffers for the session's life (after an
# index build, 1.6 GB); without it a run frees them. A query is no slower; bulk embedding is
# 7-40% slower (docs/benchmarks/memory.md)
ARENA = False
# Windows: once the last model is dropped, the idle process empties its working set (`_trim`)
TRIM = True


class _Held:
    """A model made by `load()` on first use and dropped by `drop_idle` once unused for
    IDLE_UNLOAD seconds; never while a call holds it (`use`). A load that returned a falsy
    model (the capped copy that failed) stays as it is: never dropped, never tried again.
    A use with `touch=False` (background work) leaves the idle clock where a person's last use
    set it: background work never keeps a model, and one no person used goes at the next
    check."""

    def __init__(self, load):
        self.load, self.lock = load, threading.Lock()
        self.model, self.used, self.busy = None, 0.0, 0

    def put(self, model) -> None:
        with self.lock:
            self.model, self.used = model, time.monotonic()
        _watch()

    @contextmanager
    def use(self, touch: bool = True):
        with self.lock:  # one load, not two: a racing call waits for it
            if self.model is None:
                self.model = self.load()
                _watch()
            self.busy += 1
            m = self.model
        try:
            yield m
        finally:
            with self.lock:
                self.busy -= 1
                if touch:
                    self.used = time.monotonic()

    def drop(self, now: float) -> bool:
        with self.lock:
            if not self.model or self.busy or now - self.used < IDLE_UNLOAD:
                return False
            self.model = None
            return True


def drop_idle(now: float | None = None) -> list[str]:
    """Drop every model unused for IDLE_UNLOAD seconds (`_watch`'s thread calls it every
    IDLE_CHECK); the names of those dropped."""
    now = time.monotonic() if now is None else now
    return [name for name, h in (("embedder", _main), ("background copy", _capped),
                                 ("reranker", _ranker)) if h.drop(now)]


def idle_pass(now: float | None = None) -> list[str]:
    """One check of the idle thread: `drop_idle`, then, when that dropped the last model held,
    `_trim`. The names of the models dropped."""
    dropped = drop_idle(now)
    if dropped:
        _trim_if_none()
    return dropped


def _trim_if_none() -> None:
    """`_trim` once no model is held (TRIM): after an idle drop or a background run's end."""
    if TRIM and not any(h.model for h in (_main, _capped, _ranker)):
        _trim()


def _trim() -> None:
    """Empty this process's working set (Windows; nothing elsewhere). Its pages go to the
    standby list, where other programs may have them; the next call faults back only what it
    touches. Private bytes do not change: what the process still holds is in them
    (docs/benchmarks/memory.md, M3)."""
    if sys.platform != "win32":
        return
    import ctypes

    k = ctypes.WinDLL("kernel32")  # its own instance: the argtypes below stay local
    k.GetCurrentProcess.restype = ctypes.c_void_p
    k.K32EmptyWorkingSet.argtypes = [ctypes.c_void_p]
    k.K32EmptyWorkingSet(k.GetCurrentProcess())


_WATCH_LOCK = threading.Lock()
_watching = False


def _watch() -> None:
    """Start the idle thread (once per process, on the first model load)."""
    global _watching
    with _WATCH_LOCK:
        if _watching:
            return
        _watching = True

    def loop():
        while True:
            time.sleep(IDLE_CHECK)
            idle_pass()

    threading.Thread(target=loop, name="keepfold-model-idle", daemon=True).start()


# ---- background embedding: half the cores, below-normal priority -----------------------------

BG_THREADS = max(1, (os.cpu_count() or 2) // 2)  # a background build leaves the rest to the owner
_bg = threading.local()


@contextmanager
def background():
    """Embedding in this thread is background work while inside: the real model's calls go to
    a second copy capped at BG_THREADS threads, all at below-normal priority (Windows), so the
    PC stays quick. A query in another thread keeps the full model. Nests. The outermost exit
    drops the copy (unless another thread's background work holds it): it served only this.
    When that leaves no model, the working set is emptied too (`_trim_if_none`)."""
    if getattr(_bg, "on", False):
        yield
        return
    _bg.on = True
    old = _priority(_BELOW_NORMAL)
    try:
        yield
    finally:
        _bg.on = False
        if _capped.drop(math.inf):
            _trim_if_none()
        if old is not None:
            _priority(old)


def _capped_load():
    """The capped copy of the model (made on first background use, again after an idle
    drop), or False when it cannot be loaded: the full model does background work then."""
    before = _threads()
    try:
        m = _text_embedding(threads=BG_THREADS)
        list(m.embed(["keepfold"]))  # onnxruntime has made its threads by now
    except Exception as e:
        log.warning("background embedding uses the full model: %s", type(e).__name__)
        return False
    # shortcut: a thread another part of the process made in this second is lowered
    # too; harmless, as background work is all this process does then
    _lower(_threads() - before)
    return m


# the lambdas look the loaders up at load time, so tests can replace them
_main = _Held(lambda: _text_embedding())
_capped = _Held(lambda: _capped_load())


_BELOW_NORMAL = -1  # THREAD_PRIORITY_BELOW_NORMAL


def _kernel32():
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32")  # its own instance: the argtypes below stay local
    H = wintypes.HANDLE
    k.GetCurrentThread.restype = k.OpenThread.restype = H
    k.CreateToolhelp32Snapshot.restype = H
    k.SetThreadPriority.argtypes = [H, ctypes.c_int]
    k.GetThreadPriority.argtypes = k.CloseHandle.argtypes = [H]
    k.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.Thread32First.argtypes = k.Thread32Next.argtypes = [H, ctypes.c_void_p]
    return k


def _priority(p: int) -> int | None:
    """Set this thread's priority (Windows); the one it had, None elsewhere."""
    if sys.platform != "win32":
        return None
    k = _kernel32()
    h = k.GetCurrentThread()
    old = k.GetThreadPriority(h)
    k.SetThreadPriority(h, p)
    return old


def _threads() -> set[int]:
    """The ids of this process's threads (Windows; empty elsewhere)."""
    if sys.platform != "win32":
        return set()
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):  # THREADENTRY32
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("tid", wintypes.DWORD), ("pid", wintypes.DWORD),
                    ("base", wintypes.LONG), ("delta", wintypes.LONG),
                    ("flags", wintypes.DWORD)]

    k = _kernel32()
    snap = k.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
    if not snap or snap == ctypes.c_void_p(-1).value:
        return set()
    out, e, pid = set(), Entry(), k.GetCurrentProcessId()
    e.size = ctypes.sizeof(Entry)
    try:
        ok = k.Thread32First(snap, ctypes.byref(e))
        while ok:
            if e.pid == pid:
                out.add(e.tid)
            ok = k.Thread32Next(snap, ctypes.byref(e))
    finally:
        k.CloseHandle(snap)
    return out


def _lower(tids) -> None:
    """Below-normal priority for these threads of this process (Windows)."""
    if not tids:
        return
    k = _kernel32()
    for t in tids:
        if h := k.OpenThread(0x0020, False, t):  # THREAD_SET_INFORMATION
            k.SetThreadPriority(h, _BELOW_NORMAL)
            k.CloseHandle(h)


def rerank_on() -> bool:
    """The `rerank` switch in ~/.keepfold/config.yaml (default on)."""
    from keepfold.taint import config

    return config().get("rerank") is not False


def load_reranker(download: bool = False, config: bool = True):
    """The cross-encoder as `score(query, texts) -> list[float]`, or None when it is not
    downloaded, fails to load, or is switched off (`config=False` ignores the switch). Only
    `download=True` may fetch it."""
    global _reranker
    if config and not download and not rerank_on():
        return None
    if _reranker is not None:
        return _reranker
    with _RERANK_LOCK:  # warm-up and a first Chat turn may race: one load, not two
        if _reranker is not None:
            return _reranker
        # fastembed's HF cache folder; checked first so an absent model logs no fastembed ERROR
        if not download and not (models_dir()
                                 / f"models--{RERANK_MODEL.replace('/', '--')}").is_dir():
            return None
        try:
            model = _cross_encoder(download)
        except Exception:
            if download:
                raise
            return None
        _ranker.put(model)
        _reranker = _score_real
        return _reranker


def _cross_encoder(download: bool = False):
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return TextCrossEncoder(RERANK_MODEL, cache_dir=str(models_dir()),
                            local_files_only=not download, enable_cpu_mem_arena=ARENA)


def _score_real(query, texts) -> list[float]:
    with _ranker.use() as m:
        return [float(x) for x in m.rerank(query, list(texts))]


_ranker = _Held(lambda: _cross_encoder())


def is_chat(n: dict) -> bool:
    """An imported chat page: a resource whose `Timeline:` line names its `Source:` app."""
    return n["kind"] == "resource" and bool(page_source(n["body"]))


def _today() -> str:
    """The local day recency counts from (tests patch it)."""
    return local_day(datetime.now(UTC))


def older_versions(index: Index) -> frozenset[str]:
    """RR-18: imported pages a newer import of the same file replaced: the same `Source:`
    label, `From:` origin and title on the header line (sources.HEADER) and an older Date
    (`x-2` is newer than `x` on the same day). A read-time fold; nothing is stored."""
    from keepfold.sources import HEADER  # lazy: sources pulls in the import machinery
    groups: dict[tuple, list] = {}
    for id, n in index._notes.items():
        if n["kind"] == "resource" and (m := HEADER.search(n["body"][:2000])) and m.group(3):
            groups.setdefault((m.group(1), m.group(3), n["title"]), []).append(
                (m.group(2), len(id), id))
    return frozenset(i for g in groups.values() for *_, i in sorted(g)[:-1])


def _factors(index: Index, id: str, chats: bool, quiet: dict[str, float] | None,
             current: bool = False, older: frozenset[str] = frozenset()) -> dict:
    """A page's score factors other than 1 after the floors: `quiet`, its harvest.quiet weight
    (0 hides a folded copy even with `chats`), else `chat`, CHAT_WEIGHT for a chat page (`chats`
    lifts both but 0); `status`, STALE_WEIGHT for a STALE status; `older_version`, STALE_WEIGHT
    for a page in `older` (`older_versions`); `recency`, up to
    1 + RECENT_BOOST for a journal or chat page dated in the last months, or with `current`
    (`is_current` query) up to 1 + CURRENT_BOOST for any dated page."""
    q = (quiet or {}).get(id)
    if q == 0:
        return {"quiet": 0.0}
    n = index._notes[id]
    chat = is_chat(n)
    f = {}
    if q is not None:
        if not chats:
            f["quiet"] = q
    elif chat and not chats:
        f["chat"] = CHAT_WEIGHT
    if (n["kind"], str(n["fields"].get("status"))) in STALE:  # str: a YAML list is unhashable
        f["status"] = STALE_WEIGHT
    if id in older:
        f["older_version"] = STALE_WEIGHT
    if (current or chat or n["kind"] == "journal") \
            and (d := page_date(id, n["fields"], n["body"])):
        boost, days = (CURRENT_BOOST, CURRENT_DAYS) if current else (RECENT_BOOST, RECENT_DAYS)
        try:
            age = max(0, (date.fromisoformat(_today()) - date.fromisoformat(d)).days)
            f["recency"] = 1 + boost * math.exp(-age / days)
        except ValueError:  # a malformed day (2026-13-45) gets no boost
            pass
    return f


def _weight(index: Index, id: str, chats: bool, quiet: dict[str, float] | None,
            current: bool = False, older: frozenset[str] = frozenset()) -> float:
    """The product of the page's `_factors`; 0 drops it."""
    return math.prod(_factors(index, id, chats, quiet, current, older).values(), start=1.0)


def rerank(index: Index, query: str, hits: list[dict], score, chats: bool = False,
           quiet: dict[str, float] | None = None, out: dict | None = None) -> list[dict]:
    """`hits` reordered by the cross-encoder over each hit's best chunk (`at`; chunk 0 without
    one), its sigmoid times the page's `_weight`, blended with the first-stage rank r (R3d,
    QMD's rule: _blend(r) / r + (1 - _blend(r)) x that, so the top few stay unless the
    cross-encoder is clearly surer); on any failure, as they were. Each hit's `score` and
    `match` are untouched (the Honest Double check reads them)."""
    if len(hits) < 2:
        return hits
    try:
        s = score(query, [_chunk_text(index._notes[h["id"]], h.get("at", 0)) for h in hits])
    except Exception as e:  # a broken model must never break recall
        log.warning("rerank skipped: %s", e)
        return hits
    # logits can be negative, so weight their sigmoid (same order) rather than the logit
    now, older = is_current(query), older_versions(index)
    s = [1 / (1 + np.exp(-x)) * _weight(index, h["id"], chats, quiet, now, older)
         for x, h in zip(s, hits, strict=True)]
    if out is not None:  # `--explain` shows the weighted rerank score per page
        out.update({h["id"]: float(x) for x, h in zip(s, hits, strict=True)})
    b = [_blend(r) / r + (1 - _blend(r)) * x for r, x in enumerate(s, 1)]
    return [h for _, h in sorted(zip(b, hits, strict=True), key=lambda p: -p[0])]


def _blend(r: int) -> float:
    """The first stage's weight at rank r in the rerank blend (QMD's numbers)."""
    return 0.75 if r <= 3 else 0.60 if r <= 10 else 0.40


def _unit(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) or 1.0)


def _filled(body: str) -> str:
    """The body without headings of empty sections: template headings ("## How I write to
    them" on a fresh people page) otherwise match queries they say nothing about."""
    lines, out = body.splitlines(), []
    for i, line in enumerate(lines):
        if h := _HEAD.match(line):
            nxt = next((x for x in lines[i + 1:] if x.strip()), "")
            sub = _HEAD.match(nxt)
            if not nxt or (sub and len(sub[1]) <= len(h[1])):  # nothing under it
                continue
        out.append(line)
    return "\n".join(out)


_HEAD = re.compile(r"^(#{1,6})\s")


def _text(n: dict) -> str:
    summary = n["fields"].get("summary") or ""
    return f"{n['title']}\n{summary}\n{_filled(n['body'].strip())[:BODY_CHARS]}"


HEADER_CHARS = 150


def _header(title: str, path: list[str], day: str, kind: str) -> str:
    """A deep chunk's first line, `title › heading path · day · kind`, at most HEADER_CHARS
    (the title and path are cut first), so a chunk read alone says where it is from."""
    tail = " · ".join(x for x in (day, kind) if x)
    head = " › ".join([" ".join(title.split()), *path])
    return f"{head[:HEADER_CHARS - len(tail) - 3]} · {tail}" if tail else head[:HEADER_CHARS]


def _heads(body: str, code: bool = False):
    """The heading lines of `body` outside fenced code blocks; with `code`, the heading-like
    lines inside them instead (a "# install deps" comment: no heading, C13)."""
    fences = [m.start() for m in _FENCE.finditer(body)]  # inside one: an odd count before
    return (h for h in _HEAD_LINE.finditer(body)
            if bisect.bisect(fences, h.start()) % 2 == code)


def _chunks(n: dict) -> list[tuple[int, str]]:
    """(start in the filled body, text) per chunk. Chunk 0 is `_text` (start 0). The rest are
    windows of about CHUNK_CHARS over the body from BODY_CHARS - CHUNK_OVERLAP on, each under
    a `_header` line (the last 2 levels of headings above the window start), overlapping the
    one before by CHUNK_OVERLAP and cut at the best boundary in the window's final
    CHUNK_OVERLAP characters (`_cut`: turn, paragraph, line, sentence, then space)."""
    body = _filled(n["body"].strip())
    out = [(0, _text(n))]
    if len(body) <= BODY_CHARS:
        return out
    day = page_date("", n["fields"], n["body"])
    heads = _heads(body)
    h, stack = next(heads, None), []  # [(level, "## heading")] open above the window start

    def header(s: int) -> str:
        nonlocal h
        while h is not None and h.start() < s:  # one pass: windows only move forward
            stack[:] = [x for x in stack if x[0] < len(h[1])] + [(len(h[1]), h[0].strip())]
            h = next(heads, None)
        path = [t for _, t in stack if t.lstrip("#").strip() != n["title"]]  # not "# Title"
        return _header(n["title"], path[-2:], day, n["kind"])

    s = BODY_CHARS - CHUNK_OVERLAP
    while True:
        end = s + CHUNK_CHARS
        if end >= len(body) - CHUNK_OVERLAP:  # the rest, rather than a short last window
            out.append((s, f"{header(s)}\n{body[s:]}"))
            break
        cut = _cut(body[end - CHUNK_OVERLAP:end])
        if cut > 0:
            end += cut - CHUNK_OVERLAP
        out.append((s, f"{header(s)}\n{body[s:end]}"))
        s = end - CHUNK_OVERLAP
    return out


def _cut(seg: str) -> int:
    """Where in `seg` (a window's last CHUNK_OVERLAP characters) the window ends, by a ladder:
    before the last turn, else at a paragraph break, a line end, after a sentence's end, a
    space, so a word is never split; -1 (a hard cut) only when `seg` has none of them."""
    turns = [m.start() for m in _TURN.finditer(seg)]
    if turns:
        return turns[-1]
    for at in (seg.rfind("\n\n"), seg.rfind("\n"),
               max((m.end() for m in _SENTENCE.finditer(seg)), default=-1), seg.rfind(" ")):
        if at > 0:
            return at
    return -1


_SENTENCE = re.compile(r"[.!?][\"')\]]*(?=\s)")
_HEAD_LINE = re.compile(r"^(#{1,6})[ \t][^\n]*", re.M)
_FENCE = re.compile(r"^[ \t]*(?:```|~~~)", re.M)
# a turn starts at its `### <time>` line when it has one, else at `**Me:**`
_TURN = re.compile(r"\n(?:### [^\n]*\n)?\*\*Me:\*\*|\n### ")


PASSAGE = 300  # RR-12: characters of a recall hit's best passage


def passage(n: dict, at: int, query: str) -> tuple[int, str]:
    """RR-12: (offset in the page body, at a line start; the best passage, one line of at
    most PASSAGE characters) from the best chunk starting at `at` (an offset in the filled
    body): its first line holding a query word, else its start; else the first line holding
    a query word, else the body's start."""
    body = n["body"]
    words = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 2 and t not in STOPWORDS]
    # the matching line, not the chunk's first: a chunk starts up to CHUNK_CHARS before its
    # match, and its first 60 characters may repeat earlier in the page (a log's same line)
    chunk = _chunk_text(n, at).split("\n", 1)[-1] if at else ""  # without the header line
    line = next((ln.strip() for ln in chunk.splitlines()
                 if ln.strip() and any(w in ln.lower() for w in words)), "")
    probe = line or (_filled(body.strip())[at:at + 60] if at else "")
    pos = body.find(probe) if probe.strip() else -1
    if pos < 0:
        low = body.lower()
        found = [i for t in words if (i := low.find(t)) >= 0]
        pos = min(found) if found else len(body) - len(body.lstrip())
    pos = body.rfind("\n", 0, pos) + 1
    return pos, _one_line(body[pos:pos + 2 * PASSAGE], PASSAGE)


def _chunk_text(n: dict, at: int) -> str:
    """The text of the chunk starting at `at`, as embedded; chunk 0 when there is none."""
    return next((t for s, t in _chunks(n) if s == at), _text(n)) if at else _text(n)


def _pages(index: Index) -> dict[str, tuple]:
    return {id: (index._rel(p), *index._stats[p])  # not relative_to: most of a warm query
            for p, id in index._ids.items() if id in index._notes}


# The cache: page id -> (manifest (path, mtime_ns, size), full (every chunk built), chunk
# starts int32 (k,), unit vectors (k, dim)); chunk 0 is always the first row. In memory the
# vectors are views of one float32 matrix (`vecstore.stack`); on disk, the rows of CACHE.


def _want(dim: int) -> dict:
    return {"model": MODEL, "text": TEXT_VERSION, "chunk": CHUNKING, "dim": dim}


def _npz_read(root: Path) -> dict:
    """The .npz cache from before the store, or {} when it is missing, unreadable or built
    with other settings."""
    try:
        with np.load(root / NPZ, allow_pickle=False) as z:
            meta = json.loads(z["meta"].item())
            if [meta.get("model"), meta.get("text"), meta.get("chunk")] == [
                    MODEL, TEXT_VERSION, CHUNKING]:
                ids, full, page = [str(i) for i in z["ids"]], z["full"], z["page"]
                start, vecs = z["start"], z["vecs"].astype(np.float32)
                cut = np.searchsorted(page, np.arange(len(ids) + 1))
                return {i: (tuple(meta["pages"][i]), bool(full[k]), start[cut[k]:cut[k + 1]],
                            vecs[cut[k]:cut[k + 1]]) for k, i in enumerate(ids)}
    except (OSError, ValueError, KeyError) as e:
        if not isinstance(e, FileNotFoundError):
            log.warning("the old vector cache is unreadable: %s", e)
    return {}


def _connect(root: Path, dim: int, write: bool = True, vault=None):
    """The store, its settings checked (other ones empty it; a damaged one is moved aside
    through `vault` and made again) and the .npz read into it once (never written again:
    `prune` deletes it). With `write` False (a dry run) nothing is made, moved or changed:
    None when there is no store or it holds other settings."""
    p = root / CACHE
    if not write:
        if not p.is_file():
            return None
        con = vecstore.connect(p, create=False)
        if vecstore.get_meta(con, "settings") != _want(dim):
            con.close()
            return None
        return con
    con = vecstore.connect(p, vault=vault)
    try:
        vecstore.prepare(con, _want(dim))
        if not vecstore.get_meta(con, "migrated"):  # idempotent: a rerun upserts the same rows
            vecstore.put(con, {i: (json.dumps(list(e[0])), e[1], e[2], e[3])
                               for i, e in _npz_read(root).items() if e[3].shape[1] == dim})
            with vecstore.tx(con):
                vecstore.set_meta(con, "migrated", True)
    except BaseException:
        con.close()
        raise
    return con


def _code_heads_once(con, index: Index) -> None:
    """Once per store: delete the rows of pages with a "# line" in a fenced code block, whose
    chunk headers C13 changed (the manifest key would keep their old vectors until an edit);
    the refresh re-embeds just those. The "c13" meta flag makes it run once; the deletes bump
    the store's stamp, so every process's `_sync` drops the rows from memory too."""
    if vecstore.get_meta(con, "c13"):
        return
    vecstore.delete(con, [i for i in vecstore.manifests(con) if i in index._notes and next(
        _heads(_filled(index._notes[i]["body"].strip()), code=True), None)])
    with vecstore.tx(con):
        vecstore.set_meta(con, "c13", True)


def _sync(con, dim: int, st0, cache: dict) -> tuple[tuple, bool]:
    return vecstore.sync(con, dim, st0, cache, lambda m: tuple(json.loads(m)))


def _save(con, cache: dict, changed: set, gone) -> None:
    """Upsert the `changed` pages and delete the `gone` ones; a disposable cache, so a failure
    is a warning (a later call embeds what is missing)."""
    try:
        vecstore.delete(con, [i for i in gone if i not in cache])  # a changed page: upserted
        vecstore.put(con, {i: (json.dumps(list(cache[i][0])), *cache[i][1:])
                           for i in changed if i in cache})
    except sqlite3.Error as e:
        log.warning("vector cache not saved: %s", e)


def _read(root: Path) -> dict:
    """Every stored row: {id: (manifest, full, starts, float32 vectors)}; {} without a store."""
    if not (root / CACHE).is_file():
        return {}
    with closing(vecstore.connect(root / CACHE, create=False)) as con:
        dim = (vecstore.get_meta(con, "settings") or {}).get("dim")
        return {i: (tuple(json.loads(m)), full, s, v.astype(np.float32))
                for i, m, full, s, v in vecstore.rows(con, dim)} if dim else {}


def stamp(root: Path) -> tuple | None:
    """The store's change stamp, None when there is none or it is unreadable (the brain
    map's cache key)."""
    if not (root / CACHE).is_file():
        return None
    try:
        with closing(vecstore.connect(root / CACHE, create=False)) as con:
            return vecstore.stamp(con)
    except sqlite3.Error:
        return None


def heads(root: Path) -> tuple[list[str], np.ndarray]:
    """(ids, float32 chunk-0 vectors) of every stored page, read now (the brain map's input:
    one row per page, not the whole matrix). FileNotFoundError without a store."""
    if not (root / CACHE).is_file():
        raise FileNotFoundError(root / CACHE)
    with closing(vecstore.connect(root / CACHE, create=False)) as con:
        dim = (vecstore.get_meta(con, "settings") or {}).get("dim")
        return vecstore.heads(con, dim) if dim else ([], np.zeros((0, 0), np.float32))


def prune(vault) -> int:
    """Delete the rows of pages that went or changed, with no model needed (a forget or a
    settings change must not leave a page's vectors behind; a changed page is embedded again
    later). The old .npz is deleted when it holds such a row: the store read it in, so it only
    kept forgotten pages (G-5-5). Rows deleted from the store."""
    from keepfold.catalog import Catalog

    root = vault.root
    if not ((root / CACHE).is_file() or (root / NPZ).is_file()):
        return 0
    pages = _pages(Index(vault, Catalog.load(vault)))
    n = 0
    if (root / CACHE).is_file():
        with closing(vecstore.connect(root / CACHE, vault=vault)) as con:
            n = vecstore.delete(con, [i for i, m in vecstore.manifests(con).items()
                                      if pages.get(i) != tuple(json.loads(m))])
    if any(pages.get(i) != e[0] for i, e in _npz_read(root).items()):
        vault.remove(root / NPZ)
    return n


def _embed(embed, texts: list[str]) -> np.ndarray:
    v = np.asarray(embed(texts), dtype=np.float32)
    return v / np.where((n := np.linalg.norm(v, axis=1, keepdims=True)) > 0, n, 1.0)


def embed_all(embed, texts: list[str], batch: int = BATCH, say=None,
              what: str = "texts") -> np.ndarray:
    """`_embed` of many texts in batches, shortest first so a batch pads little (the vectors
    are the same; 1.4x faster on titles), returned in input order. `say` gets a counts-only
    line every PROGRESS_S seconds."""
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    parts, last = [], time.monotonic()
    for s in range(0, len(order), batch):
        parts.append(_embed(embed, [texts[i] for i in order[s:s + batch]]))
        if say and time.monotonic() - last >= PROGRESS_S:
            say(f"embedded {s + len(parts[-1])} of {len(texts)} {what}")
            last = time.monotonic()
    if not parts:
        return np.zeros((0, 1), np.float32)
    got = np.concatenate(parts)
    out = np.empty_like(got)
    out[order] = got
    return out


def vectors(index: Index, embed, dim: int, budget: float | None = None,
            save_every: float | None = None, progress=None, heads: float | None = None,
            only: set[str] | None = None, save: bool = True):
    """Bring the vault's cache up to date and return (page ids, row -> page index, row chunk
    start, unit row vectors), rows grouped by page.

    Chunk 0 of new and changed pages gets at most `heads` seconds per call (None: all of
    them); a page without it is left out of the rows until a later call or `keepfold model
    build` embeds it. Gone pages are dropped. The other chunks get at most `budget` seconds
    per call (None: no limit, for `keepfold model build`); until all of a page's chunks are
    built it scores by those it has.
    With `save_every` (seconds) the build saves as it goes, so an interrupted one resumes;
    `progress(done, total)` counts the chunks built in this call. With `only` (page ids) the
    missing chunk 0 of no other page is embedded in this call (the browse import needs the hub
    members only; recall and `keepfold model build` embed the rest). With `save=False` (a dry
    run) nothing is written and the in-memory cache is left as it was.

    The store is read lazily: the whole of it on a process's first call, then only the rows
    other processes saved since (its change stamp); a save writes the changed pages only."""
    root = index.vault.root
    pages = _pages(index)
    st, cache, stack = _MEM.get(root, (None, {}, None))
    cache = dict(cache)
    con = None
    try:
        con = _connect(root, dim, save, index.vault)
        if save and con is not None:
            _code_heads_once(con, index)
        if con is None:  # a dry run with no usable store: nothing cached
            st, cache, stack = None, {}, None
        else:
            st, moved = _sync(con, dim, st, cache)
            stack = None if moved else stack
    except (sqlite3.Error, OSError) as e:  # a disposable cache: embed what this call can
        log.warning("vector cache not read: %s", e)
        if con is not None:
            con.close()
        con = None
        if save and quarantine.torn(e):  # damaged past the first open: the next call starts over
            vecstore.heal(root / CACHE, e, index.vault)
    try:
        return _vectors(index, embed, dim, pages, st, cache, stack, con, budget, save_every,
                        progress, heads, only, save)
    finally:
        if con is not None:
            con.close()


def _vectors(index: Index, embed, dim: int, pages: dict, st, cache: dict, stack, con, budget,
             save_every, progress, heads, only, save: bool):
    root = index.vault.root
    save = save and con is not None
    gone = [i for i, e in cache.items() if pages.get(i) != e[0]]  # went, changed or private now
    for i in gone:
        del cache[i]
    dirty, changed = bool(gone), set()
    todo = [i for i in pages if i not in cache and (only is None or i in only)]
    t0, step = time.monotonic(), (BATCH if heads is None else TAIL_BATCH)
    for s in range(0, len(todo), step):
        if heads is not None and time.monotonic() - t0 >= heads:
            break
        batch = todo[s:s + step]
        vecs = _embed(embed, [_text(index._notes[i]) for i in batch])
        for i, v in zip(batch, vecs, strict=True):
            cache[i] = (pages[i], False, np.zeros(1, np.int32), v[None])
        changed.update(batch)
        dirty = True
    pending = [i for i in pages if i in cache and not cache[i][1]]
    work = {}
    if progress:  # counted up front only for the CLI build
        work = {i: _chunks(index._notes[i]) for i in pending}
        total = sum(len(c) for c in work.values()) - sum(len(cache[i][2]) for i in pending)
    t0 = last = time.monotonic()
    done, step = 0, (BATCH if budget is None else TAIL_BATCH)
    for i in pending:
        meta, _, have, vecs = cache[i]
        built = set(have.tolist())
        rest = [c for c in work.pop(i, None) or _chunks(index._notes[i]) if c[0] not in built]
        if not rest:  # short page: chunk 0 is all of it
            cache[i], dirty = (meta, True, have, vecs), True
            changed.add(i)
        while rest and (budget is None or time.monotonic() - t0 < budget):
            part, rest = rest[:step], rest[step:]
            have = np.concatenate([have, np.array([s for s, _ in part], np.int32)])
            vecs = np.concatenate([vecs, _embed(embed, [t for _, t in part])])
            cache[i], dirty, done = (meta, not rest, have, vecs), True, done + len(part)
            changed.add(i)
            if progress:
                progress(done, total)
            if save and save_every is not None and time.monotonic() - last >= save_every:
                _save(con, cache, changed, gone)
                changed, gone = set(), []
                last = time.monotonic()
        if budget is not None and time.monotonic() - t0 >= budget:
            break
    if save and (changed or gone):
        _save(con, cache, changed, gone)
    if save and dirty:  # read back what this call and any other process saved since
        try:
            st = _sync(con, dim, st, cache)[0]
        except sqlite3.Error as e:
            log.warning("vector cache not read back: %s", e)
        for i in [i for i, e in cache.items() if pages.get(i) != e[0]]:
            del cache[i]  # another process's newer page: in the store, not in this index yet
    if dirty or stack is None:
        stack = vecstore.stack(cache, dim)
    if save:
        _MEM[root] = (st, cache, stack)
    return stack


def best_chunks(page: np.ndarray, start: np.ndarray, sims: np.ndarray,
                n: int) -> tuple[np.ndarray, np.ndarray]:
    """Per page (rows grouped by page; every page has chunk 0): the best chunk's cosine and
    its start offset."""
    if not n:
        return np.zeros(0, np.float32), np.zeros(0, np.int32)
    best = np.maximum.reduceat(sims, np.searchsorted(page, np.arange(n)))
    rows = np.flatnonzero(sims == best[page])
    _, first = np.unique(page[rows], return_index=True)
    return best, start[rows[first]]


def _terms(query: str) -> list[str]:
    """Distinct FTS terms without stopwords and, unless nothing else is left, QUESTION_WORDS;
    short words match exactly: a prefix like "i"* would hit most of the vault."""
    words = [t for t in re.findall(r"\w+", (query or "").lower()) if t not in STOPWORDS]
    words = [t for t in words if t not in QUESTION_WORDS] or words
    return list(dict.fromkeys(f'"{t}"' + ("*" if len(t) > 3 else "") for t in words))


def _cover(index: Index, query: str) -> dict[str, float]:
    """Page id -> share of the query's terms it matches (pages matching none are absent)."""
    terms = _terms(query)
    n: dict[str, int] = {}
    for t in terms:
        for (i,) in index.db.execute("SELECT id FROM fts WHERE fts MATCH ?", (t,)):
            n[i] = n.get(i, 0) + 1
    return {i: c / len(terms) for i, c in n.items()}


def _keyword(index: Index, query: str) -> dict[str, float]:
    """BM25 over the FTS index, any non-stopword term (OR), scaled so the best hit is 1."""
    terms = _terms(query)
    if not terms:
        return {}
    rows = index.db.execute(f"SELECT id, {BM25} FROM fts WHERE fts MATCH ?",
                            (" OR ".join(terms),)).fetchall()
    s = {i: -b for i, b in rows}
    top = max(s.values(), default=0.0)
    return {i: v / top for i, v in s.items()} if top > 0 else {}


def _neighbours(index: Index, id: str):
    return index.db.execute("SELECT dst, type FROM edges WHERE src = ?1 UNION "
                            "SELECT src, type FROM edges WHERE dst = ?1", (id,)).fetchall()


MENTIONED_BY = 8  # R3a: the pages saying a page's name that count as linked, best BM25 first
_WORD = re.compile(r"[\w-]+")  # a hyphenated id is one word: "dec-x" is not in "dec-x-v2"


def _names(id: str, title: str, also: list[str] = ()) -> list[tuple[str, ...]]:
    """The words a page is named by in other pages' text: its title without a trailing
    "(…)" ("Paper (PDF)" is said "Paper") and an id with a hyphen and a letter; a one-word
    name only of 5+ characters. R13d: `also` (its `aliases:`) are names too, of 2+ characters
    (an owner chose them: "DSA" names the page), matched as whole words like the rest."""
    out = []
    for name in (re.sub(r"\s*\([^)]*\)\s*$", "", title), id if "-" in id and re.search(
            r"[^\W\d_]", id) else ""):
        w = tuple(_WORD.findall(name.lower()))
        if (len(w) > 1 or (w and len(w[0]) >= 5)) and w not in out:
            out.append(w)
    for name in also:
        w = tuple(_WORD.findall(name.lower()))
        if len("".join(w)) >= 2 and w not in out:
            out.append(w)
    return out


def _holds(words: list[str], name: tuple[str, ...]) -> bool:
    return any(tuple(words[k:k + len(name)]) == name for k, w in enumerate(words) if w == name[0])


def _says(index: Index, name: tuple[str, ...]) -> list[str]:
    """The MENTIONED_BY pages whose body says `name` most (an FTS phrase, BM25 order)."""
    phrase = " ".join(re.findall(r"\w+", " ".join(name)))  # word characters only: no FTS syntax
    return [] if not phrase else [i for (i,) in index.db.execute(
        "SELECT id FROM fts WHERE fts MATCH ? ORDER BY rank LIMIT ?",
        (f'body : "{phrase}"', MENTIONED_BY))]


def linked(index: Index, ids: list[str], quiet: dict[str, float] | None = None,
           rank: dict[str, float] | None = None) -> list[str]:
    """R3a: the pages linked with those in `ids` by the most kinds of link (summed over
    `ids`), then by `rank` (higher first: how well each answers the question), then in the
    order of `ids`. The kinds: an edge either way (wiki links, front-matter keys), the page
    saying another's name (`_names`) and another saying the page's name (`_says`). `ids`,
    quiet pages (weight 0), pages the index lacks and the pages Keepfold builds over many
    others (rollups, topic hubs: they link everything) are left out."""
    def words(i: str) -> list[str]:
        return _WORD.findall(index._notes[i]["body"].lower())

    def names(i: str) -> list[tuple[str, ...]]:
        return _names(i, index._notes[i]["title"], aliases(index._notes[i]["fields"]))

    def first(text: str) -> str:
        return (m := _WORD.search(text.lower())) and m[0]

    count: dict[str, list] = {}  # id -> [links, first seed]
    for s, seed in enumerate(ids):
        if seed not in index._notes:
            continue
        w = words(seed)
        have = set(w)  # a name's first word must be here: skips _names for most pages
        got = [{n for n, _ in _neighbours(index, seed)},
               {i for i, n in index._notes.items()
                if (i in have or first(n["title"]) in have
                    or any(first(a) in have for a in aliases(n["fields"])))
                and any(_holds(w, name) for name in names(i))},
               {i for name in names(seed)
                for i in _says(index, name) if i in index._notes and _holds(words(i), name)}]
        for i in set().union(*got):
            if i not in ids and i in index._notes and (quiet or {}).get(i) != 0 \
                    and index._notes[i]["kind"] != "rollup" and not is_hub(index._notes[i]):
                c = count.setdefault(i, [0, s])
                c[0] += sum(i in g for g in got)
    return sorted(count, key=lambda i: (-count[i][0], -(rank or {}).get(i, 0.0), count[i][1], i))


def recall(index: Index, query: str, embed=None, limit: int = 8, hops: int = 1,
           rerank_with=None, fuse: str = "weighted", chats: bool = False,
           quiet: dict[str, float] | None = None, explain: dict | None = None,
           save: bool = True) -> list[dict]:
    """Hybrid hits (0.7 cosine + 0.3 BM25; keyword only when `embed` is None; a cosine under
    MIN_COS counts as 0, and such a page is a hit only through a keyword match of at least
    MIN_KW_COVER of the query's terms), plus graph
    neighbours of the seeds within `hops` (score x0.5 per hop, `via` names the edge). Up to a
    quarter of the `limit` slots go to linked pages. Returns
    [{id, kind, title, summary, score, match: similar|keyword|linked, via?, at?}], best first;
    `at` is the start of the page's best chunk in its filled body (absent for chunk 0).
    Imported chat pages (`is_chat`) score x CHAT_WEIGHT after the floors, unless `chats`;
    `quiet` ({id: weight}, harvest.quiet) overrides that weight, and weight 0 drops the page,
    from the graph neighbours too, even with `chats`. A STALE page or an older version of an
    imported file (`older_versions`) scores x STALE_WEIGHT and a
    recent journal or chat page up to x(1 + RECENT_BOOST), or any recent dated page up to
    x(1 + CURRENT_BOOST) for an `is_current` query (`_factors`).
    With `rerank_with` (a `load_reranker` scorer) the top RERANK_POOL are reordered by it when
    the top 3 are close (`_close`); otherwise they keep the first stage's order.
    `explain` (a dict) is filled with the score parts for `explained`. With `save` False (the
    app's dry run) the vector cache is read, never written (`vectors`)."""
    limit = max(1, min(MAX_LIMIT, int(limit)))
    hops = max(0, min(2, int(hops)))
    if rerank_with is not None:
        pool = _rank(index, query, embed, max(limit, RERANK_POOL), hops, fuse, chats, quiet,
                     explain, save)
        if not _close(pool):  # a clear first stage: skip the ~2 s cross-encoder
            return pool[:limit]
        out = None if explain is None else explain.setdefault("rerank", {})
        return rerank(index, query, pool, rerank_with, chats, quiet, out)[:limit]
    return _rank(index, query, embed, limit, hops, fuse, chats, quiet, explain, save)


def _close(hits: list[dict]) -> bool:
    """Whether the first stage's top 3 (or 2) are within RERANK_MARGIN of the top score (as a
    share of it), the only case where the rerank is worth its cost."""
    if len(hits) < 2:
        return False
    return hits[min(2, len(hits) - 1)]["score"] >= (1 - RERANK_MARGIN) * hits[0]["score"]


def _rank(index: Index, query: str, embed, limit: int, hops: int, fuse: str,
          chats: bool = False, quiet: dict[str, float] | None = None,
          explain: dict | None = None, save: bool = True) -> list[dict]:
    kw = raw_kw = _keyword(index, query)
    cos: dict[str, float] = {}
    raw_cos: dict[str, float] = {}
    cover: dict[str, float] = {}
    at: dict[str, int] = {}  # page -> start of its best chunk, for the rerank
    use_cos = embed is not None and bool((query or "").strip())
    if use_cos:
        q = _unit(np.asarray(embed([QUERY_PREFIX + query])[0], dtype=np.float32))
        ids, page, start, m = vectors(index, embed, len(q), RECALL_BUDGET, heads=HEAD_BUDGET,
                                       save=save)
        best, where = best_chunks(page, start, m @ q, len(ids))
        at = dict(zip(ids, where.tolist(), strict=True))
        # under the floor a page is only a hit through a keyword match of most query terms,
        # so one shared common word ("best") is no hit, while an exact id or title still is
        raw_cos = dict(zip(ids, best.tolist(), strict=True))
        cos = {i: c for i, c in raw_cos.items() if c >= MIN_COS}
        cover = _cover(index, query) if kw else {}
        kw = {i: v for i, v in kw.items() if i in cos or cover.get(i, 0.0) >= MIN_KW_COVER}
    w_cos, w_kw = (W_COS, W_KW) if use_cos else (0.0, 1.0)
    score = {i: w_cos * cos.get(i, 0.0) + w_kw * kw.get(i, 0.0)
             for i in set(cos) | set(kw) if i in index._notes}
    if fuse == "rrf":  # reciprocal rank fusion of the two lists (eval only)
        score = dict.fromkeys(score, 0.0)
        for d in (cos, kw):
            for r, i in enumerate(sorted(d, key=lambda i: (-d[i], i))):
                if i in score:
                    score[i] += 1 / (RRF_K + r + 1)
    # after the floors, so a chat page that is the only match still shows
    now, older = is_current(query), older_versions(index)
    score = {i: sc * w for i, sc in score.items()
             if (w := _weight(index, i, chats, quiet, now, older))}
    if explain is not None:
        explain.update(cos=raw_cos, kw=raw_kw, cover=cover, at=at, score=score, chats=chats,
                       quiet=quiet, current=now, w=(w_cos, w_kw), older=older)

    def match(i):
        return "keyword" if w_kw * kw.get(i, 0.0) > w_cos * cos.get(i, 0.0) else "similar"

    ranked = sorted((i for i in score if score[i] > 0), key=lambda i: (-score[i], i))[:limit]
    n_linked = min(max(1, limit // 4), limit - 1) if hops else 0
    kept, spare = ranked[:limit - n_linked], ranked[limit - n_linked:]
    hits = {i: (score[i], match(i), None) for i in kept}
    linked: dict[str, tuple[float, str]] = {}
    for seed in kept:
        seen, frontier = {seed}, [seed]
        for h in range(1, hops + 1):
            nxt = []
            for f in frontier:
                for n, typ in _neighbours(index, f):
                    if n in seen or n not in index._notes or (quiet or {}).get(n) == 0:
                        continue
                    seen.add(n)
                    nxt.append(n)
                    sc = score[seed] * DECAY ** h
                    if n not in hits and sc > linked.get(n, (0.0, ""))[0]:
                        linked[n] = (sc, f"{f} ({typ})")
            frontier = nxt
    for i, (sc, via) in sorted(linked.items(), key=lambda kv: (-kv[1][0], kv[0]))[:n_linked]:
        hits[i] = (score[i], match(i), None) if score.get(i, 0.0) > sc else (sc, "linked", via)
    for i in spare:
        if len(hits) >= limit:
            break
        hits.setdefault(i, (score[i], match(i), None))
    out = []
    for i, (sc, how, via) in sorted(hits.items(), key=lambda kv: (-kv[1][0], kv[0])):
        n = index._notes[i]
        summary = n["fields"].get("summary")
        out.append({"id": i, "kind": n["kind"], "title": n["title"],
                    "summary": _one_line(str(summary)) if summary else "",
                    "score": round(sc, 3), "match": how, **({"via": via} if via else {}),
                    **({"at": at[i]} if at.get(i) else {})})
    return out


def search(index: Index, query: str, limit: int = 8, hops: int = 1,
           rerank: bool = True, chats: bool = False,
           quiet: dict[str, float] | None = None,
           explain: dict | None = None, save: bool = True) -> tuple[list[dict], str]:
    """`recall` with the downloaded model (and the reranker when present, on and `rerank`);
    without the embedder, keyword + graph and the NOT_SET_UP notice (never a download)."""
    embed = load_embedder()
    ce = load_reranker() if rerank else None
    return recall(index, query, embed, limit, hops, ce, chats=chats, quiet=quiet,
                  explain=explain, save=save), \
        "" if embed else NOT_SET_UP


def explained(index: Index, explain: dict, hits: list[dict],
              misses: int = 0) -> tuple[list[dict], list[dict]]:
    """The score parts of each hit, and of the `misses` best pages that missed the cut, from a
    `recall(explain=...)` dict: {id, cos (raw best-chunk cosine, None without the model), kw
    (BM25 scaled to the best hit), cover (share of query terms), weight, factors (`_factors`,
    whose product is the weight), score, rerank?, at,
    chunk (its text without the title line), via?, reason (misses only)}. Read-only."""
    raw_cos, kw, cover = explain["cos"], explain["kw"], explain["cover"]
    score, (w_cos, w_kw), rr = explain["score"], explain["w"], explain.get("rerank", {})

    def weight(i: str) -> float:
        return _weight(index, i, explain["chats"], explain["quiet"],
                       explain.get("current", False), explain.get("older", frozenset())) or 0.0

    def parts(i: str, h: dict | None = None) -> dict:
        at = explain["at"].get(i, 0)
        chunk = _chunk_text(index._notes[i], at).split("\n", 1)[-1] if raw_cos else ""
        f = _factors(index, i, explain["chats"], explain["quiet"], explain.get("current", False),
                     explain.get("older", frozenset()))
        return {"id": i, "cos": raw_cos.get(i), "kw": kw.get(i, 0.0), "cover": cover.get(i),
                "weight": weight(i), "factors": {k: round(v, 3) for k, v in f.items()},
                "score": h["score"] if h else round(score.get(i, 0.0), 3),
                **({"rerank": round(rr[i], 3)} if i in rr else {}), "at": at,
                "chunk": _one_line(chunk)[:80], **({"via": h["via"]} if h and "via" in h else {})}

    def reason(p: dict) -> str:
        if not p["weight"]:
            return "hidden (harvest quiet weight 0)"
        if p["id"] in score:
            return "below limit" + (" after rerank" if rr else "")
        why = [] if p["cos"] is None else [f"cosine {p['cos']:.3f} < MIN_COS {MIN_COS}"]
        if p["kw"]:
            why.append(f"keyword cover {p['cover'] or 0.0:.2f} < MIN_KW_COVER {MIN_KW_COVER}")
        return ", ".join(why) or "no signal"

    def would(i: str) -> float:  # the fused score without the floors: the order of the misses
        return (w_cos * raw_cos.get(i, 0.0) + w_kw * kw.get(i, 0.0)) * weight(i)

    shown = {h["id"] for h in hits}
    cand = sorted((i for i in set(raw_cos) | set(kw) if i in index._notes and i not in shown),
                  key=lambda i: (-would(i), i))[:misses]
    return ([parts(h["id"], h) for h in hits],
            [{**(p := parts(i)), "reason": reason(p)} for i in cand])
