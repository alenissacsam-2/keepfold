"""Brain v3 layout (docs/design/brain-v3.md, brain-v2.md): fixture vaults with a fake vector
cache only."""

import base64
import hashlib
import json
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

from keepfold import brainmap, dashboard, vecstore
from keepfold.cli import app
from keepfold.semantic import CACHE
from keepfold.vault import Vault

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
# 12 topic lobes in 4 families of 3 (hub-0..hub-2, hub-3..hub-5, ...): the lobes of a family
# are about the same thing. (Lobes all unrelated to each other are equidistant: no 3D map can
# keep more than four of them apart, so the fixture gives the map some structure.)
LOBES, PER, DIM = 12, 30, 32
CHAT = "Timeline: [[2026-03-01]] · Source: Gemini · Date: 2026-03-05\n"


def _unit(v):
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


def _note(root: Path, folder: str, id: str, fields: dict, body: str = "x\n") -> None:
    (root / folder).mkdir(exist_ok=True)
    fm = "".join(f"{k}: {v}\n" for k, v in fields.items())
    (root / folder / f"{id}.md").write_text(f"---\n{fm}---\n{body}", encoding="utf-8")


def _write_vectors(root: Path, vecs: dict) -> None:
    with closing(vecstore.connect(root / CACHE)) as con:
        vecstore.prepare(con, {"model": "fake", "dim": len(next(iter(vecs.values())))})
        # the manifest names the vector, so a changed one replaces the stored row
        vecstore.put(con, {i: (hashlib.md5(np.asarray(v).tobytes()).hexdigest(), True,
                               np.zeros(1, np.int32), np.asarray(v)[None])
                           for i, v in vecs.items()})


@pytest.fixture
def made(tmp_path):
    """A vault of LOBES topic hubs with PER pages each in 3 sub-topics, 12 unsorted
    chats, 3 journal pages and a private page, plus its vectors."""
    r = tmp_path / "v"
    r.mkdir()
    (r / "catalog.yaml").write_text("schema: 1\ndomains: []\n", encoding="utf-8")
    rng = np.random.default_rng(0)
    family = [_unit(rng.normal(size=DIM)) for _ in range(LOBES // 3)]
    cent = [_unit(family[k // 3] + 0.4 * _unit(rng.normal(size=DIM))) for k in range(LOBES)]
    vecs = {}
    for k in range(LOBES):
        _note(r, "notes", f"hub-{k}", {"kind": "note", "title": f'"Hub {k}"', "tags": "[topic]"})
        vecs[f"hub-{k}"] = cent[k]
        subs = [_unit(cent[k] + 0.6 * _unit(rng.normal(size=DIM))) for _ in range(3)]
        for i in range(PER):
            _note(r, "notes", f"p-{k}-{i:02d}", {"kind": "note", "related": f'["[[hub-{k}]]"]'})
            vecs[f"p-{k}-{i:02d}"] = _unit(subs[i % 3] + 0.25 * _unit(rng.normal(size=DIM)))
    for i in range(12):
        _note(r, "resources", f"chat-{i:02d}", {"kind": "resource"}, CHAT)
        vecs[f"chat-{i:02d}"] = _unit(cent[i % 3] + 0.2 * _unit(rng.normal(size=DIM)))
    for i in range(3):
        _note(r, "journal", f"2026-11-0{i + 1}", {"kind": "journal"})
        vecs[f"2026-11-0{i + 1}"] = _unit(rng.normal(size=DIM))
    _note(r, "notes", "secret", {"kind": "note", "private": "true", "related": '["[[hub-0]]"]'})
    vecs["secret"] = cent[0]
    _write_vectors(r, vecs)
    return r, vecs


def _brain(root: Path, **kw) -> dict:
    return dashboard.build_data(Vault(root), now=NOW, **kw)["brain"]


def _arr(b64: str, dtype: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(b64), dtype)


def _xyz(brain: dict) -> dict:
    p = _arr(brain["nodes"]["xyz"], "<f4").reshape(-1, 3)
    return dict(zip(brain["nodes"]["ids"], map(tuple, p.tolist()), strict=True))


def test_contract_arrays_and_mesh(made):
    root, _ = made
    d = dashboard.build_data(Vault(root), now=NOW)
    b = d["brain"]
    assert set(b) == {"version", "legend", "nodes", "lobes", "mesh", "fibres", "tracts", "near",
                      "beacons", "stats"}
    assert b["version"] == 3 and b["legend"].startswith("lobe = a topic")
    assert "bright fibres = your links; faint fibres = similar meaning" in b["legend"]
    ids = b["nodes"]["ids"]
    n, nl = len(ids), len(b["lobes"])
    assert ids == [p["id"] for p in d["pages"]] and "secret" not in ids  # private: absent
    assert [x["id"] for x in b["lobes"]] == [c["id"] for c in d["clusters"]]
    assert set(b["lobes"][0]) == {"id", "title", "kind", "count", "anchor", "normal", "centre",
                                  "color", "archive", "area"}
    xyz = _arr(b["nodes"]["xyz"], "<f4").reshape(-1, 3)
    assert len(xyz) == n
    lobe = _arr(b["nodes"]["lobe"], "<u2")
    assert len(lobe) == n and lobe.max() < nl
    assert [b["lobes"][x]["id"] for x in lobe] == [p["cluster"] for p in d["pages"]]
    ring = np.array([b["lobes"][x]["archive"] for x in lobe])
    assert brainmap.inside(xyz[~ring]).all() and not brainmap.inside(xyz[ring]).any()
    for x in b["lobes"]:
        if not x["archive"]:
            assert brainmap.inside([x["anchor"], x["centre"]]).all()
    assert len(_arr(b["nodes"]["depth"], "<f4")) == n
    w = _arr(b["nodes"]["weight"], "<f4")
    assert len(w) == n and (w >= 0).all() and (w <= 1).all()
    assert w[ids.index("hub-0")] > np.percentile(w, 90)  # the most linked weigh most
    assert _arr(b["nodes"]["embedded"], "u1").tolist() == [1] * n
    near = _arr(b["near"]["ids"], "<u2").reshape(n, 8)
    assert near.max() < n and all(r not in row for r, row in enumerate(near))
    assert b["stats"] == {"embedded": n, "unembedded": 0, "relayout": True, "vectors": True}
    (m,) = b["mesh"]
    assert m["name"] == "brain"
    pos, nrm = (_arr(m[k], "<f4").reshape(-1, 3) for k in ("pos", "nrm"))
    assert nrm.shape == pos.shape and np.isfinite(pos).all() and np.isfinite(nrm).all()
    tri = _arr(m["idx"], "<u4").reshape(-1, 3)
    assert tri.max() < len(pos) and len(tri) <= 40_000
    assert np.abs(brainmap._field(pos.astype(float))).max() < 1e-3  # on the surface
    a, bb, c = pos[tri[:, 0]], pos[tri[:, 1]], pos[tri[:, 2]]
    face = (np.cross(bb - a, c - a) * (nrm[tri[:, 0]] + nrm[tri[:, 1]] + nrm[tri[:, 2]])).sum(1)
    assert (face > 0).mean() > 0.99  # counter-clockwise seen from outside, smooth normals
    ml = _arr(m["lobe"], "<u2")
    assert len(ml) == len(pos) and ((ml < nl) | (ml == 65535)).all() and (ml < nl).mean() > 0.9
    text = json.dumps(b)
    assert "secret" not in text
    for part in (b["nodes"], m, b["fibres"]):
        for v in part.values():
            if isinstance(v, str):
                assert "<" not in v and "://" not in v
    journal = next(x for x in b["lobes"] if x["id"] == "journal")
    assert journal["anchor"][2] < -0.3 and journal["anchor"][1] < -0.1  # in the cerebellum


def test_rebuild_is_byte_identical_and_persisted(made):
    root, _ = made
    first = _brain(root)
    layout = root / brainmap.LAYOUT
    stamp = layout.stat().st_mtime_ns
    second, third = _brain(root), _brain(root)
    assert first["stats"]["relayout"] and not second["stats"]["relayout"]
    assert json.dumps(second) == json.dumps(third)
    assert json.dumps(first | {"stats": None}) == json.dumps(second | {"stats": None})
    assert layout.stat().st_mtime_ns == stamp  # an unchanged layout is not rewritten
    st = json.loads(layout.read_text(encoding="utf-8"))
    assert st["version"] == 3 and st["params_hash"] == brainmap.PARAMS_HASH
    assert st["pages"]["p-0-00"][3] == "hub-0" and "secret" not in st["pages"]


def test_new_page_moves_nothing_and_lands_by_its_neighbour(made):
    root, vecs = made
    before = _xyz(_brain(root))
    _note(root, "notes", "p-0-new", {"kind": "note", "related": '["[[hub-0]]"]'})
    vecs["p-0-new"] = _unit(vecs["p-0-05"] + 0.01)
    _write_vectors(root, vecs)
    b = _brain(root)
    after = _xyz(b)
    assert not b["stats"]["relayout"]
    assert {i: after[i] for i in before} == before
    new = np.array(after["p-0-new"])
    lobe0 = [i for i in before if i.startswith("p-0-")]
    dist = sorted((np.linalg.norm(np.array(before[i]) - new), i) for i in lobe0)
    assert "p-0-05" in [i for _, i in dist[:5]]



def test_added_stamps_only_new_pages_and_keeps_them(made, monkeypatch):
    root, vecs = made
    layout = root / brainmap.LAYOUT
    assert _brain(root)["nodes"]["added"] == {}  # a first layout: nothing is new
    stamps = iter(["2026-11-20T09:00:00.000Z", "2026-11-20T10:00:00.000Z"])
    monkeypatch.setattr(brainmap, "_now", lambda: next(stamps))
    monkeypatch.setattr(brainmap, "_recent", dict)  # the fixed stamps above are kept
    _note(root, "notes", "p-0-new", {"kind": "note", "related": '["[[hub-0]]"]'})
    vecs["p-0-new"] = _unit(vecs["p-0-05"] + 0.01)
    _write_vectors(root, vecs)
    assert _brain(root)["nodes"]["added"] == {"p-0-new": "2026-11-20T09:00:00.000Z"}
    _note(root, "notes", "p-1-new", {"kind": "note", "related": '["[[hub-1]]"]'})
    b = _brain(root)
    assert b["nodes"]["added"] == {"p-0-new": "2026-11-20T09:00:00.000Z",
                                   "p-1-new": "2026-11-20T10:00:00.000Z"}
    st = json.loads(layout.read_text(encoding="utf-8"))
    assert st["added"] == b["nodes"]["added"]
    assert _brain(root, relayout=True)["nodes"]["added"] == b["nodes"]["added"]  # survives
    (root / "notes" / "p-1-new.md").unlink()
    assert _brain(root)["nodes"]["added"] == {"p-0-new": "2026-11-20T09:00:00.000Z"}


def test_an_old_layout_has_no_added_stamps(made):
    root, _ = made
    _brain(root)
    layout = root / brainmap.LAYOUT
    st = json.loads(layout.read_text(encoding="utf-8"))
    del st["added"]  # a layout written before the stamps
    layout.write_text(json.dumps(st), encoding="utf-8")
    b = _brain(root)
    assert b["nodes"]["added"] == {} and not b["stats"]["relayout"]
    st["added"] = {"p-0-00": 5, "p-0-01": "2000-01-01T00:00:00.000Z"}  # odd values: ignored
    layout.write_text(json.dumps(st), encoding="utf-8")
    assert _brain(root)["nodes"]["added"] == {}  # too old for the blob
    kept = json.loads(layout.read_text(encoding="utf-8"))["added"]
    assert kept == {"p-0-01": "2000-01-01T00:00:00.000Z"}


def test_related_lobes_lie_together(made):
    """By the 3-D distance between region centres, every lobe's nearest family is its own,
    its region touches a family member's, and a family lies together at under 0.7 of the mean
    distance of all pairs (equal regions packed in a volume: adjacent centres already sit at
    about 0.55 of it, so §7's 0.5 for angles on a surface is out of reach here)."""
    root, _ = made
    lobes = _brain(root)["lobes"]
    c = {x["id"]: np.array(x["centre"]) for x in lobes if x["kind"] == "topic"}
    st = json.loads((root / brainmap.LAYOUT).read_text(encoding="utf-8"))
    at = {x["id"]: k for k, x in enumerate(lobes)}
    adj = brainmap._adjacent(brainmap._regions(st["lobes"], list(at))[1], len(lobes))
    families = [[f"hub-{3 * f + i}" for i in range(3)] for f in range(LOBES // 3)]

    def dist(i, j):
        return np.linalg.norm(c[i] - c[j])

    every = np.mean([dist(a, b) for k, a in enumerate(c) for b in list(c)[k + 1:]])
    for family in families:
        for i in family:
            near = min(families, key=lambda f: np.mean([dist(i, j) for j in f if j != i]))
            assert near is family
            assert any(adj[at[i], at[j]] for j in family if j != i)
        inside = np.mean([dist(family[0], family[1]), dist(family[0], family[2]),
                          dist(family[1], family[2])])
        assert inside < 0.7 * every


def test_pages_in_a_lobe_keep_their_neighbours(made):
    root, _ = made
    b = _brain(root)
    ids = b["nodes"]["ids"]
    vec, has, ok = brainmap._vectors(root, ids)
    lobe = _arr(b["nodes"]["lobe"], "<u2")
    ring = [k for k, x in enumerate(b["lobes"]) if x["archive"]]
    xyz = _arr(b["nodes"]["xyz"], "<f4").reshape(-1, 3).astype(float)
    assert ok and brainmap.preservation(xyz, vec, has, lobe, skip=ring) >= 0.6


def test_regions_are_round_with_their_hubs_inside(made):
    """Each region (its share of the volume samples) is a rounded cloud, not a slab: v3's
    fixed sites left cells down to 0.22 smallest-to-largest axis (mean 0.36); and each lobe's
    hub page sits nearer its lobe's centre than the median page."""
    root, _ = made
    b = _brain(root)
    st = json.loads((root / brainmap.LAYOUT).read_text(encoding="utf-8"))
    _, lab = brainmap._regions(st["lobes"], [x["id"] for x in b["lobes"]])
    p = brainmap._samples()[0]
    ratio = []
    for k, x in enumerate(b["lobes"]):
        if not x["archive"] and (lab == k).sum() >= 50:
            ev = np.linalg.eigvalsh(np.cov(p[lab == k].T))
            ratio.append(np.sqrt(ev[0] / ev[2]))
    assert len(ratio) >= 12 and min(ratio) > 0.33 and np.mean(ratio) > 0.5
    ids = b["nodes"]["ids"]
    xyz = _arr(b["nodes"]["xyz"], "<f4").reshape(-1, 3)
    lobe = _arr(b["nodes"]["lobe"], "<u2")
    for k in range(LOBES):
        r = ids.index(f"hub-{k}")
        own = xyz[lobe == lobe[r]]
        c = own.mean(0)
        assert np.linalg.norm(xyz[r] - c) < np.median(np.linalg.norm(own - c, axis=1))


def test_unsorted_chats_form_the_ring_with_beacons(made):
    root, _ = made
    b = _brain(root)
    ids = b["nodes"]["ids"]
    k = next(i for i, x in enumerate(b["lobes"]) if x["id"] == "chats")
    assert b["lobes"][k]["archive"] and b["lobes"][k]["area"] == 0
    assert not any(x["archive"] for x in b["lobes"] if x["id"] != "chats")
    xyz = _xyz(b)
    chats = [i for i in ids if i.startswith("chat-")]
    band = np.array([xyz[i] for i in chats])
    off = np.hypot(np.hypot(band[:, 0], band[:, 2]) - brainmap.RING_R, band[:, 1] - brainmap.RING_Y)
    assert (off <= brainmap.RING_W + 0.1).all() and band[:, 1].max() <= brainmap.RING_Y + 0.08
    assert np.ptp(band[:, 1]) > brainmap.RING_W  # a band with depth, not a flat line
    for i in chats:
        beacons = b["beacons"][str(ids.index(i))]
        assert len(beacons) == 3 and k not in beacons
        assert b["lobes"][beacons[0]]["id"] == f"hub-{int(i[-2:]) % 3}"
    assert len(b["beacons"]) == len(chats)
    assert all(t["a"] != k and t["b"] != k for t in b["tracts"])


def test_without_vectors_everything_is_unembedded(made):
    root, _ = made
    (root / CACHE).unlink()
    b = _brain(root)
    n = len(b["nodes"]["ids"])
    assert b["stats"] == {"embedded": 0, "unembedded": n, "relayout": True, "vectors": False}
    assert set(_arr(b["near"]["ids"], "<u2").tolist()) == {65535}
    assert _arr(b["nodes"]["embedded"], "u1").sum() == 0 and not b["beacons"]
    assert np.isfinite(_arr(b["nodes"]["xyz"], "<f4")).all()
    (root / CACHE).write_bytes(b"not a zip file")  # unreadable: the same, and no error
    b2 = _brain(root)
    assert b2["stats"]["vectors"] is False and not b2["stats"]["relayout"]
    assert b2["nodes"] == b["nodes"]
    assert set(_arr(b["fibres"]["k"], "u1").tolist()) <= {0}  # no vectors: links only


def test_links_draw_tracts_between_lobes(made):
    root, _ = made
    for i in range(3):
        p = root / "notes" / f"p-3-{i:02d}.md"
        p.write_text(p.read_text(encoding="utf-8") + f"[[p-4-{i:02d}]] [[p-4-1{i}]]\n",
                     encoding="utf-8")
    b = _brain(root)
    k3, k4 = (next(i for i, x in enumerate(b["lobes"]) if x["id"] == h)
              for h in ("hub-3", "hub-4"))
    (t,) = b["tracts"]
    assert {t["a"], t["b"]} == {k3, k4} and t["n"] == 6 and len(t["p"]) == 4
    assert t["p"][0] == b["lobes"][t["a"]]["anchor"] and t["p"][3] == b["lobes"][t["b"]]["anchor"]


def _hue(color: str) -> float:
    """The OKLCH hue of an sRGB hex colour."""
    rgb = np.array([int(color[i:i + 2], 16) for i in (1, 3, 5)]) / 255
    lin = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    lms = np.cbrt(np.array([[0.4122214708, 0.5363325363, 0.0514459929],
                            [0.2119034982, 0.6806995451, 0.1073969566],
                            [0.0883024619, 0.2817188376, 0.6299787005]]) @ lin)
    a = np.array([1.9779984951, -2.4285922050, 0.4505937099]) @ lms
    b = np.array([0.0259040371, 0.7827717662, -0.8086757660]) @ lms
    return float(np.degrees(np.arctan2(b, a)) % 360)


def test_lobe_colours_are_distinct_and_touching_regions_differ(made):
    root, _ = made
    lobes = _brain(root)["lobes"]
    colors = [x["color"] for x in lobes]
    assert all(len(c) == 7 and c[0] == "#" and int(c[1:], 16) >= 0 for c in colors)
    assert len(set(colors)) == len(colors)
    st = json.loads((root / brainmap.LAYOUT).read_text(encoding="utf-8"))
    _, lab = brainmap._regions(st["lobes"], [x["id"] for x in lobes])
    adj = brainmap._adjacent(lab, len(lobes))
    tinted = [k for k, x in enumerate(lobes) if not x["archive"]
              and x["kind"] in ("topic", "domain", "group")]
    pairs = [(i, j) for i in tinted for j in tinted if i < j and adj[i, j]]
    assert len(pairs) >= len(tinted)  # the check is not vacuous
    for i, j in pairs:  # 25 degrees apart, less what 8-bit rounding moves a hue
        d = abs(_hue(colors[i]) - _hue(colors[j])) % 360
        assert min(d, 360 - d) >= brainmap.HUE_GAP - 2, (lobes[i]["id"], lobes[j]["id"])
    chats = next(x for x in lobes if x["id"] == "chats")
    r, g, bb = (int(chats["color"][i:i + 2], 16) for i in (1, 3, 5))
    assert r == g == bb  # the archive is grey


def test_fibres_are_the_links_and_three_nearest(made):
    root, _ = made
    for i in range(3):  # a few cross-lobe links, one of them written twice
        p = root / "notes" / f"p-3-{i:02d}.md"
        p.write_text(p.read_text(encoding="utf-8") + f"[[p-4-{i:02d}]] [[p-4-{i:02d}]]\n",
                     encoding="utf-8")
    d = dashboard.build_data(Vault(root), now=NOW)
    b = d["brain"]
    ids = b["nodes"]["ids"]
    n, at = len(ids), {i: k for k, i in enumerate(ids)}
    f = {k: _arr(b["fibres"][k], t) for k, t in (("a", "<u4"), ("b", "<u4"), ("k", "u1"))}
    ctrl = _arr(b["fibres"]["c"], "<f4").reshape(-1, 3)
    m = len(f["k"])
    assert len(f["a"]) == len(f["b"]) == len(ctrl) == m and set(f["k"].tolist()) == {0, 1}
    assert max(f["a"].max(), f["b"].max()) < n and (f["a"] != f["b"]).all()
    assert np.isfinite(ctrl).all()
    pair = [(min(a, c), max(a, c)) for a, c in zip(f["a"].tolist(), f["b"].tolist(), strict=True)]
    wiki = {(min(a, c), max(a, c)) for p in d["pages"] if (a := at[p["id"]]) is not None
            for c in (at[x] for x in p["links"] if x in at) if a != c}
    linked = [x for x, k in zip(pair, f["k"], strict=True) if k == 0]
    similar = [x for x, k in zip(pair, f["k"], strict=True) if k == 1]
    assert len(linked) == len(set(linked)) and set(linked) == wiki
    assert len(similar) == len(set(similar)) and not set(similar) & wiki
    near = _arr(b["near"]["ids"], "<u2").reshape(n, 8)
    src = f["a"][f["k"] == 1]
    assert np.bincount(src, minlength=n).max() <= 3
    for a, c in zip(src, f["b"][f["k"] == 1], strict=True):
        assert c in near[a, :3]
    xyz = _arr(b["nodes"]["xyz"], "<f4").reshape(-1, 3)
    mid = (xyz[f["a"]] + xyz[f["b"]]) / 2  # a control point bends its fibre, never far
    assert np.linalg.norm(ctrl - mid, axis=1).max() < 1.0


def _spacings(b: dict, root: Path) -> np.ndarray:
    """Each page's region spacing: its region's volume over every page in it (guests too)."""
    st = json.loads((root / brainmap.LAYOUT).read_text(encoding="utf-8"))
    lobes = b["lobes"]
    host = [st["lobes"][x["id"]].get("host", x["id"]) for x in lobes]
    area = {x["id"]: x["area"] for x in lobes if x["area"]}
    count: dict = {}
    for h, x in zip(host, lobes, strict=True):
        count[h] = count.get(h, 0) + x["count"]
    vol = brainmap.SAMPLES * brainmap._samples()[2]
    per = [(area[h] * vol / count[h]) ** (1 / 3) if h in area else np.nan for h in host]
    return np.array(per)[_arr(b["nodes"]["lobe"], "<u2")]


def _nn(xyz: np.ndarray) -> np.ndarray:
    d = np.linalg.norm(xyz[:, None] - xyz[None], axis=2)
    np.fill_diagonal(d, np.inf)
    return d.min(1)


def test_new_hub_and_near_copies_never_stack(made):
    """Gate Q5: a new 40-page topic (a guest lobe) and 40 near-copies of one page, placed
    without a relayout, keep every page at least 0.3 x its region's spacing from any other;
    nothing placed before moves; each copy stays within 3 spacings of its original."""
    root, vecs = made
    before = _xyz(_brain(root))
    rng = np.random.default_rng(7)
    hub = _unit(rng.normal(size=DIM))
    _note(root, "notes", "hub-new", {"kind": "note", "title": '"Hub new"', "tags": "[topic]"})
    vecs["hub-new"] = hub
    for i in range(40):
        _note(root, "notes", f"q-{i:02d}", {"kind": "note", "related": '["[[hub-new]]"]'})
        vecs[f"q-{i:02d}"] = _unit(hub + 0.3 * _unit(rng.normal(size=DIM)))
        _note(root, "notes", f"copy-{i:02d}", {"kind": "note", "related": '["[[hub-0]]"]'})
        vecs[f"copy-{i:02d}"] = _unit(vecs["p-0-05"] + 1e-4 * rng.normal(size=DIM))
    _write_vectors(root, vecs)
    b = _brain(root, auto_relayout=False)
    assert not b["stats"]["relayout"]
    after = _xyz(b)
    assert {i: after[i] for i in before} == before
    st = json.loads((root / brainmap.LAYOUT).read_text(encoding="utf-8"))
    assert "host" in st["lobes"]["hub-new"] and st["room"] == 1  # the new topic is a guest
    ids = b["nodes"]["ids"]
    ring = np.array([b["lobes"][x]["archive"] for x in _arr(b["nodes"]["lobe"], "<u2")])
    xyz = _arr(b["nodes"]["xyz"], "<f4").reshape(-1, 3).astype(float)
    assert brainmap.inside(xyz[~ring]).all()
    sp = _spacings(b, root)
    nn = _nn(xyz[~ring])
    assert np.isfinite(sp[~ring]).all() and (nn >= 0.3 * sp[~ring]).all(), (nn / sp[~ring]).min()
    p05, s05 = xyz[ids.index("p-0-05")], sp[ids.index("p-0-05")]
    for i in range(40):
        assert np.linalg.norm(xyz[ids.index(f"copy-{i:02d}")] - p05) <= 3 * s05


def test_old_pages_do_not_move(made, tmp_path):
    """A stored layout without the "room" mark (written before it) has its stacked pages
    moved once, the same way every time; every other page keeps its stored place."""
    import shutil

    root, _ = made
    _brain(root)
    layout = root / brainmap.LAYOUT
    st = json.loads(layout.read_text(encoding="utf-8"))
    del st["room"]
    stack = [f"p-1-{i:02d}" for i in range(6)]
    for i in stack[1:]:  # five pages on top of p-1-00, as incremental placement left them
        st["pages"][i][:3] = st["pages"][stack[0]][:3]
    layout.write_text(json.dumps(st), encoding="utf-8")
    twin = tmp_path / "twin"
    shutil.copytree(root, twin)
    b = _brain(root)
    assert not b["stats"]["relayout"]
    got = json.loads(layout.read_text(encoding="utf-8"))
    assert got["room"] == 1
    assert {i: v for i, v in got["pages"].items() if i not in stack} == {
        i: v for i, v in st["pages"].items() if i not in stack}
    xyz = _xyz(b)
    pts = np.array([xyz[i] for i in stack])
    sp = _spacings(b, root)[b["nodes"]["ids"].index(stack[0])]
    assert _nn(pts).min() >= 0.3 * sp and brainmap.inside(pts).all()
    text = layout.read_text(encoding="utf-8")
    assert _xyz(_brain(root)) == xyz and layout.read_text(encoding="utf-8") == text  # once
    _brain(twin)
    assert (twin / brainmap.LAYOUT).read_text(encoding="utf-8") == text  # deterministic


@pytest.mark.perf
def test_placing_20k_points_is_fast():
    """Batch placement (nearest 3 by one block matmul) and room for 20k new points among 5k
    placed ones in a unit cube, under 4 s (1.5-2.1 s measured on a PC loaded to 100 % by other
    work; the push alone 0.6-0.9 s, the rest the 20k x 5k matmul). Afterwards almost no new
    point is within 0.3 spacing of another, and none within 0.2: ROOM_ROUNDS can leave a few
    boxed-in points short of ROOM. (No region here: `keep` leaves points where they are pushed.)"""
    import time

    rng = np.random.default_rng(3)
    old, new = 5_000, 20_000
    s = (1 / (old + new)) ** (1 / 3)
    ov = _unit(rng.normal(size=(old, DIM))).astype(np.float32)
    nv = _unit(ov[rng.integers(0, old, new)] + 0.05 * rng.normal(size=(new, DIM)))
    nv = nv.astype(np.float32)
    p = np.concatenate([rng.random((old, 3)), np.zeros((new, 3))])
    keys = [f"p{i}" for i in range(old + new)]
    t = time.perf_counter()
    best, sims = brainmap._top(nv, ov, 3)
    w = np.exp(50 * (sims - sims[:, :1]))
    p[old:] = (w[..., None] * p[best]).sum(1) / w.sum(1, keepdims=True)
    out = brainmap._room(p, np.arange(old, old + new), brainmap.ROOM * s, keys, lambda q, i: q)
    took = time.perf_counter() - t
    assert (out[:old] == p[:old]).all()
    fresh = np.arange(old, old + new)  # the old points are random: some lie close together
    assert len(np.unique(brainmap._pairs(out, 0.3 * s, fresh)[0])) <= new // 1000
    assert not len(brainmap._pairs(out, 0.2 * s, fresh)[0])
    assert took < 4, took


def test_cli_relayout(made, monkeypatch):
    root, _ = made
    monkeypatch.delenv("KEEPFOLD_VAULT", raising=False)
    v, now = Vault(root), datetime.now(UTC)
    assert dashboard.build(v, now=now)["brain"]["stats"]["relayout"] is True  # the first lays out
    plain = dashboard.build(v, now=now)["brain"]
    assert plain["stats"]["relayout"] is False
    seen, real = [], dashboard.build_data

    def build_data(*a, **k):
        out = real(*a, **k)
        seen.append(out["brain"])
        return out
    monkeypatch.setattr(dashboard, "build_data", build_data)
    for flags in ((), ("--relayout",)):  # only --relayout builds the brain
        r = CliRunner().invoke(app, ["dashboard", "--no-open", "--vault", str(root), *flags])
        assert r.exit_code == 0, r.output
    assert len(seen) == 1
    assert seen[0]["stats"]["relayout"] is True and seen[0]["nodes"] == plain["nodes"]
