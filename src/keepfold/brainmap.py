"""Brain v3, the neural network look: geometry and page layout.

Python owns the geometry and the layout; `assets/brain3d.js` only draws them. One implicit
brain volume (`inside`): a smooth union of two overlapping hemispheres, a temporal lobe per
side, a tucked cerebellum and a brainstem stub, with a shallow groove on top and gentle folds,
meshed once. Lobes (the dashboard clusters) get 3-D sites by classical MDS of 1 - affinity and
round regions by a centroidal capacity-constrained power diagram over seeded samples of the
volume (volume ~ count^0.65), with an empty gap at region borders. Pages sit in their region
by a PCA + kNN relaxation of their vectors in 3-D, partly transported to fill it, the most
linked pages pulled into a nucleus at its centre; unsorted chats form a thick band under the
brain. Each lobe's hue differs by HUE_GAP from every region it touches.
Fibres are the wikilinks plus each page's 3 nearest by meaning, as quadratic Béziers bundled
toward the regions' centres. The layout persists in `.keepfold/brain-layout.json` and grows
incrementally. It only reads the vector cache: it never embeds and never downloads a model.

Builder choices (measured on the real vault, see the report of 17 Nov 2026):
- the mesh is surface nets (dual marching cubes, no case table): one vertex per crossed cell,
  projected onto the surface by two Newton steps, lit by the field's gradient;
- the affinity's shared-parent term is "same lobe kind";
- MDS axes go to front-back, left-right, up-down; the coordinates are whitened and each lobe
  set at its radius rank before the push apart (plain MDS crowded most lobes into the centre
  and made slab-shaped regions); then damped Lloyd moves round the regions while each lobe's
  two most affine lobes are pulled back toward touching it (fixed sites left 14 of 40 real
  regions as slabs; plain Lloyd lost the fixture's families); the cerebellum is its own pool
  when a routine lobe exists;
- the in-lobe relaxation repels by negative sampling (UMAP-style), is fitted to its region by
  mean and covariance, then moved 40 % of the way to its sliced optimal transport onto the
  region (closes the gaps between clumps; 40 % keeps in-lobe preservation near 0.7);
- hubs are a lobe's top 5 % or the vault's top 1 % by degree, pulled toward the centre by
  up to 70 % (log degree), so a journal lobe full of daily hubs gets a nucleus too;
- a lobe's centre is the mean of its region's interior samples, its anchor the interior
  sample nearest that centre, its normal the anchor's direction from the brain's centre;
- a page new since the last layout goes to the similarity-weighted mean of its 3 nearest
  placed pages (one block matmul for all of them), so a near-duplicate lands by its twin;
  then new pages are pushed ROOM x their region's spacing off every other page (a grid hash),
  so points never stack; a stored layout without the "room" mark has its pages closer than
  REPAIR x spacing moved the same way, once;
- the layout's `added` holds when each page first entered it (`_now`), kept through rewrites
  and relayouts; a first layout and an old layout's pages have none, so an upgrade is never a
  burst of new pages; the blob's `nodes.added` carries the last ADDED_DAYS days of them for
  the app's growth moment;
- touching regions are lobes whose samples share a voxel or two face-adjacent ones of a
  32-cube grid; each lobe wants the hue of its site's circular order around the vertical axis
  spread evenly over the wheel; touching lobes get hues 60 degrees apart by colouring the
  touching graph with six hues (DSatur, backtracking), then each moves toward its wanted hue
  as far as its gaps allow; lobes that share a hex colour step 0.03 darker;
- `nodes.weight` (0-1, for the renderer's node size) is 0.6 log link degree + 0.25 log
  similar-fibre degree (each scaled by the vault's maximum) + 0.15 recency of `updated`
  (e-folding RECENT days before the newest page);
- a lobe that appears between relayouts is a guest in its most affine lobe's region; a
  region's spacing is its volume over every page in it, guests' pages included;
- a stored page also records whether it had a vector: pages new, moved or embedded since
  the last layout count toward the 20 % relayout threshold;
- the ring takes no tracts; `tracts` stay for the lobe inspector's "Strongest connections".
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache

import numpy as np

from keepfold import semantic
from keepfold.browse import lists
from keepfold.vault import VaultError

log = logging.getLogger(__name__)

VERSION = 3
REVISION = 2  # of the layout code: a bump relays every vault out once
LAYOUT = ".keepfold/brain-layout.json"
LEGEND = ("lobe = a topic from your topic tree, coloured unlike every topic it touches; "
          "position = similar meaning (nearby points are related, distances "
          "are approximate); volume grows slower than the page count; counts are exact in "
          "text; bright fibres = your links; faint fibres = similar meaning.")

# the volume: ellipsoids as (centre, radii x, y above the centre, y below it, z)
HEMI = ((0.24, 0.0, 0.0), (0.44, 0.55, 0.40, 0.86))  # the right one; the left is its mirror
TEMPORAL = ((0.40, -0.25, 0.12), (0.22, 0.18, 0.18, 0.42))  # likewise
CEREBELLUM = ((0.0, -0.38, -0.55), (0.40, 0.17, 0.17, 0.26))
STEM = ((0.0, -0.30, -0.25), (0.0, -0.74, -0.36), 0.085)  # a capsule: two ends, a radius
SMOOTH = 0.08  # polynomial smooth-min width
GROOVE = (0.04, 0.06)  # depth and half-width of the longitudinal groove on top
FOLD = (0.015, 12.0)  # fold amplitude and wave number
BOX = ((-0.78, -0.88, -0.98), (0.78, 0.68, 0.98))  # holds the volume with a margin
GRID = 64  # mesh cells along the longest side of BOX
SAMPLES, MARGIN = 60000, 0.02  # volume samples, each at least MARGIN inside the surface
GAP = 0.02  # pages and region samples keep this far from a region border
AREA_EXP = 0.65
SPREAD = 0.3  # minimum site spacing, a share of the sum of two region radii
LLOYD, LLOYD_STEP = 10, 0.5  # site moves toward their cell's centroid, and how far each goes
TIES, TIE_PULL, TOUCH = 2, 1.0, 0.5  # each lobe's most affine lobes, pulled back toward
# TOUCH x the sum of the two regions' radii
FILL = 0.8  # sites stay inside this share of their pool's extent
FIT = 0.9  # a lobe's layout fills this share of its region's spread
FILL_OT = 0.4  # then goes this share of the way to filling its region evenly
HUBS, VAULT_HUBS = 0.05, 0.01  # hubs: a lobe's top share of pages by degree, or the vault's
HUB_PULL = 0.7  # how far the most linked hub of a lobe is pulled toward its centre
RING_Y, RING_R, RING_W = -0.62, 0.75, 0.07  # the unsorted chats' band: height, radius, thickness
KNN, NEG, EPOCHS = 6, 5, 100  # in-lobe relaxation: attracting neighbours, repelling samples
N_NEAR, N_TRACTS, N_BEACONS, N_SIMILAR = 8, 150, 3, 3
RECENT = 30  # days: a node's weight for recency falls by e for each RECENT days older
PULL, JITTER = 0.35, 0.02  # fibre control points: toward the regions' centres, seeded jitter
HUE_GAP, HUE_MIN = 60, 3  # degrees between touching regions' hues, and (where it fits) any two
RELAYOUT_SHARE = 0.2
ROOM, ROOM_ROUNDS = 0.35, 10  # new points keep ROOM x their region's spacing apart; push rounds
REPAIR = 0.15  # once per stored layout (no "room" mark): its points this close to another move
ADDED_DAYS = 30  # the blob carries the `added` stamps of this many days (the app's growth moment)
ROUTINES = {"journal", "you", "sessions", "placements", "browsing"}  # group lobes in the cerebellum
FRONTAL = {"projects", "people"}  # group lobes pinned to the frontal pole
ARCHIVE = {"chats"}  # group lobes on the basal ring (plus any lobe of kind "archive")
PARTS = ("cerebrum", "cerebellum")
PARAMS_HASH = hashlib.sha256(json.dumps(
    [VERSION, REVISION, HEMI, TEMPORAL, CEREBELLUM, STEM, SMOOTH, GROOVE, FOLD, BOX, SAMPLES,
     MARGIN, GAP, AREA_EXP, SPREAD, LLOYD, LLOYD_STEP, TIES, TIE_PULL, TOUCH, FILL, FIT,
     FILL_OT, HUBS, VAULT_HUBS, HUB_PULL, RING_Y, RING_R, RING_W, KNN, NEG, EPOCHS]
).encode()).hexdigest()[:16]


@lru_cache(maxsize=1 << 16)  # read-only arrays (frombuffer of bytes), so sharing them is safe
def _u(key: str, k: int = 4) -> np.ndarray:
    """k uniforms in [0, 1) from a stable hash of `key`."""
    d = hashlib.blake2b(key.encode(), digest_size=4 * k).digest()
    return np.frombuffer(d, "<u4") / 2.0 ** 32


def _unit(x: np.ndarray) -> np.ndarray:
    return x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-12)


# ---- the volume ----------------------------------------------------------------------------

def _ellipsoid(p: np.ndarray, c, r) -> np.ndarray:
    """About the signed distance to an ellipsoid whose lower half has its own height."""
    q = p - np.asarray(c)
    rad = np.empty_like(q)
    rad[:, 0], rad[:, 1], rad[:, 2] = r[0], np.where(q[:, 1] >= 0, r[1], r[2]), r[3]
    k0 = np.linalg.norm(q / rad, axis=1)
    k1 = np.linalg.norm(q / rad ** 2, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(k1 > 1e-9, k0 * (k0 - 1) / k1, -min(r))


def _capsule(p: np.ndarray, a, b, r: float) -> np.ndarray:
    a, ba = np.asarray(a), np.subtract(b, a)
    t = np.clip((p - a) @ ba / (ba @ ba), 0, 1)
    return np.linalg.norm(p - a - t[:, None] * ba, axis=1) - r


@lru_cache
def _waves() -> tuple[np.ndarray, np.ndarray]:
    u = np.array([_u(f"fold:{k}", 3) for k in range(6)])
    z, a = 2 * u[:, 0] - 1, 2 * np.pi * u[:, 1]
    r = np.sqrt(1 - z * z)
    return np.stack([r * np.cos(a), r * np.sin(a), z], 1) * FOLD[1], 2 * np.pi * u[:, 2]


def _parts(p: np.ndarray) -> np.ndarray:
    """Distance to each part: hemispheres (left, right), temporal lobes (left, right),
    cerebellum, brainstem."""
    m = np.array([-1.0, 1.0, 1.0])
    (hc, hr), (tc, tr) = HEMI, TEMPORAL
    return np.stack([_ellipsoid(p, np.multiply(hc, m), hr), _ellipsoid(p, hc, hr),
                     _ellipsoid(p, np.multiply(tc, m), tr), _ellipsoid(p, tc, tr),
                     _ellipsoid(p, *CEREBELLUM), _capsule(p, *STEM)], 1)


def _field(p: np.ndarray, d: np.ndarray | None = None) -> np.ndarray:
    """The brain's implicit function: negative inside, about the distance to the surface."""
    d = _parts(p) if d is None else d
    f = d[:, 0]
    for k in range(1, d.shape[1]):  # polynomial smooth min
        h = np.maximum(SMOOTH - np.abs(f - d[:, k]), 0) / SMOOTH
        f = np.minimum(f, d[:, k]) - h * h * SMOOTH / 4
    depth, width = GROOVE
    f = f + depth * np.exp(-(p[:, 0] / width) ** 2) * np.clip(p[:, 1] / 0.25, 0, 1)
    k, ph = _waves()
    return f + FOLD[0] * np.sin(p @ k.T + ph).sum(1) / math.sqrt(3)


def inside(p) -> np.ndarray:
    """Whether each point (N x 3) is inside the brain volume."""
    return _field(np.atleast_2d(np.asarray(p, float))) < 0


def _grad(p: np.ndarray, h: float = 1e-4) -> np.ndarray:
    return np.stack([_field(p + e) - _field(p - e) for e in np.eye(3) * h], 1) / (2 * h)


@lru_cache(maxsize=1)
def _samples() -> tuple[np.ndarray, np.ndarray, float]:
    """SAMPLES seeded points at least MARGIN inside the volume (none in the brainstem), which
    of them are cerebellum, and the volume each stands for."""
    rng = np.random.default_rng(3)
    lo, hi = np.array(BOX[0]), np.array(BOX[1])
    pts, cb, drawn = [], [], 0
    while sum(map(len, pts)) < SAMPLES:
        p = lo + (hi - lo) * rng.random((1 << 16, 3))
        d = _parts(p)
        part = np.argmin(d, 1)
        ok = (_field(p, d) < -MARGIN) & (part != 5)
        pts.append(p[ok])
        cb.append(part[ok] == 4)
        drawn += len(p)
    kept = sum(map(len, pts))
    return (np.concatenate(pts)[:SAMPLES], np.concatenate(cb)[:SAMPLES],
            float(np.prod(hi - lo)) * kept / drawn / SAMPLES)


def _pool(cb: np.ndarray, part: str, split: bool) -> np.ndarray:
    """Which of the points flagged cerebellum-or-not belong to `part`'s partition: the
    cerebellum is its own pool only when a lobe lives there."""
    return cb == (part == "cerebellum") if split else np.full(len(cb), part == "cerebrum")


@lru_cache(maxsize=1)
def _mesh() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Surface nets over a grid of GRID cells along BOX's longest side: one vertex per cell
    the surface crosses (the mean of its edge crossings, then projected onto the surface), one
    quad per crossed grid edge, counter-clockwise seen from outside; gradient normals."""
    lo, hi = np.array(BOX[0]), np.array(BOX[1])
    h = float((hi - lo).max()) / GRID
    n = np.ceil((hi - lo) / h).astype(int) + 1
    g = np.stack(np.meshgrid(*(lo[a] + h * np.arange(n[a]) for a in range(3)), indexing="ij"), -1)
    f = _field(g.reshape(-1, 3)).reshape(*n)
    ins = f < 0
    cs = tuple(int(x) for x in n - 1)
    acc, cnt, quads = np.zeros((*cs, 3)), np.zeros(cs), []
    for a in range(3):
        b, c = (a + 1) % 3, (a + 2) % 3
        s0, s1 = [slice(None)] * 3, [slice(None)] * 3
        s0[a], s1[a] = slice(0, -1), slice(1, None)
        f0, f1 = f[tuple(s0)], f[tuple(s1)]
        x = ins[tuple(s0)] != ins[tuple(s1)]
        t = np.where(x, f0 / np.where(x, f0 - f1, 1), 0)
        pt = (g[tuple(s0)] + (t * h)[..., None] * np.eye(3)[a]) * x[..., None]
        for db in (0, 1):  # an edge along a belongs to the 4 cells around it
            for dc in (0, 1):
                s = [slice(None)] * 3
                s[b], s[c] = slice(db, db + cs[b]), slice(dc, dc + cs[c])
                acc += pt[tuple(s)]
                cnt += x[tuple(s)]
        e = np.argwhere(x)
        e = e[(e[:, b] >= 1) & (e[:, b] < cs[b]) & (e[:, c] >= 1) & (e[:, c] < cs[c])]
        corners = []
        for ob, oc in ((0, 0), (1, 0), (1, 1), (0, 1)):  # counter-clockwise about +a
            k = e.copy()
            k[:, b] += ob - 1
            k[:, c] += oc - 1
            corners.append(np.ravel_multi_index(tuple(k.T), cs))
        q = np.stack(corners, 1)
        quads.append(np.where(ins[tuple(e.T)][:, None], q, q[:, ::-1]))  # inside below: faces +a
    act = cnt.ravel() > 0
    vid = np.cumsum(act) - 1
    v = acc.reshape(-1, 3)[act] / cnt.ravel()[act, None]
    for _ in range(2):  # onto the surface: Newton steps of at most half a cell
        gr = _grad(v)
        step = (_field(v) / np.maximum((gr * gr).sum(1), 1e-9))[:, None] * gr
        v = v - step * np.minimum(1, 0.5 * h / np.maximum(np.linalg.norm(step, axis=1),
                                                          1e-12))[:, None]
    q = vid[np.concatenate(quads)]
    return v, _unit(_grad(v)), np.concatenate([q[:, [0, 1, 2]], q[:, [0, 2, 3]]])


# ---- partition -----------------------------------------------------------------------------

def _assign(p: np.ndarray, s: np.ndarray, w: np.ndarray):
    """Power-diagram cell of each point and its distance to the nearest border (inf with one
    site); cell -1 with no site."""
    n, k = len(p), len(s)
    if k < 2:
        return np.full(n, 0 if k else -1), np.full(n, np.inf)
    cost = ((p[:, None, :] - s[None]) ** 2).sum(2) - w
    r = np.arange(n)
    a = np.argmin(cost, axis=1)
    c1 = cost[r, a]
    cost[r, a] = np.inf
    a2 = np.argmin(cost, axis=1)
    return a, (cost[r, a2] - c1) / (2 * np.linalg.norm(s[a] - s[a2], axis=1) + 1e-9)


def _capacity(p: np.ndarray, s: np.ndarray, target: np.ndarray, r2: np.ndarray,
              wt: np.ndarray | None = None, rounds: int = 60) -> np.ndarray:
    """Power weights until each site's cell holds about its target count of samples."""
    wt, target = np.zeros(len(s)) if wt is None else wt, np.maximum(target, 1)
    d = ((p[:, None, :] - s[None]) ** 2).sum(2)
    for _ in range(rounds):
        got = np.bincount(np.argmin(d - wt, axis=1), minlength=len(s))
        wt = wt + 0.5 * r2 * np.clip((target - got) / target, -1, 1)
    return wt


def _lloyd(q: np.ndarray, x: np.ndarray, share: np.ndarray, r: np.ndarray,
           aff: np.ndarray) -> np.ndarray:
    """A centroidal capacity-constrained power diagram, on every k-th sample: LLOYD rounds of
    capacity weights, then each site LLOYD_STEP of the way to its cell's centroid (round cells
    instead of the slabs that fixed, crowded sites cut), and each lobe and its TIES most affine
    lobes pulled TIE_PULL of the way back to touching (r_i + r_j apart) when the moves part them
    (plain Lloyd spread the fixture's families of related lobes apart)."""
    sub = q[::max(1, len(q) // 12000)]
    k = len(x)
    a = aff - 2 * np.eye(k)
    near = np.argsort(-a, axis=1, kind="stable")[:, :min(TIES, k - 1)]
    i, j = np.repeat(np.arange(k), near.shape[1]), near.ravel()
    wt = None
    for _ in range(LLOYD):
        wt = _capacity(sub, x, share * len(sub), r ** 2, wt, 20)
        cell = np.argmin(((sub[:, None, :] - x[None]) ** 2).sum(2) - wt, axis=1)
        cnt = np.bincount(cell, minlength=k)
        cen = np.stack([np.bincount(cell, sub[:, c], k) for c in range(3)], 1)
        x = np.where(cnt[:, None] > 0, x + LLOYD_STEP * (cen / np.maximum(cnt, 1)[:, None] - x), x)
        d = x[j] - x[i]
        dist = np.linalg.norm(d, axis=1)
        far = np.maximum(dist - TOUCH * (r[i] + r[j]), 0)
        mv = (TIE_PULL / 2 * far / np.maximum(dist, 1e-9))[:, None] * d
        dx = np.zeros_like(x)
        np.add.at(dx, i, mv)
        np.add.at(dx, j, -mv)
        x = x + dx / np.maximum(np.bincount(np.concatenate([i, j]), minlength=k), 1)[:, None]
    return x


def _mds(dist: np.ndarray, dim: int = 3) -> np.ndarray:
    k = len(dist)
    out = np.zeros((k, dim))
    if k < 2:
        return out
    j = np.eye(k) - 1 / k
    val, vec = np.linalg.eigh(-0.5 * j @ dist ** 2 @ j)
    top = np.argsort(val)[::-1][:dim]
    out[:, :len(top)] = vec[:, top] * np.sqrt(np.maximum(val[top], 0))
    return out


def _spread(x: np.ndarray, r: np.ndarray, pinned: list[int], c: np.ndarray,
            ext: np.ndarray) -> np.ndarray:
    """Push sites apart toward SPREAD x the sum of their region radii, inside FILL of the
    pool's extent `ext` around its centre `c`."""
    free = np.ones(len(x), bool)
    free[pinned] = False
    need = SPREAD * (r[:, None] + r[None])
    np.fill_diagonal(need, 0)
    for _ in range(60):
        d = x[:, None] - x[None]
        dist = np.linalg.norm(d, axis=2)
        np.fill_diagonal(dist, 1)
        push = np.clip(need - dist, 0, None) / dist
        x = x + 0.25 * free[:, None] * (push[..., None] * d).sum(1)
        e = (x - c) / ext
        x = c + ext * e * np.minimum(1, FILL / np.maximum(np.linalg.norm(e, axis=1),
                                                         1e-9))[:, None]
    return x


def _sites(clusters: list[dict], base: list, ring: np.ndarray, aff: np.ndarray) -> dict:
    """A full relayout of the lobes: {id: {body, site, w}} ({body: "ring"} on the ring)."""
    ids, size = [c["id"] for c in clusters], [c["count"] for c in clusters]
    out = {ids[k]: {"body": "ring"} for k in range(len(ids)) if ring[k]}
    rest = [k for k in range(len(ids)) if not ring[k]]
    groups = {"cerebrum": [k for k in rest if base[k] not in ROUTINES],
              "cerebellum": [k for k in rest if base[k] in ROUTINES]}
    p, cb, vol = _samples()
    for part, ks in groups.items():
        if not ks:
            continue
        q = p[_pool(cb, part, bool(groups["cerebellum"]))]
        ks = sorted(ks, key=lambda k: (-size[k], ids[k]))
        area = np.array([size[k] for k in ks], float) ** AREA_EXP
        share = area / area.sum()
        dist = 1 - aff[np.ix_(ks, ks)]
        np.fill_diagonal(dist, 0)
        x = _mds(dist)[:, [1, 2, 0]]  # the widest spread front to back, then across, then up
        x = x * np.where(x[0] < 0, -1, 1)  # the biggest lobe toward the front, top and right
        # whitened, then each lobe at its radius rank (uniform in the ball): MDS's few far
        # lobes would otherwise crowd the rest into the centre, and its main axis into slabs
        u = x - x.mean(0)
        u = u / np.maximum(u.std(0), 1e-9)
        rank = np.argsort(np.argsort(np.linalg.norm(u, axis=1), kind="stable"), kind="stable")
        x = _unit(u) * np.cbrt((rank + 0.5) / len(x))[:, None]
        x = x + (np.array([_u(f"site:{ids[k]}", 3) for k in ks]) - 0.5) * 1e-3  # exact ties
        pinned = [j for j, k in enumerate(ks) if base[k] in FRONTAL]
        for t, j in enumerate(pinned):
            x[j] = (0.35 * (t - (len(pinned) - 1) / 2), 0.1, 0.85)
        c, ext = q.mean(0), math.sqrt(5) * q.std(0)  # a uniform ellipsoid's semi-axes
        r = (3 * share * len(q) * vol / (4 * np.pi)) ** (1 / 3)
        x = _lloyd(q, _spread(c + x * ext * FILL, r, pinned, c, ext), share, r,
                   aff[np.ix_(ks, ks)])
        wt = _capacity(q, x, share * len(q), r ** 2)
        for j, k in enumerate(ks):
            out[ids[k]] = {"body": part, "site": np.round(x[j], 5).tolist(),
                           "w": round(float(wt[j]), 6)}
    return out


def _regions(lobes: dict, lid: list[str]):
    """Each pool's (samples, its sited lobe ids, sites, weights, cell of each sample, border
    distance) from the stored sites, and every sample's lobe index (-1 for none)."""
    p, cb, _ = _samples()
    split = any(e["body"] == "cerebellum" for e in lobes.values())
    li = {c: k for k, c in enumerate(lid)}
    geo, lab = {}, np.full(len(p), -1)
    for part in PARTS:
        m = _pool(cb, part, split)
        own = sorted(i for i, e in lobes.items() if e["body"] == part and "site" in e)
        s = np.array([lobes[i]["site"] for i in own], float).reshape(-1, 3)
        wt = np.array([lobes[i]["w"] for i in own], float)
        cell, gap = _assign(p[m], s, wt)
        geo[part] = (p[m], own, s, wt, cell, gap)
        if own:
            lab[m] = np.array([li.get(i, -1) for i in own])[cell]
    return geo, lab


def _adjacent(lab: np.ndarray, nl: int) -> np.ndarray:
    """Lobe pairs whose regions touch: their samples share a voxel, or two face-adjacent
    voxels, of a 32-cube grid over BOX."""
    p = _samples()[0]
    lo, hi = np.array(BOX[0]), np.array(BOX[1])
    g = np.minimum(((p - lo) / (hi - lo) * 32).astype(int), 31)
    ok = lab >= 0
    m = np.zeros((32, 32, 32, nl), np.float32)
    m[g[ok, 0], g[ok, 1], g[ok, 2], lab[ok]] = 1
    a = m.reshape(-1, nl).T @ m.reshape(-1, nl)
    for ax in range(3):
        x = np.moveaxis(m, ax, 0)
        t = x[:-1].reshape(-1, nl).T @ x[1:].reshape(-1, nl)
        a += t + t.T
    np.fill_diagonal(a, 0)
    return a >= 2


# ---- colour --------------------------------------------------------------------------------

@lru_cache(maxsize=1 << 12)  # a recolour after a lobe reorder asks again for the same shades
def _hex(light: float, chroma: float, hue: float) -> str:
    """OKLCH to an sRGB hex colour; chroma is reduced until it fits the gamut."""
    a, b = chroma * math.cos(math.radians(hue)), chroma * math.sin(math.radians(hue))
    for _ in range(60):
        lms = (np.array([[1, 0.3963377774, 0.2158037573], [1, -0.1055613458, -0.0638541728],
                         [1, -0.0894841775, -1.2914855480]]) @ (light, a, b)) ** 3
        rgb = np.array([[4.0767416621, -3.3077115913, 0.2309699292],
                        [-1.2684380046, 2.6097574011, -0.3413193965],
                        [-0.0041960863, -0.7034186147, 1.7076147010]]) @ lms
        if rgb.min() >= 0 and rgb.max() <= 1:
            break
        a, b = 0.95 * a, 0.95 * b
    rgb = np.clip(rgb, 0, 1)
    s = np.where(rgb <= 0.0031308, 12.92 * rgb, 1.055 * rgb ** (1 / 2.4) - 0.055)
    return "#" + "".join(f"{round(float(x) * 255):02x}" for x in s)


def _hd(a, b) -> np.ndarray:
    d = np.abs(np.asarray(a, float) - b) % 360
    return np.minimum(d, 360 - d)


_HD = _hd(np.arange(360)[:, None], np.arange(360))  # whole-degree hue distances, row by hue


def _hues(want: np.ndarray, adj: np.ndarray, order: list[int]) -> dict[int, int]:
    """Whole-degree hues, HUE_GAP apart where lobes touch. First a colouring of the touching
    graph with 360 / HUE_GAP evenly spaced hues (the first lobe of `order` at its `want`): DSatur
    with backtracking (most distinct hues among touching lobes first, then most touching
    lobes, then `order`; the hues nearest a lobe's `want` tried first). Should that fail within
    its budget, a greedy pass in the same order takes the hue that falls least short, and
    lobes still short pick again. Then every lobe moves toward `want` as far as its gaps
    allow, HUE_MIN from all other lobes where it can. (Greedy in size order, as v3 did, left
    35 touching pairs under 60 degrees on the real vault; this leaves none.)"""
    cand, m = np.arange(360), 360 // HUE_GAP
    slot = (round(want[order[0]]) + HUE_GAP * np.arange(m)) % 360 if order else cand
    rank = {k: t for t, k in enumerate(order)}
    deg = {k: int(adj[k, order].sum()) for k in order}
    hue: dict[int, int] = {}
    steps = [0]

    def nxt(left):
        return max(left, key=lambda k: (len({hue[j] for j in hue if adj[k, j]}), deg[k],
                                        -rank[k]))

    def search(left):  # shortcut: recursion depth = lobe count, fine below ~900 lobes
        if not left:
            return True
        k = nxt(left)
        used = {hue[j] for j in hue if adj[k, j]}
        for h in sorted((int(h) for h in slot if h not in used),
                        key=lambda h: (_hd(h, want[k]), h)):
            steps[0] += 1
            if steps[0] > 20000:
                return False
            hue[k] = h
            if search([j for j in left if j != k]):
                return True
            del hue[k]
        return False

    def pick(k, apart=False):
        js = [j for j in hue if j != k]
        d = _HD[[hue[j] for j in js]]  # whole degrees, so these sums are exact in any order
        every = d.min(0, initial=np.inf)
        short = (np.maximum(HUE_GAP - d, 0) * adj[k, js][:, None]).sum(0)
        off = _hd(cand, want[k])
        ok = short == 0
        if ok.any():
            if apart and (ok & (every >= HUE_MIN)).any():
                ok &= every >= HUE_MIN
            return int(cand[ok][np.argmin(off[ok])])
        return int(cand[np.lexsort((off, short))[0]])

    if not search(list(order)):
        hue.clear()
        left = list(order)
        while left:
            k = nxt(left)
            left.remove(k)
            hue[k] = pick(k)
        for _ in range(20):
            bad = [k for k in order if any(adj[k, j] and _hd(hue[k], h) < HUE_GAP
                                           for j, h in hue.items())]
            for k in bad:
                hue[k] = pick(k)
    for _ in range(2):
        for k in order:
            hue[k] = pick(k, True)
    return hue


# ---- vectors, affinity, in-lobe layout -----------------------------------------------------

def _stamp(path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _vectors(root, ids: list[str], memo: dict | None = None
             ) -> tuple[np.ndarray, np.ndarray, bool]:
    """Unit page vectors (each page's head chunk) from the semantic cache, a has-vector mask,
    and whether the cache was readable. `memo` keeps the loaded vectors while the store is
    unchanged."""
    n = len(ids)
    try:
        key = semantic.stamp(root)
        hit = memo.get("vectors") if memo is not None else None
        if hit is None or hit[0] != key or key is None:
            vid, unit = semantic.heads(root)  # FileNotFoundError when there is none
            norm = np.linalg.norm(unit, axis=1)
            ok = np.isfinite(norm) & (norm > 0)
            unit[ok] /= norm[ok, None]
            unit[~ok] = 0
            hit = (key, {str(i): k for k, i in enumerate(vid)}, unit, ok)
            if memo is not None:
                memo["vectors"] = hit
        _, row, unit, ok = hit
        g = np.array([row.get(i, -1) for i in ids], dtype=np.int64)
        has = g >= 0
        has[has] = ok[g[has]]
        out = np.zeros((n, unit.shape[1]), np.float32)
        out[has] = unit[g[has]]
    except Exception as e:  # a disposable cache: lay the brain out by links instead
        if not isinstance(e, FileNotFoundError):
            log.warning("brain: vector cache unreadable, laying out by links: %s", e)
        return np.zeros((n, 0), np.float32), np.zeros(n, bool), False
    return out, has, True


def _knn(x: np.ndarray, k: int, block: int = 1024) -> tuple[np.ndarray, np.ndarray]:
    """Each row's k nearest other rows by dot product, best first (ties by index)."""
    n = len(x)
    k = max(0, min(k, n - 1))
    idx, sim = np.zeros((n, k), np.int64), np.zeros((n, k), np.float32)
    if not k:
        return idx, sim
    for s in range(0, n, block):
        m = x[s:s + block] @ x.T
        r = np.arange(len(m))
        m[r, s + r] = -np.inf
        part = np.argpartition(-m, k - 1, axis=1)[:, :k]
        val = np.take_along_axis(m, part, 1)
        o = np.lexsort((part, -val), axis=1)
        idx[s:s + block], sim[s:s + block] = (np.take_along_axis(part, o, 1),
                                              np.take_along_axis(val, o, 1))
    return idx, sim


def _top(a: np.ndarray, b: np.ndarray, k: int, block: int = 1024
         ) -> tuple[np.ndarray, np.ndarray]:
    """Each row of `a`'s k best rows of `b` by dot product, best first (ties by index), and
    those dot products: one block matmul per `block` rows, then k argmax passes (for a small k,
    4 times faster than argpartition, and argmax already breaks ties by index)."""
    k = min(k, len(b))
    idx, sim = np.zeros((len(a), k), np.int64), np.zeros((len(a), k), np.float32)
    for s in range(0, len(a), block):
        m = a[s:s + block] @ b.T
        r = np.arange(len(m))
        for t in range(k):
            j = m.argmax(1)
            idx[s + r, t], sim[s + r, t] = j, m[r, j]
            m[r, j] = -np.inf
    return idx, sim


def _pairs(p: np.ndarray, h: float, q: np.ndarray):
    """Pairs (a, b) of distinct points of `p` closer than `h`, a among the indices `q`, and
    p[a] - p[b]: a grid hash of cells h, each a searched in its 27 cells."""
    # shortcut: memory grows as n^2 for n points in one cell; chunk the queries if
    # thousands ever land on one spot
    c = np.floor(p / h).astype(np.int64)
    c -= c.min(0) - 1
    dim = c.max(0) + 2
    key = (c[:, 0] * dim[1] + c[:, 1]) * dim[2] + c[:, 2]
    o = np.argsort(key, kind="stable")
    sk = key[o]
    off = np.array([(x * dim[1] + y) * dim[2] + z
                    for x in (-1, 0, 1) for y in (-1, 0, 1) for z in (-1, 0, 1)])
    nk = key[q][:, None] + off
    lo = np.searchsorted(sk, nk).ravel()
    cnt = np.searchsorted(sk, nk, "right").ravel() - lo
    a = np.repeat(np.repeat(q, 27), cnt)
    b = o[np.repeat(lo - np.cumsum(cnt) + cnt, cnt) + np.arange(cnt.sum())]
    d = p[a] - p[b]
    m = (a != b) & ((d * d).sum(1) < h * h)
    return a[m], b[m], d[m]


def _room(p: np.ndarray, move, h: float, keys: list[str], keep) -> np.ndarray:
    """`p` (N x 3) with the points `move` pushed until none is within `h` of another: off a
    fixed point by the whole overlap, off another moving point by half (it moves the other
    half); a zero distance takes a direction seeded by both page ids (`keys`). After each round
    `keep(pts, idx)` returns the pushed points put back where they may be; at most
    ROOM_ROUNDS rounds."""
    p = np.array(p, float)
    move = np.asarray(move, np.int64)
    moving = np.zeros(len(p), bool)
    moving[move] = True
    ask = move
    for _ in range(ROOM_ROUNDS):
        a, b, d = _pairs(p, h, ask)  # a new overlap has a point pushed last round in it
        if not len(a):
            break
        asked = np.zeros(len(p), bool)
        asked[ask] = True
        m = moving[b] & ~asked[b]  # the other half, for a moving point not asked this round
        a, b, d = np.concatenate([a, b[m]]), np.concatenate([b, a[m]]), np.concatenate([d, -d[m]])
        dist = np.linalg.norm(d, axis=1)
        u = d / np.maximum(dist, 1e-12)[:, None]
        z = np.flatnonzero(dist < 1e-9)
        if len(z):
            u[z] = _unit(np.array([_u(f"room:{keys[i]}", 3) for i in a[z]])
                         - np.array([_u(f"room:{keys[i]}", 3) for i in b[z]]))
        dp = np.zeros_like(p)
        np.add.at(dp, a, ((h - dist) * np.where(moving[b], 0.5, 1.0))[:, None] * u)
        ask = np.unique(a)
        p[ask] = keep(p[ask] + dp[ask], ask)
    return p


def _near(memo: dict | None, keys: list[str], x: np.ndarray, k: int) -> np.ndarray:
    """_knn's indices for rows named `keys`. With `memo`, when only a few rows came, went or
    changed vector since the last call, only those and the rows that lost a neighbour are
    searched again; every other row merges its old neighbours with the new rows. Ties are by
    index, as in _knn; a near-tie may order differently from a full search (the dot products
    come from a different matrix product, equal only to the last bit or so)."""
    old = memo.get("knn") if memo is not None else None
    n = len(keys)
    k = max(0, min(k, n - 1))
    fresh = gone = None
    if old is not None and old[0] == k and old[1] == keys and np.array_equal(old[2], x):
        return old[3]  # nothing came, went or moved
    if old is not None and old[0] == k and old[2].shape[1] == x.shape[1]:
        okeys, oi = old[1], {key: i for i, key in enumerate(old[1])}
        com = np.array([oi.get(key, -1) for key in keys], np.int64)
        same = com >= 0
        same[same] = (old[2][com[same]] == x[same]).all(1)
        fresh = np.flatnonzero(~same)
        kept = set(com[same].tolist())
        gone = len(okeys) - len(kept)
        if len(fresh) + gone > max(8, n // 50):  # many changes: a full search is as cheap
            fresh = None
    if fresh is None:
        idx, sim = _knn(x, k)
    else:
        idx, sim = np.zeros((n, k), np.int64), np.zeros((n, k), np.float32)
        to_new = np.full(len(old[1]), -1, np.int64)
        to_new[com[same]] = np.flatnonzero(same)
        oidx = to_new[old[3][com[same]]]  # the kept rows' old neighbours, renumbered
        lost = (oidx < 0).any(1)
        rows = np.flatnonzero(same)
        redo = np.concatenate([fresh, rows[lost]]).astype(np.int64)
        merge = rows[~lost]
        if len(merge):
            ci = np.concatenate([oidx[~lost], np.broadcast_to(fresh, (len(merge), len(fresh)))], 1)
            cs = np.concatenate([old[4][com[merge]], x[merge] @ x[fresh].T], 1)
            o = np.lexsort((ci, -cs), axis=1)[:, :k]
            idx[merge], sim[merge] = np.take_along_axis(ci, o, 1), np.take_along_axis(cs, o, 1)
        for r in redo:  # few: one row against all
            s = x @ x[r]
            s[r] = -np.inf
            o = np.lexsort((np.arange(n), -s))[:k]
            idx[r], sim[r] = o, s[o]
    if memo is not None:
        memo["knn"] = (k, list(keys), x.copy(), idx, sim)
    return idx


def _affinity(cent, hasc, links: np.ndarray, pl: np.ndarray, clusters: list[dict]) -> np.ndarray:
    """A = 0.65 cos(centroids) + 0.25 cross-links / max + 0.10 shared parent (same lobe kind)."""
    n = len(clusters)
    cos = cent @ cent.T if cent.shape[1] else np.zeros((n, n))
    known = np.outer(hasc, hasc)
    off = known & ~np.eye(n, dtype=bool)
    cos = np.where(known, cos, cos[off].mean() if off.any() else 0.0)
    x = np.zeros((n, n))
    if len(links):
        la, lb = pl[links[:, 0]], pl[links[:, 1]]
        m = la != lb
        np.add.at(x, (la[m], lb[m]), 1)
        x = x + x.T
    x = x / x.max() if x.max(initial=0) else x
    fam = [{"topic": -1, "domain": -2, "group": -3}.get(c["kind"], k)
           for k, c in enumerate(clusters)]  # unfiled and archive share no parent
    return 0.65 * cos + 0.25 * x + 0.10 * np.equal.outer(fam, fam)


def _embed(v: np.ndarray, key: str) -> np.ndarray:
    """A lobe's pages in 3-D: PCA of their vectors, then EPOCHS rounds of relaxation in which
    each page is pulled toward its KNN nearest by cosine (weighted by it) and pushed from NEG
    seeded random pages (UMAP-style)."""
    n = len(v)
    if n < 4:
        return np.eye(3)[:n]
    x = v - v.mean(0)
    if n < x.shape[1]:  # PCA scores from the smaller Gram matrix: eigh of 384 x 384 is slow
        val, vec = np.linalg.eigh(x @ x.T)
        y = vec[:, -3:] * np.sqrt(np.maximum(val[-3:], 0))
    else:
        y = x @ np.linalg.eigh(x.T @ x)[1][:, -3:]
    y = (y - y.mean(0)) / (y.std() or 1) * 3
    nb, sim = _knn(v, KNN)
    i, j = np.repeat(np.arange(n), nb.shape[1]), nb.ravel()
    w = np.maximum(sim.ravel(), 0)
    w = w / (w.max() or 1)
    ii = np.repeat(np.arange(n), NEG)
    rng = np.random.default_rng(int.from_bytes(hashlib.blake2b(key.encode(), digest_size=8)
                                               .digest(), "little"))
    for e in range(EPOCHS):
        d = y[i] - y[j]
        g = np.clip((-2 * w / (1 + (d * d).sum(1)))[:, None] * d, -4, 4)
        jj = rng.integers(0, n, len(ii))
        dn = y[ii] - y[jj]
        d2 = (dn * dn).sum(1)
        gn = np.clip((2 / ((0.001 + d2) * (1 + d2)))[:, None] * dn, -4, 4) * (ii != jj)[:, None]
        dy = np.stack([np.bincount(i, g[:, c], n) - np.bincount(j, g[:, c], n)
                       + np.bincount(ii, gn[:, c], n) for c in range(3)], 1)
        y = y + (1 - e / EPOCHS) * dy / nb.shape[1]
    return y


def _msqrt(c: np.ndarray, p: float) -> np.ndarray:
    val, vec = np.linalg.eigh(c)
    return vec @ np.diag(np.maximum(val, 1e-12) ** p) @ vec.T


def _fit(y: np.ndarray, q: np.ndarray) -> np.ndarray:
    """Map a 3-D page layout into a region's samples `q` by matching FIT of their spread
    (mean and covariance): affine, so neighbours stay neighbours; settle() snaps the few
    pages left outside. (Filling the region by radius rank was measured on the real vault:
    0.65 of the 5 nearest kept, against 0.68 for this.)"""
    mq = q.mean(0)
    if len(y) < 4 or len(q) < 4:
        return mq + (y - y.mean(0)) * 1e-2
    yc = y - y.mean(0)
    return mq + FIT * yc @ _msqrt(np.cov(yc.T), -0.5) @ _msqrt(np.cov((q - mq).T), 0.5)


def _fill(y: np.ndarray, q: np.ndarray, key: str) -> np.ndarray:
    """Move a fitted layout FILL_OT of the way to its sliced optimal transport onto the
    region's samples `q` (seeded rotations; each axis matched by rank to q's quantiles): the
    gaps between the relaxation's clumps close and the lobe fills its round region, while
    neighbours mostly stay neighbours."""
    n = len(y)
    if n < 4 or len(q) < 4:
        return y
    rng = np.random.default_rng(int.from_bytes(hashlib.blake2b(f"fill:{key}".encode(),
                                                               digest_size=8).digest(), "little"))
    at = ((np.arange(n) + 0.5) / n * len(q)).astype(int)
    z = y.copy()
    for _ in range(40):
        rot = np.linalg.qr(rng.normal(size=(3, 3)))[0]
        pz, pq = z @ rot, q @ rot
        o = np.argsort(pz, axis=0, kind="stable")
        dz = np.empty_like(pz)
        np.put_along_axis(dz, o, np.sort(pq, axis=0)[at] - np.take_along_axis(pz, o, 0), 0)
        z = z + dz @ rot.T
    return y + FILL_OT * (z - y)


# ---- persistence ---------------------------------------------------------------------------

def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _read_layout(root, memo: dict | None = None) -> tuple[str, dict | None]:
    """The stored layout's text, and the layout when it is valid for these parameters.
    `memo` keeps the parsed layout while its file is unchanged; callers get a copy whose
    `pages` and `lobes` they may change."""
    key = _stamp(root / LAYOUT)
    hit = memo.get("layout") if memo is not None else None
    if hit is None or hit[0] != key or key is None:
        hit = (key, *_parse_layout(root))
        if memo is not None:
            memo["layout"] = hit
    text, st = hit[1], hit[2]
    return text, st and {**st, "pages": dict(st["pages"]), "lobes": dict(st["lobes"])}


def _parse_layout(root) -> tuple[str, dict | None]:
    try:
        text = (root / LAYOUT).read_text(encoding="utf-8")
        st = json.loads(text)
    except (OSError, ValueError):
        return "", None
    ok = (isinstance(st, dict) and st.get("version") == VERSION
          and st.get("params_hash") == PARAMS_HASH
          and isinstance(st.get("pages"), dict) and isinstance(st.get("lobes"), dict)
          and all(isinstance(v, list) and len(v) == 5 and all(map(_num, v[:3]))
                  and isinstance(v[3], str) and v[4] in (0, 1) for v in st["pages"].values())
          and all(isinstance(e, dict) for e in st["lobes"].values()))
    sited = ok and {i: e["body"] for i, e in st["lobes"].items() if e.get("body") in PARTS
                    and isinstance(e.get("site"), list) and len(e["site"]) == 3
                    and all(map(_num, e["site"])) and _num(e.get("w"))}
    ok = ok and all(e.get("body") == "ring" or i in sited or (
        isinstance(e.get("host"), str) and sited.get(e["host"]) == e.get("body"))
        for i, e in st["lobes"].items())
    return text, st if ok else None


def _now() -> str:
    """An `added` stamp: UTC to the millisecond, as JavaScript's toISOString writes it."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _recent(added: dict) -> dict:
    """The `added` stamps of the last ADDED_DAYS days, for the blob."""
    cut = (datetime.now(UTC) - timedelta(days=ADDED_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    return {i: v for i, v in added.items() if v >= cut}


def _day(s) -> int:
    """A date string's day number; 0 when it has none."""
    try:
        return date.fromisoformat(str(s)[:10]).toordinal()
    except ValueError:
        return 0


def _b64(a, dtype: str) -> str:
    return base64.b64encode(np.ascontiguousarray(a, dtype=dtype).tobytes()).decode("ascii")


def _r(v, nd=4) -> list[float]:
    return [round(float(x), nd) for x in v]


@lru_cache(maxsize=1)
def _mesh_b64() -> tuple[str, str, str]:
    vp, vn, tri = _mesh()
    return _b64(vp, "<f4"), _b64(vn, "<f4"), _b64(tri, "<u4")


def _sited(lobes: dict, memo: dict | None = None):
    """What the sites alone decide, whatever the lobes' order: `_regions` over every stored
    lobe id (sorted), each pool's cell of every mesh vertex, which lobes touch, and a dict that
    `_geometry` fills with each lobe's own region and centre. `memo` keeps it while the stored
    lobes stay the same (a lobe's page count reorders the lobes)."""
    key = json.dumps(lobes, sort_keys=True)
    hit = memo.get("sited") if memo is not None else None
    if hit is None or hit[0] != key:
        ids = sorted(lobes)
        geo, lab = _regions(lobes, ids)
        vp = _mesh()[0]
        split = any(e["body"] == "cerebellum" for e in lobes.values())
        vcb = np.argmin(_parts(vp), 1) == 4
        cells = {}
        for part in PARTS:
            _, own, s, wt, _, _ = geo[part]
            m = _pool(vcb, part, split)
            if own and m.any():
                cells[part] = (m, _assign(vp[m], s, wt)[0])
        touch = _adjacent(lab, len(ids)) if ids else np.zeros((0, 0), bool)
        hit = (key, {i: k for k, i in enumerate(ids)}, geo, cells, touch, {})
        if memo is not None:
            memo["sited"] = hit
    return hit[1:]


def _geometry(lobes: dict, lid: list[str], kind: list[str], memo: dict | None = None):
    """What the stored lobes alone decide: each sited lobe's region (pool, cell index, the
    pool's sites and weights, interior samples), the lobes' centres, anchors, normals and areas,
    the mesh (each vertex takes the lobe of its region in its pool) and the lobe colours."""
    nl = len(lid)
    li = {c: k for k, c in enumerate(lid)}
    ring = np.array([lobes[i]["body"] == "ring" for i in lid], bool)
    at, geo, cells, touch, own_part = _sited(lobes, memo)

    def placed(i):  # a sited lobe's region, centre, anchor, normal and area, whatever the order
        if i not in own_part:
            e = lobes[i]
            q, own, s, wt, cell, gap = geo[e["body"]]
            j = own.index(e.get("host", i))
            m = cell == j
            inner = m & (gap >= GAP)
            q = q[inner if inner.any() else m]
            q = q if len(q) else s[j:j + 1]
            c = np.asarray(q.mean(0), float)
            a = np.asarray(q[np.argmin(((q - c) ** 2).sum(1))], float)
            own_part[i] = ((e["body"], j, s, wt, q), c, a, _unit(a),
                           0 if "host" in e else (cell == j).sum() / SAMPLES)
        return own_part[i]

    regions = {k: placed(lid[k])[0] for k in range(nl) if not ring[k]}
    centre, anchor, normal, area = (np.zeros((nl, 3)), np.zeros((nl, 3)), np.zeros((nl, 3)),
                                    np.zeros(nl))
    for k in range(nl):
        if ring[k]:
            centre[k], anchor[k], normal[k] = (0, RING_Y, 0), (0, RING_Y, RING_R), (0, 0, 1)
            continue
        _, centre[k], anchor[k], normal[k], area[k] = placed(lid[k])

    vl = np.full(len(_mesh()[0]), 65535)
    for part, (m, cell) in cells.items():
        vl[m] = np.array([li.get(i, 65535) for i in geo[part][1]])[cell]
    pos, nrm, idx = _mesh_b64()
    mesh = [{"name": "brain", "pos": pos, "nrm": nrm, "idx": idx, "lobe": _b64(vl, "<u2")}]

    # colours: hue from the site's direction around the vertical axis, apart where regions touch
    perm = [at[i] for i in lid]  # touching is pairwise: the stored lobes' matrix, in lid order
    adj = touch[np.ix_(perm, perm)] if nl else np.zeros((0, 0), bool)
    for k in range(nl):  # a guest touches its host and the host's neighbours
        h = li.get(lobes[lid[k]].get("host"))
        if h is not None:
            adj[k] |= adj[h]
            adj[:, k] |= adj[:, h]
            adj[k, h] = adj[h, k] = True
            adj[k, k] = False
    tinted = [k for k in range(nl) if not ring[k] and kind[k] in ("topic", "domain", "group")]
    angle, mid = {}, {}
    for k in tinted:
        e = lobes[lid[k]]
        if e["body"] not in mid:
            mid[e["body"]] = geo[e["body"]][0].mean(0)
        site, c = np.array(lobes[e.get("host", lid[k])]["site"]), mid[e["body"]]
        angle[k] = (math.degrees(math.atan2(site[2] - c[2], site[0] - c[0]))
                    + (1e-3 if "host" in e else 0)) % 360
    # spread over the whole wheel in the same circular order, the biggest lobe at its angle
    order = sorted(tinted, key=lambda k: (angle[k], k))
    want = np.zeros(nl)
    for t, k in enumerate(order):
        want[k] = (angle[tinted[0]] + 360 * (t - order.index(tinted[0])) / len(order)) % 360
    hue = _hues(want, adj, tinted)
    color: list[str] = []
    for k in range(nl):
        light, chroma = ((0.74, 0.07 if kind[k] == "group" else 0.19) if k in hue
                         else (0.55 if ring[k] else 0.70, 0))
        c = _hex(light, chroma, hue.get(k, 0))
        while c in color:  # a hue shared by lobes that do not touch: a shade darker
            light -= 0.03
            c = _hex(light, chroma, hue.get(k, 0))
        color.append(c)
    return regions, centre, anchor, normal, area, mesh, color


# ---- the blob ------------------------------------------------------------------------------

def build(vault, pages: list[dict], clusters: list[dict], relayout: bool = False,
          auto_relayout: bool = True, memo: dict | None = None) -> dict:
    """The dashboard's `brain` key for build_data's pages and
    lobes; reads and, when it changed, rewrites `.keepfold/brain-layout.json`.
    `auto_relayout=False` (the app's live home) never lays the brain out again by itself,
    however much changed: placed pages keep their place and new ones join their neighbours;
    `relayout` still does. `memo` is a dict the caller keeps between builds: the same inputs
    then give the same blob object, and the vector cache, the parsed layout, the lobe geometry
    and the nearest-page search are reused while their inputs stay the same."""
    root, n, nl = vault.root, len(pages), len(clusters)
    inputs = None
    if memo is not None and not relayout:
        inputs = (auto_relayout, json.dumps(clusters, sort_keys=True),
                  [(p["id"], p["cluster"], p["updated"], p["links"]) for p in pages],
                  semantic.stamp(root), _stamp(root / LAYOUT))
        if memo.get("inputs") == inputs:
            return memo["out"]
    ids = [p["id"] for p in pages]
    at = {i: k for k, i in enumerate(ids)}
    lid = [c["id"] for c in clusters]
    li = {c: k for k, c in enumerate(lid)}
    pl = np.array([li[p["cluster"]] for p in pages], dtype=np.int64)
    vec, has, vec_ok = _vectors(root, ids, memo)
    ea = np.array([(a, at[x]) for a, p in enumerate(pages) for x in p["links"] if x in at],
                  dtype=np.int64).reshape(-1, 2)
    links = np.unique(np.sort(ea[ea[:, 0] != ea[:, 1]], axis=1), axis=0).reshape(-1, 2)
    deg = np.bincount(links.ravel(), minlength=n)
    nbrs: list[list[int]] = [[] for _ in range(n)]
    for a, b in links.tolist():
        nbrs[a].append(b)
        nbrs[b].append(a)
    base = [c["id"].removesuffix("-group") if c["kind"] == "group" else None for c in clusters]
    ring = np.array([c["kind"] == "archive" or b in ARCHIVE for c, b in zip(clusters, base,
                                                                            strict=True)], bool)
    hr = np.flatnonzero(has)
    hr = hr[np.argsort(pl[hr], kind="stable")]
    # a lobe's sum depends only on its pages' vectors in page order: `memo` keeps it by lobe id
    stamp = vec_ok and memo["vectors"][0] if memo is not None else None
    was, sums = (memo.get("cent", {}) if memo is not None else {}), {}
    cent = np.zeros((nl, vec.shape[1]), np.float32)
    lk, cut = np.unique(pl[hr], return_index=True)
    ends = [*cut.tolist(), len(hr)]
    for j, k in enumerate(lk.tolist()):  # in page order, bit for bit as np.add.at summed
        rows = hr[ends[j]:ends[j + 1]]
        key = (stamp, [ids[r] for r in rows])
        got = was.get(lid[k])
        if got is None or got[0] != key:
            got = (key, np.cumsum(vec[rows], axis=0)[-1])
        sums[lid[k]] = got
        cent[k] = got[1]
    if memo is not None:
        memo["cent"] = sums
    hasc = np.linalg.norm(cent, axis=1) > 0
    cent = _unit(cent) * hasc[:, None]
    aff = _affinity(cent, hasc, links, pl, clusters)

    old_text, st = _read_layout(root, memo)
    # when each page first entered the layout: none for an old layout's pages or a first layout
    had = set(st["pages"]) if st is not None else None
    was_added = st.get("added") if st is not None else None
    if not isinstance(was_added, dict):
        was_added = {}
    was_added = {i: v for i, v in was_added.items() if isinstance(v, str)}
    plid = [lid[k] for k in pl.tolist()]  # each page's lobe id
    hasl = has.tolist()
    changed = n if st is None else sum(
        1 for k, i in enumerate(ids) if (s := st["pages"].get(i)) is None
        or s[3] != plid[k] or (hasl[k] and not s[4]))
    full = relayout or st is None or (auto_relayout and changed > RELAYOUT_SHARE * n)
    if full:
        st = {"version": VERSION, "params_hash": PARAMS_HASH, "pages": {},
              "lobes": _sites(clusters, base, ring, aff)}
    else:
        st["pages"] = {i: s for k, i in enumerate(ids)
                       if (s := st["pages"].get(i)) and s[3] == plid[k]}
    lobes = st["lobes"]
    for k in range(nl):  # a lobe new since the last layout: a guest in its most affine region
        if lid[k] in lobes:
            continue
        owners = [h for h in range(nl) if "site" in lobes.get(lid[h], {})]
        sited = sorted(i for i, e in lobes.items() if "site" in e)
        if ring[k] or not sited:
            lobes[lid[k]] = {"body": "ring"}
        else:
            host = lid[max(owners, key=lambda h: (aff[k, h], -h))] if owners else sited[0]
            lobes[lid[k]] = {"body": lobes[host]["body"], "host": host}
    ring_k = ring  # the lobe's kind; where it sits is the stored body
    ring = np.array([lobes[i]["body"] == "ring" for i in lid], bool)
    kind = [c["kind"] for c in clusters]
    gkey = json.dumps([lobes, lid, kind], sort_keys=True)
    hit = memo.get("geometry") if memo is not None else None
    if hit is None or hit[0] != gkey:
        hit = (gkey, _geometry(lobes, lid, kind, memo))
        if memo is not None:
            memo["geometry"] = hit
    regions, centre, anchor, normal, area, mesh, color = hit[1]
    vol = _samples()[2]

    # place every page that has no stored position
    pos = np.full((n, 3), np.nan)
    if st["pages"]:
        pos[[at[i] for i in st["pages"]]] = [s[:3] for s in st["pages"].values()]
    fresh = np.isnan(pos[:, 0])
    regs: dict = {}  # each region's lobes: a sited lobe and its guests share one
    for k in range(nl):
        if not ring[k]:
            regs.setdefault(regions[k][:2], []).append(k)

    def jitter(rows, tag, size):
        return (np.array([_u(f"{tag}:{ids[r]}", 3) for r in rows]).reshape(-1, 3) - 0.5) * size

    def settle(k, pts, rows):
        """Into lobe k's region: points outside it, near a border or near the surface snap
        to its nearest interior sample."""
        _, j, s, wt, q = regions[k]
        pts = np.array(pts, float)
        cell, gap = _assign(pts, s, wt)
        bad = np.flatnonzero((cell != j) | (gap < GAP / 2) | (_field(pts) > -MARGIN / 2))
        if len(bad):
            near = np.argmin((q * q).sum(1)[None] - 2 * pts[bad] @ q.T, axis=1)
            pts[bad] = q[near] + jitter([rows[i] for i in bad], "snap", 0.6 * GAP)
        pos[rows] = np.round(pts, 5)
        return pos[rows]

    def spacing(k):  # the region's volume over every page in it, a guest's pages included
        ks = regs[regions[k][:2]]
        a = max(area[h] for h in ks) or len(regions[k][4]) / SAMPLES  # a guest of a gone host
        return (max(a * SAMPLES * vol, 1e-6)
                / max(sum(clusters[h]["count"] for h in ks), 1)) ** (1 / 3)

    cand = ([k for k in range(nl) if not ring[k] and hasc[k]
             and clusters[k]["kind"] in ("topic", "domain")]
            or [k for k in range(nl) if not ring[k] and hasc[k]])

    def beacons(r):
        if not has[r] or not cand:
            return []
        sims = cent[cand] @ vec[r]
        return [cand[t] for t in sorted(range(len(cand)), key=lambda t: (-sims[t], t))[:N_BEACONS]]

    big = max(float(np.quantile(deg, 1 - VAULT_HUBS)), 1) if n else 1  # the vault's top hubs
    later = []
    for k in range(nl):
        rows = np.flatnonzero((pl == k) & np.isnan(pos[:, 0])).tolist()
        if not rows:
            continue
        if ring[k]:
            for r in rows:
                u = _u(f"ring:{ids[r]}", 4)
                bl = beacons(r)
                if bl:
                    wgt = np.exp(20 * (cent[bl] @ vec[r] - 1))
                    a = math.atan2(float(wgt @ anchor[bl, 0]), float(wgt @ anchor[bl, 2]))
                else:
                    a = 2 * math.pi * u[3]
                a += (u[0] - 0.5) * 0.3
                rho, phi = RING_W * math.sqrt(u[1]), 2 * math.pi * u[2]  # in the band's section
                rr, y = RING_R + rho * math.cos(phi), RING_Y + rho * math.sin(phi)
                while _field(np.array([[rr * math.sin(a), y, rr * math.cos(a)]]))[0] < MARGIN:
                    y -= MARGIN  # clear of the cerebellum
                pos[r] = np.round((rr * math.sin(a), y, rr * math.cos(a)), 5)
            continue
        emb = [r for r in rows if has[r]]
        later += [r for r in rows if not has[r]]
        own_placed = ((pl == k) & has & ~np.isnan(pos[:, 0])).any()
        if emb and "host" not in lobes[lid[k]] and not own_placed:
            q = regions[k][4]  # a fresh region: the lobe's map, filling it
            y = _fill(_fit(_embed(vec[emb], lid[k]), q), q, lid[k])
            rank = sorted(range(len(emb)), key=lambda t: (-deg[emb[t]], ids[emb[t]]))
            hubs = [t for i, t in enumerate(rank) if deg[emb[t]] > 0
                    and (i < math.ceil(HUBS * len(emb)) or deg[emb[t]] >= big)]
            if hubs:  # the nucleus: the more links, the nearer the centre
                d = np.log1p(deg[[emb[t] for t in hubs]])
                y[hubs] += (HUB_PULL * d / d.max())[:, None] * (centre[k] - y[hubs])
            settle(k, y, emb)
            continue
        if not emb:
            continue
        host = li.get(lobes[lid[k]].get("host"), k)
        # next to its 3 nearest placed neighbours by vector, inside its lobe (its host's at first)
        pool = np.flatnonzero((pl == k) & has & ~np.isnan(pos[:, 0]))
        if not len(pool):
            pool = np.flatnonzero((pl == host) & has & ~np.isnan(pos[:, 0]))
        if not len(pool):
            later += emb
            continue
        best, sims = _top(vec[emb], vec[pool], 3)
        wgt = np.exp(50 * (sims - sims[:, :1]))  # the closest in meaning weighs most
        pt = (wgt[..., None] * pos[pool[best]]).sum(1) / wgt.sum(1, keepdims=True)
        settle(k, pt + jitter(emb, "new", 0.3 * spacing(k)), emb)
    for r in sorted(later, key=lambda r: (-deg[r], ids[r])):  # by links, else a seeded spot
        k = pl[r]
        placed = [x for x in nbrs[r] if not np.isnan(pos[x, 0])]
        if placed:
            pt = pos[placed].mean(0) + jitter([r], "link", 0.5 * spacing(k))[0]
        else:
            q = regions[k][4]
            pt = q[int(_u(f"seed:{ids[r]}", 1)[0] * len(q))] + jitter([r], "seed", 0.5 * GAP)[0]
        settle(k, pt[None], [r])

    # room: new pages keep ROOM x spacing apart; once per stored layout, its stacked pages move
    repair, moved = st.get("room") != 1, set()
    for ks in regs.values():
        rr = np.flatnonzero(np.isin(pl, ks))
        if not len(rr):
            continue
        s, mv = spacing(ks[0]), fresh[rr].copy()
        if repair and (~mv).any():  # stored pages too close to another stored page
            o = np.flatnonzero(~mv)
            mv[o[_pairs(pos[rr[o]], REPAIR * s, np.arange(len(o)))[0]]] = True
            moved.update(rr[mv & ~fresh[rr]].tolist())
        if mv.any():
            _room(pos[rr], np.flatnonzero(mv), ROOM * s, [ids[r] for r in rr],
                  lambda pts, i, k=ks[0], rr=rr: settle(k, pts, rr[i]))
    st["room"] = 1

    for k, i in enumerate(ids):
        s = st["pages"].get(i)
        if s is None or k in moved:
            st["pages"][i] = [*(float(x) for x in pos[k]), lid[pl[k]],
                              int(has[k]) if s is None else s[4]]
    now = _now() if had is not None and any(i not in had for i in ids) else None
    st["added"] = {i: v for i in ids if (v := was_added.get(i) or (
        now if had is not None and i not in had else None))}
    text = json.dumps(st, sort_keys=True, separators=(",", ":"))
    if text != old_text:
        try:
            vault.write_text(root / LAYOUT, text)
            if memo is not None:  # what was just written is what the next build reads
                memo["layout"] = (_stamp(root / LAYOUT), text, st)
        except (VaultError, OSError) as e:  # a view cache: the next build lays it out again
            log.warning("brain layout not saved: %s", e)

    # depth inside the surface as placed; 0 on the ring
    dep = np.zeros(n)
    keepfold = ~ring[pl] if n else np.zeros(0, bool)
    if keepfold.any():
        dep[keepfold] = np.maximum(-_field(pos[keepfold]), 0)

    wide = n >= 65535
    none = 0xFFFFFFFF if wide else 65535
    near = np.full((n, N_NEAR), none, np.int64)
    emb = np.flatnonzero(has)
    nb = _near(memo, [ids[r] for r in emb], vec[emb], N_NEAR)
    near[emb, :nb.shape[1]] = emb[nb]

    # fibres: every link, then each embedded page's nearest by meaning that is not a link
    sb = near[emb, :N_SIMILAR]
    sa, sb = np.repeat(emb, sb.shape[1]), sb.ravel()
    sa, sb = sa[sb != none], sb[sb != none]
    pair = np.minimum(sa, sb) * max(n, 1) + np.maximum(sa, sb)
    pair[np.isin(pair, links[:, 0] * max(n, 1) + links[:, 1])] = -1
    first = np.sort(np.unique(pair, return_index=True)[1])  # a pair's first sighting only
    first = first[pair[first] >= 0]
    sa, sb = sa[first], sb[first]
    fa = np.concatenate([links[:, 0], sa]).astype(np.int64)
    fb = np.concatenate([links[:, 1], sb]).astype(np.int64)
    home = np.where(ring[pl][:, None], pos, centre[pl]) if n else pos  # ring ends: no pull
    mid = (pos[fa] + pos[fb]) / 2
    seed = np.array([_u(f"fibre:{i}", 6) for i in ids]).reshape(-1, 6)  # a pair's: both ends'
    ctrl = (mid + PULL * ((home[fa] + home[fb]) / 2 - mid)
            + ((seed[fa, :3] + seed[fb, 3:]) % 1 - 0.5) * 2 * JITTER)
    fibres = {"a": _b64(fa, "<u4"), "b": _b64(fb, "<u4"), "c": _b64(ctrl, "<f4"),
              "k": _b64([0] * len(links) + [1] * len(sa), "u1")}

    # node weight for the renderer's size: links, similar-meaning fibres, recency of `updated`
    sdeg = np.bincount(np.concatenate([sa, sb]).astype(np.int64), minlength=n)
    day = np.array([_day(p.get("updated")) for p in pages], float)
    recent = np.exp((day - day.max()) / RECENT) if n else day
    weight = (0.6 * np.log1p(deg) / max(math.log1p(deg.max(initial=0)), 1e-9)
              + 0.25 * np.log1p(sdeg) / max(math.log1p(sdeg.max(initial=0)), 1e-9)
              + 0.15 * recent)

    cross: dict = {}
    for la, lb in np.sort(pl[links], axis=1).tolist():
        if la != lb and not ring[la] and not ring[lb]:
            cross[la, lb] = cross.get((la, lb), 0) + 1
    tracts = []
    for (a, b), cnt in sorted(cross.items(), key=lambda t: (-t[1], t[0]))[:N_TRACTS]:
        if cnt < 2:
            break
        p1, p2 = (anchor[a] + (anchor[b] - anchor[a]) * t for t in (1 / 3, 2 / 3))
        tracts.append({"a": a, "b": b, "n": cnt, "p": [
            _r(anchor[a]), _r(0.55 * p1), _r(0.55 * p2), _r(anchor[b])]})  # 45 % toward the centre

    out = {
        "version": VERSION,
        "legend": LEGEND,
        "nodes": {"ids": ids, "xyz": _b64(pos, "<f4"), "lobe": _b64(pl, "<u2"),
                  "depth": _b64(dep, "<f4"), "embedded": _b64(has, "u1"),
                  "weight": _b64(np.clip(weight, 0, 1), "<f4"),
                  "added": _recent(st["added"])},
        "lobes": [{"id": c["id"], "title": c["title"], "kind": c["kind"], "count": c["count"],
                   "anchor": _r(anchor[k]), "normal": _r(normal[k]), "centre": _r(centre[k]),
                   "color": color[k], "archive": bool(ring_k[k]),
                   "area": round(float(area[k]), 4)} for k, c in enumerate(clusters)],
        "mesh": mesh,
        "fibres": fibres,
        "tracts": tracts,
        "near": {"ids": _b64(near, "<u4" if wide else "<u2")},
        "beacons": {str(r): bl for r in np.flatnonzero(ring[pl]).tolist() if (bl := beacons(r))},
        "stats": {"embedded": int(has.sum()), "unembedded": int(n - has.sum()),
                  "relayout": bool(full), "vectors": vec_ok},
    }
    if inputs is not None:  # the layout as it is now on disk: this build may have written it
        memo["inputs"], memo["out"] = (*inputs[:4], _stamp(root / LAYOUT)), out
    return out


# ---- the brain stem: the owner's websites (the app's live home only) ----------------------

STEM_COL, STEM_R = ((0.0, -0.92, -0.36), (0.0, -1.66, -0.36)), 0.05  # the sites' column below
# the shell (top: first seen, bottom: newest) and their farthest distance from its axis
STEM_TUBE = ((0.0, -0.70, -0.36), (0.0, -1.74, -0.36), 0.085, 0.06)  # the faint tube drawn
# round them: from inside the stem capsule down past the column's end; top and bottom radii
STEM_LINKS, HUB_SITES = 3, 10  # fibres per site at most; sites tied to one topic hub page at most
_CATS = (*lists.SITE_CATEGORIES, *lists.CATEGORIES)


@lru_cache(maxsize=64)
def site_color(category: str | None) -> str:
    """A site category's colour: a golden-angle hue by its place in the category lists, so
    neighbouring categories differ; "other" and unknown ones are grey."""
    if category not in _CATS or category == "other":
        return _hex(0.70, 0, 0)
    return _hex(0.74, 0.15, (_CATS.index(category) * 137.508 + 20) % 360)


def stem_order(rows: list[dict], placed: dict) -> None:
    """Give each site of `rows` ({site, first}) missing from `placed` its share (0-1) of the
    stem's axis, ordered by first seen: with nothing placed, its rank among `rows`; after
    that, interpolated between the placed sites by its first visit, so nothing placed moves
    (a site newer than all sits at the newest end). `placed`: {site: (first, share)}."""
    new = [r for r in rows if r["site"] not in placed]
    if not new:
        return
    if not placed:
        order = sorted(new, key=lambda r: (r["first"] or 0, r["site"]))
        placed.update({r["site"]: (r["first"] or 0, (k + 0.5) / len(order))
                       for k, r in enumerate(order)})
        return
    have = sorted(placed.values())
    xs, ts = np.array([f for f, _ in have], float), np.array([t for _, t in have])
    for r in new:
        placed[r["site"]] = (r["first"] or 0, float(np.interp(r["first"] or 0, xs, ts)))


def stem_xyz(sites: list[str], share: list[float]) -> np.ndarray:
    """Each site's point in the stem's column (STEM_COL) below the shell: down the axis by
    its share, round it at a seeded angle and radius (even over the cross-section)."""
    a, b = np.array(STEM_COL[0]), np.array(STEM_COL[1])
    ax = _unit(b - a)
    e1 = _unit(np.cross(ax, (1.0, 0.0, 0.0)))
    e2 = np.cross(ax, e1)
    u = np.array([_u(f"stem:{s}", 3) for s in sites]).reshape(-1, 3)
    s = np.clip(np.asarray(share, float) + (u[:, 2] - 0.5) * 0.01, 0, 1)
    axis = a + s[:, None] * (b - a)
    ang, rad = 2 * np.pi * u[:, 1], STEM_R * np.sqrt(u[:, 0])
    return axis + rad[:, None] * (np.cos(ang)[:, None] * e1 + np.sin(ang)[:, None] * e2)


@lru_cache(maxsize=1)
def stem_tube() -> dict:
    """The mesh body (`build`'s mesh format, named "stem", no lobe) of the tube round the
    stem's column: a tapered cylinder (STEM_TUBE) with a rounded bottom and an open top
    inside the stem capsule."""
    a, b, r0, r1 = np.array(STEM_TUBE[0]), np.array(STEM_TUBE[1]), *STEM_TUBE[2:]
    around, length = 24, float(np.linalg.norm(b - a))
    prof = [(t * length, r0 + (r1 - r0) * t) for t in np.linspace(0, 1, 14)]  # (depth, radius)
    prof += [(length + r1 * math.sin(q), r1 * math.cos(q)) for q in np.linspace(0.3, 1.35, 5)]
    ax = _unit(b - a)
    e1 = _unit(np.cross(ax, (1.0, 0.0, 0.0)))
    e2 = np.cross(ax, e1)
    ang = 2 * np.pi * np.arange(around) / around
    rim = np.cos(ang)[:, None] * e1 + np.sin(ang)[:, None] * e2  # unit, round the axis
    pos, nrm = [], []
    for k, (d, r) in enumerate(prof):
        d0, ra = prof[max(k - 1, 0)]
        d1, rb = prof[min(k + 1, len(prof) - 1)]
        tn = np.array([d1 - d0, ra - rb])  # the profile's outward normal: (out, along)
        tn /= np.linalg.norm(tn)
        pos.append(a + ax * d + rim * r)
        nrm.append(rim * tn[0] + ax * tn[1])
    pos.append((a + ax * (length + r1))[None])
    nrm.append(ax[None])
    idx = []
    for k in range(len(prof) - 1):
        for j in range(around):
            p, q = k * around + j, k * around + (j + 1) % around
            idx += [p, q + around, q, p, p + around, q + around]
    pole, base = len(prof) * around, (len(prof) - 1) * around
    for j in range(around):
        idx += [base + j, pole, base + (j + 1) % around]
    vp = np.concatenate(pos)
    return {"name": "stem", "pos": _b64(vp, "<f4"), "nrm": _b64(np.concatenate(nrm), "<f4"),
            "idx": _b64(np.array(idx), "<u4"), "lobe": _b64(np.full(len(vp), 65535), "<u2")}


def _stem_links(sites: list[dict], hub_at: dict[str, int], first: int) -> list[tuple[int, int]]:
    """The stem's fibres as node pairs (sites are `first` + their index): first each site up
    to its biggest topic hub's page (a hub takes its HUB_SITES most visited sites), then
    between similar sites: the related ones the store keeps, then the neighbours in
    first-seen order that share its biggest hub, then its category. Most visited sites
    choose first; no site gets more than STEM_LINKS."""
    n = len(sites)
    deg, pairs, out, per_hub = [0] * n, set(), [], {}
    order = sorted(range(n), key=lambda k: (-sites[k]["visits"], sites[k]["site"]))
    for k in order:
        hub = next((h for h, _ in sites[k]["hubs"] if h in hub_at), None)
        if hub is not None and per_hub.get(hub, 0) < HUB_SITES:
            per_hub[hub] = per_hub.get(hub, 0) + 1
            deg[k] += 1
            out.append((first + k, hub_at[hub]))
    at = {r["site"]: k for k, r in enumerate(sites)}
    cand: list[list[int]] = [[at[s] for s in r["related"] if s in at] for r in sites]
    for key in (lambda r: r["hubs"][0][0] if r["hubs"] else None, lambda r: r["category"]):
        groups: dict = {}
        for k, r in enumerate(sites):  # node order is first-seen order
            if (g := key(r)) is not None:
                groups.setdefault(g, []).append(k)
        for ks in groups.values():
            for t, k in enumerate(ks):
                cand[k] += ks[max(t - 1, 0):t] + ks[t + 1:t + 2]
    for k in order:
        for j in cand[k]:
            if deg[k] >= STEM_LINKS:
                break
            p = (min(j, k), max(j, k))
            if j == k or deg[j] >= STEM_LINKS or p in pairs:
                continue
            pairs.add(p)
            deg[k] += 1
            deg[j] += 1
            out.append((first + p[0], first + p[1]))
    return out


def with_stem(brain: dict, sites: list[dict], placed: dict, total: int) -> dict:
    """`brain` (a `build` blob) with the websites as a stem below the lobes: one node per site
    (`site:<site>`) after the pages, outside every lobe (lobe 65535), weighted by visits; their
    fibres (`_stem_links`, as similar-meaning fibres); and `stem` {first, total, shown, sites:
    [[site, category, visits, first day, last day, private]], colors: {category: hex}}.
    The mesh gains the faint tube round them (`stem_tube`).
    `sites`: site_rows records in node order, each with `first_day`, `last_day` and
    `private`; `placed` holds their axis shares (`stem_order`). The blob's other parts are
    shared, not copied."""
    nd = brain["nodes"]
    n0, m = len(nd["ids"]), len(sites)
    n = n0 + m

    def col(k, t, w=1):
        return np.frombuffer(base64.b64decode(nd[k]), t).reshape(-1, w)

    xyz = stem_xyz([r["site"] for r in sites], [placed[r["site"]][1] for r in sites])
    visits = np.maximum(np.array([r["visits"] or 0 for r in sites], float), 0)
    weight = np.log1p(visits) / max(math.log1p(visits.max(initial=0)), 1e-9)
    nodes = {"ids": [*nd["ids"], *(f"site:{r['site']}" for r in sites)],
             "xyz": _b64(np.concatenate([col("xyz", "<f4", 3), xyz]), "<f4"),
             "lobe": _b64(np.concatenate([col("lobe", "<u2")[:, 0], np.full(m, 65535)]), "<u2"),
             "depth": _b64(np.concatenate([col("depth", "<f4")[:, 0],
                                           np.maximum(-_field(xyz), 0) if m else []]), "<f4"),
             "embedded": _b64(np.concatenate([col("embedded", "u1")[:, 0], np.zeros(m)]), "u1"),
             "weight": _b64(np.concatenate([col("weight", "<f4")[:, 0], weight]), "<f4"),
             "added": nd.get("added", {})}  # pages only: a site is never a growth
    old_none, none = (0xFFFFFFFF if n0 >= 65535 else 65535), (0xFFFFFFFF if n >= 65535 else 65535)
    near = np.frombuffer(base64.b64decode(brain["near"]["ids"]),
                         "<u4" if n0 >= 65535 else "<u2").astype(np.int64).reshape(-1, N_NEAR)
    near = np.where(near == old_none, none, near)
    near = np.concatenate([near, np.full((m, N_NEAR), none, np.int64)])
    fb = brain["fibres"]
    links = np.array(_stem_links(sites, {i: k for k, i in enumerate(nd["ids"])}, n0),
                     np.int64).reshape(-1, 2)
    allp = np.concatenate([col("xyz", "<f4", 3), xyz])
    fibres = {"a": _b64(np.concatenate([np.frombuffer(base64.b64decode(fb["a"]), "<u4"),
                                        links[:, 0]]), "<u4"),
              "b": _b64(np.concatenate([np.frombuffer(base64.b64decode(fb["b"]), "<u4"),
                                        links[:, 1]]), "<u4"),
              "c": _b64(np.concatenate([np.frombuffer(base64.b64decode(fb["c"]), "<f4"),
                                        ((allp[links[:, 0]] + allp[links[:, 1]]) / 2).ravel()]),
                        "<f4"),
              "k": _b64(np.concatenate([np.frombuffer(base64.b64decode(fb["k"]), "u1"),
                                        np.ones(len(links))]), "u1")}
    return {**brain, "nodes": nodes, "near": {"ids": _b64(near, "<u4" if n >= 65535 else "<u2")},
            "fibres": fibres, "mesh": [*brain["mesh"], stem_tube()], "stem": {
                "first": n0, "total": total, "shown": m,
                "sites": [[r["site"], r["category"] or "other", r["visits"], r["first_day"],
                           r["last_day"], int(r["private"])] for r in sites],
                "colors": {c: site_color(c) for c in sorted({r["category"] or "other"
                                                            for r in sites})}}}


def preservation(xyz: np.ndarray, vec: np.ndarray, has: np.ndarray, lobe: np.ndarray,
                 skip=(), sample: int = 50, k3: int = 5, kv: int = 20) -> float:
    """The in-lobe check: for `sample` pages (seeded), the share of their k3 nearest same-lobe
    points in 3D that are among their kv nearest same-lobe pages by vector."""
    rows = [r for r in range(len(xyz)) if has[r] and lobe[r] not in skip
            and (has & (lobe == lobe[r])).sum() > kv]
    rows = sorted(rows, key=lambda r: _u(f"sample:{r}", 1)[0])[:sample]
    got = []
    for r in rows:
        m = np.flatnonzero(has & (lobe == lobe[r]))
        m = m[m != r]
        a = m[np.argsort(np.linalg.norm(xyz[m] - xyz[r], axis=1), kind="stable")[:k3]]
        b = m[np.argsort(-(vec[m] @ vec[r]), kind="stable")[:kv]]
        got.append(len(set(a) & set(b)) / k3)
    return float(np.mean(got)) if got else 1.0
