"""The brain's time scrub (a bar per month above the Growth range; 1 year, All and Play) and the
Focus Lens (a Home row's page and its linked pages stay lit, the rest dims as in Today). The lens
rules run under node on their own; the controls run in the headless browser of
test_dashboard_js.py. Skipped without node or a Chromium browser."""

import json
import shutil
import subprocess
from datetime import UTC, datetime

import pytest
from test_dashboard_js import _run, demo_vault  # noqa: F401  (demo_vault: a fixture)

from keepfold import dashboard
from keepfold.vault import Vault

BRAIN_JS = dashboard.ASSETS / "brain3d.js"

HARNESS = r"""
globalThis.window = globalThis;
require(process.argv[2]);
const L = window.KeepfoldBrain.LENS, out = {};
const G = (edges) => {
  const m = new Map();
  const add = (a, b) => { if (!m.has(a)) m.set(a, new Set()); m.get(a).add(b); };
  for (const [a, b] of edges) { add(a, b); add(b, a); }
  return (id) => m.get(id) || [];
};
// 1 link away: five of them are enough, the page first
out.five = L.set('p', G([['p', 'a'], ['p', 'b'], ['p', 'c'], ['p', 'd'], ['p', 'e'], ['a', 'far']]));
// fewer than five: the pages 2 links away join, after the near ones
out.few = L.set('p', G([['p', 'a'], ['p', 'b'], ['a', 'x'], ['b', 'y'], ['x', 'z']]));
out.alone = L.set('p', G([]));
// at most 40 in all
const star = []; for (let i = 0; i < 100; i++) star.push(['p', 'n' + i]);
out.cap = L.set('p', G(star));
out.capN = out.cap.length;
// 4 near pages with 60 behind each: still 40
const wide = []; for (let i = 0; i < 4; i++) { wide.push(['p', 'a' + i]); for (let j = 0; j < 60; j++) wide.push(['a' + i, 'b' + i + '_' + j]); }
out.wideN = L.set('p', G(wide)).length;
// 5k pages, about 6 links each: build the neighbours (once), then take the set, cold and warm
const N = 5000, pages = [];
let s = 7; const rnd = () => (s = (s * 1664525 + 1013904223) % 4294967296) / 4294967296;
for (let i = 0; i < N; i++) { const links = []; for (let k = 0; k < 6; k++) links.push('p' + Math.floor(rnd() * N)); pages.push({ id: 'p' + i, links }); }
const t0 = performance.now();
const adj = new Map(), add = (a, b) => { if (!adj.has(a)) adj.set(a, new Set()); adj.get(a).add(b); };
for (const p of pages) for (const t of p.links) if (t !== p.id) { add(p.id, t); add(t, p.id); }
out.indexMs = performance.now() - t0;
const t1 = performance.now();
const cold = L.set('p42', (id) => adj.get(id) || []);
out.coldMs = performance.now() - t1;
let worst = 0;
for (let i = 0; i < 200; i++) { const t = performance.now(); L.set('p' + i * 7, (id) => adj.get(id) || []); worst = Math.max(worst, performance.now() - t); }
out.warmMs = worst;
out.coldN = cold.length;
console.log(JSON.stringify(out));
"""  # noqa: E501  (JavaScript)


def _node(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node")
    (tmp_path / "h.cjs").write_text(HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(tmp_path / "h.cjs"), str(BRAIN_JS)], capture_output=True,
                       text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_focus_set_rules(tmp_path):
    o = _node(tmp_path)
    assert o["five"] == ["p", "a", "b", "c", "d", "e"]  # 5 are 1 away: no 2-hop pages
    assert o["few"] == ["p", "a", "b", "x", "y"]  # 2 are 1 away: those 2 links further join, not 3
    assert o["alone"] == ["p"]
    assert o["capN"] == 40 and o["cap"][0] == "p"
    assert o["wideN"] == 40


@pytest.mark.perf
def test_focus_set_under_16_ms_on_5k_pages(tmp_path):
    o = _node(tmp_path)
    assert o["coldMs"] < 16, o["coldMs"]  # the first set, before the engine has warmed up
    assert o["indexMs"] < 100, o["indexMs"]  # the neighbours of 5k pages, built once
    assert o["warmMs"] < 16, o["warmMs"]


# ---- the controls, in the browser ----

@pytest.fixture
def page(tmp_path, demo_vault):  # noqa: F811
    """The demo vault's dashboard with pages dated across 30 months, a real loop, a loop of a page
    the brain does not hold, and every Home group showing."""
    data = dashboard.build(Vault(demo_vault), now=datetime.now(UTC))
    day = datetime.fromisoformat(data["day"][:10])
    for k, p in enumerate(data["pages"]):
        m = (day.year * 12 + day.month - 1) - (k % 30)
        p["date"] = f"{m // 12}-{m % 12 + 1:02d}-15"
    ids = set(data["brain"]["nodes"]["ids"])
    by = {p["id"]: p for p in data["pages"]}
    hub = max((p for p in data["pages"] if p["id"] in ids), key=lambda p: len(p["links"]))
    loop = {"id": hub["id"], "title": hub["title"], "loop_type": "idea", "created": data["day"],
            "due": None, "summary": ""}
    data["loops"] = [loop, {**loop, "id": "ghost-private", "title": "A ghost loop"}]
    data["signals"] = {}
    assert hub["id"] in by
    f = tmp_path / "scrub-lens.html"
    f.write_text(dashboard.render_html(data), encoding="utf-8")
    return f


SCRUB_PROBE = r"""
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  await __L.ready();
  const b = __L.brain(), out = {};
  document.querySelector('#home-brain-tools [data-mode="growth"]').click();
  const root = document.querySelector('.home-brain > .scrub'), range = root.querySelector('input');
  const bars = () => [...root.querySelectorAll('.sb')], lit = () => bars().filter((x) => x.classList.contains('on')).length;
  const btn = (t) => [...root.querySelectorAll('button')].find((x) => x.textContent === t);
  out.shown = !root.hidden;
  out.hiddenAria = root.querySelector('.scrub-bars').getAttribute('aria-hidden');
  out.label = range.getAttribute('aria-label');
  out.bars = bars().length; out.max = +range.max; out.lit = lit();
  out.tall = Math.max(...bars().map((x) => x.firstChild.offsetHeight));
  range.value = '3'; range.dispatchEvent(new Event('input', { bubbles: true }));
  out.afterInput = { lit: lit(), text: range.getAttribute('aria-valuetext'), growth: b.growth };
  bars()[10].click();
  out.afterClick = { value: +range.value, lit: lit() };
  btn('1 year').click();
  out.year = { bars: bars().length, max: +range.max, yr: btn('1 year').getAttribute('aria-pressed'), all: btn('All').getAttribute('aria-pressed'), lit: lit() };
  btn('All').click();
  out.all = { bars: bars().length, yr: btn('1 year').getAttribute('aria-pressed'), all: btn('All').getAttribute('aria-pressed') };
  // Play, on a fake clock
  let now = 1000; const cbs = [];
  performance.now = () => now;
  window.requestAnimationFrame = (f) => { cbs.push(f); return cbs.length; };
  window.cancelAnimationFrame = () => {};
  const tick = (ms) => { now += ms; const f = cbs.splice(0); f.forEach((x) => x(now)); };
  const play = btn('Play');
  out.playShown = !play.hidden;
  play.click();
  out.start = { text: play.textContent, value: +range.value };
  tick(3000);
  out.mid = { value: +range.value, growth: b.growth };
  tick(3100);
  out.end = { value: +range.value, max: +range.max, text: play.textContent, growth: b.growth };
  play.click(); tick(10);
  out.again = play.textContent;
  play.click();  // Pause
  out.paused = play.textContent;
  play.click(); tick(10); out.input = play.textContent;
  range.dispatchEvent(new Event('input', { bubbles: true }));
  out.afterRange = play.textContent;
  play.click(); tick(10);
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  out.afterEsc = play.textContent;
  play.click(); tick(10);
  document.querySelector('#home-brain-tools [data-mode="activity"]').click();
  out.afterMode = [play.textContent, root.hidden];
  return out;
})()
"""  # noqa: E501  (JavaScript)


@pytest.mark.xdist_group("browser")
def test_the_scrub_has_bars_spans_and_play(tmp_path, page):
    out = _run(tmp_path, page, probe=SCRUB_PROBE)
    assert out["errors"] == []
    p = out["probe"]
    assert p["shown"] and p["hiddenAria"] == "true"
    assert p["label"] == "Growth: show pages up to this month"
    assert p["bars"] == 30 and p["max"] == 29 and p["lit"] == 30  # a bar per month
    assert 0 < p["tall"] <= 32
    assert p["afterInput"]["lit"] == 4 and p["afterInput"]["growth"]
    assert p["afterClick"] == {"value": 10, "lit": 11}  # a click on a bar moves the handle there
    assert p["year"] == {"bars": 12, "max": 11, "yr": "true", "all": "false", "lit": 12}
    assert p["all"] == {"bars": 30, "yr": "false", "all": "true"}
    assert p["playShown"] and p["start"] == {"text": "Pause", "value": 0}
    assert 12 <= p["mid"]["value"] <= 17 and p["mid"]["growth"]
    assert p["end"]["value"] == p["end"]["max"] and p["end"]["text"] == "Play"
    assert p["again"] == "Pause" and p["paused"] == "Play"  # Pause stops it
    assert p["input"] == "Pause" and p["afterRange"] == "Play"  # input on the range stops it
    assert p["afterEsc"] == "Play"
    assert p["afterMode"] == ["Play", True]  # leaving Growth stops it and hides the scrub


READY_PLAY = r"""
(async () => {
  await __L.ready();
  document.querySelector('#home-brain-tools [data-mode="growth"]').click();
  const root = document.querySelector('.home-brain > .scrub');
  const play = [...root.querySelectorAll('button')].find((x) => x.textContent === 'Play');
  return { play: !!play && play.hidden, shown: !root.hidden };
})()
"""


@pytest.mark.xdist_group("browser")
def test_play_is_absent_under_reduced_motion(tmp_path, page):
    out = _run(tmp_path, page, reduced=True, probe=READY_PLAY)
    assert out["errors"] == []
    assert out["probe"] == {"play": True, "shown": True}


LENS_PROBE = r"""
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  await __L.ready();
  const b = __L.brain(), out = {};
  const TAU = 2 * Math.PI, r3 = (x) => +x.toFixed(3);
  const cam = () => { const c = b.camera; return JSON.stringify([r3(((c.yaw % TAU) + TAU) % TAU), r3(c.pitch), r3(c.dist), r3(c.tx), r3(c.ty), r3(c.tz)]); };
  const buttons = () => [...document.querySelectorAll('#view-home [data-lens]')];
  const note = () => { const e = document.querySelector('#home-brain-tools .lens-bar'); return e.hidden ? null : e.textContent; };
  const settle = async () => { let a = cam(); for (let i = 0; i < 60; i++) { await sleep(150); const c = cam(); if (c === a && !b.busy) return; a = c; } };
  out.rows = [...document.querySelectorAll('#view-home .brow')].map((r) => [r.querySelector('.ttl').textContent, !!r.querySelector('[data-lens]')]);
  const btns = buttons();
  out.n = btns.length;
  const real = btns.find((x) => x.getAttribute('aria-label').includes('in the brain') && x.dataset.lens !== 'ghost-private');
  const id = real.dataset.lens;
  out.label = real.getAttribute('aria-label');
  out.before = real.getAttribute('aria-pressed');
  const cam0 = cam();
  real.click();
  await settle();
  const ids = b.lensIds;
  out.set = { ids, first: ids && ids[0], pressed: real.getAttribute('aria-pressed'), note: note(), camMoved: cam() !== cam0 };
  const other = __L.opts().pages.map((p) => p.id).find((x) => b.shows(x) && !ids.includes(x));
  out.dim = [b.drawn(id).a, b.drawn(ids[ids.length - 1]).a, b.drawn(other).a];
  out.todayNull = b.todayIds;
  // Clear restores the camera
  [...document.querySelectorAll('#home-brain-tools .lens-bar button')].find((x) => x.textContent === 'Clear').click();
  await settle();
  out.clear = { ids: b.lensIds, note: note(), pressed: real.getAttribute('aria-pressed'), cam: cam() === cam0,
    dim: b.drawn(other).a === b.drawn(id).a || b.drawn(other).a };
  // the same row again
  real.click(); real.click();
  out.twice = [b.lensIds, real.getAttribute('aria-pressed')];
  // Esc
  real.click();
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
  out.esc = [b.lensIds, note()];
  // Today ends it
  real.click();
  document.querySelector('#home-brain-tools .today-group .tbtn').click();
  await sleep(300);
  out.today = { lens: b.lensIds, note: note(), today: !!b.todayIds, pressed: real.getAttribute('aria-pressed') };
  document.querySelector('#home-brain-tools .today-group .tbtn').click();
  await settle();
  // another row moves the focus
  const real2 = btns.find((x) => x !== real && x.dataset.lens !== id && x.dataset.lens !== 'ghost-private');
  real.click();
  if (real2) { real2.click(); out.moved = [b.lensIds && b.lensIds[0] === real2.dataset.lens, real.getAttribute('aria-pressed'), real2.getAttribute('aria-pressed')]; }
  out.empty = [b.lens(['no-such-page']), !!b.lensIds && b.lensIds[0] === (real2 || real).dataset.lens, b.todayIds];
  real2 ? real2.click() : real.click();
  out.end = [b.lensIds, cam() === cam0 || 'moved'];
  return out;
})()
"""  # noqa: E501  (JavaScript)


@pytest.mark.xdist_group("browser")
def test_focus_lens_on_home_rows(tmp_path, page):
    out = _run(tmp_path, page, probe=LENS_PROBE)
    assert out["errors"] == []
    p = out["probe"]
    rows = dict(p["rows"])
    assert rows["A ghost loop"] is False  # a page the brain does not hold: no button
    assert p["n"] >= 1 and any(has for t, has in p["rows"] if t != "A ghost loop")
    assert p["label"].startswith("Show ") and p["label"].endswith(" in the brain")
    assert p["before"] == "false"
    s = p["set"]
    assert s["first"] and 1 <= len(s["ids"]) <= 40 and s["pressed"] == "true" and s["camMoved"]
    assert s["note"].startswith("Focus: ") and s["note"].endswith("Clear")
    assert f", {len(s['ids']) - 1} linked page" in s["note"]
    full, last, rest = p["dim"]
    assert full == 255 and last == 255 and rest < 255  # the rest dims
    assert p["todayNull"] is None  # the lens is not Today
    assert (p["clear"]["ids"], p["clear"]["note"], p["clear"]["pressed"], p["clear"]["cam"]) == (
        None, None, "false", True)
    assert p["twice"] == [None, "false"]
    assert p["esc"] == [None, None]
    assert p["today"] == {"lens": None, "note": None, "today": True, "pressed": "false"}
    assert p["end"][0] is None
    assert p["empty"] == [0, True, None]  # no visible page: the lens stays as it was


@pytest.fixture
def uneven_page(tmp_path, demo_vault):  # noqa: F811
    """Pages dated 1, 0, 4 and 2 to a month over four months, the rest undated."""
    data = dashboard.build(Vault(demo_vault), now=datetime.now(UTC))
    day = datetime.fromisoformat(data["day"][:10])
    months = [(day.year * 12 + day.month - 1) - k for k in (3, 1, 1, 1, 1, 0, 0)]
    for k, p in enumerate(data["pages"]):
        m = months[k] if k < len(months) else None
        p["date"] = f"{m // 12}-{m % 12 + 1:02d}-15" if m else None
    f = tmp_path / "uneven.html"
    f.write_text(dashboard.render_html(data), encoding="utf-8")
    return f


BARS_PROBE = r"""
(async () => {
  await __L.ready();
  document.querySelector('#home-brain-tools [data-mode="growth"]').click();
  const root = document.querySelector('.home-brain > .scrub');
  return [...root.querySelectorAll('.sb')].map((x) => x.firstChild.offsetHeight);
})()
"""


@pytest.mark.xdist_group("browser")
def test_bars_are_as_tall_as_the_months_count(tmp_path, uneven_page):
    out = _run(tmp_path, uneven_page, probe=BARS_PROBE)
    assert out["errors"] == []
    h = out["probe"]
    assert len(h) == 4 and h[1] == 0  # an empty month draws no bar
    assert h[2] == 32 and abs(h[0] - 8) <= 1 and abs(h[3] - 16) <= 1  # 1, 4 and 2 of a peak of 4
