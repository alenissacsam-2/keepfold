# ruff: noqa: E501, F811
"""U2: the small motion pass. CSS text checks for both style sheets, the app's section switch
(View Transitions, node with the stub DOM of test_app_js), the Copy button's face, and the
dashboard's tab glide (a real browser, skipped without one)."""

import json
import re
import shutil
import subprocess

import pytest
from test_app_js import APP_JS, HARNESS, _run, _sweep_routes
from test_dashboard_js import (
    _run as browser_run,  # imported at collection: it reads the real environment
)
from test_dashboard_js import demo_page, demo_vault  # noqa: F401  (fixtures)

from keepfold import dashboard
from keepfold.app import server

pytestmark = pytest.mark.usefixtures("home")

APP_CSS = (server.ASSETS / "app.css").read_text(encoding="utf-8")
DASH_CSS = (dashboard.ASSETS / "dashboard.css").read_text(encoding="utf-8")
TOKENS = ("--dur-1: 120ms; --dur-2: 200ms; --ease-out: cubic-bezier(.2,.7,.2,1); "
          "--ease-in: cubic-bezier(.4,0,1,1)")


def _block(css: str, head: str) -> str:
    """The text inside the braces of the last `head {` (a media query, say)."""
    start = css.rindex(head) + len(head)
    depth, i = 1, css.index("{", start) + 1
    for j in range(i, len(css)):
        depth += {"{": 1, "}": -1}.get(css[j], 0)
        if not depth:
            return css[i:j]
    raise AssertionError("unbalanced braces")


@pytest.mark.parametrize("css", [APP_CSS, DASH_CSS], ids=["app", "dashboard"])
def test_both_style_sheets_share_the_motion_tokens_and_reduce_them(css):
    assert TOKENS in re.sub(r"\s+", " ", css)
    reduced = _block(css, "@media (prefers-reduced-motion: reduce)")
    assert "transition: none !important" in reduced and "animation: none !important" in reduced
    assert re.search(r"button:active:not\(:disabled\)[^{]*\{ transform: none; \}", reduced)


@pytest.mark.parametrize("css", [APP_CSS, DASH_CSS], ids=["app", "dashboard"])
def test_a_press_scales_to_97_and_never_on_a_disabled_button(css):
    rules = re.findall(r"([^{}]*:active[^{}]*)\{([^}]*)\}", css)
    scaling = [sel for sel, body in rules if "scale(" in body]
    assert len(scaling) == 1 and "scale(.97)" in dict(rules)[scaling[0]]
    assert "button:active:not(:disabled)" in scaling[0]  # a disabled button has no :active rule
    assert not re.search(r"button:disabled[^{]*\{[^}]*(scale|transform)", css)


def test_app_only_the_current_sidebar_item_carries_the_view_transition_name():
    names = re.findall(r"([^{}]*)\{[^}]*view-transition-name", APP_CSS)
    assert len(names) == 1 and '.side button[aria-current="page"]' in names[0]
    # the page content swaps as today: the root's own cross-fade is off, only the highlight glides
    assert re.search(r"::view-transition-old\(root\), ::view-transition-new\(root\) \{ animation: none;",
                     APP_CSS)
    reduced = _block(APP_CSS, "@media (prefers-reduced-motion: reduce)")
    assert "::view-transition-group(*)" in reduced and "animation: none !important" in reduced


def test_app_toast_arrives_with_a_slide_and_leaves_with_a_fade_only():
    base = re.search(r"\n\.toast \{(.*?)\n\}", APP_CSS, re.S)[1]
    assert "transition: opacity var(--dur-1) var(--ease-in)" in base and "transform" not in base.split(
        "transition:")[1].split(";")[0]  # leaving: opacity only
    assert re.search(r"@keyframes toast-in \{ from \{ opacity: 0; transform: translate\(-50%, 6px\)", APP_CSS)


def test_dashboard_toast_is_css_alone_with_starting_style_and_discrete_display():
    assert "@starting-style { .toast { opacity: 0; transform: translateX(-50%) translateY(6px); } }" in DASH_CSS
    assert re.search(r"\.toast\[hidden\] \{ opacity: 0; transition: opacity var\(--dur-1\) var\(--ease-in\), "
                     r"display var\(--dur-1\) allow-discrete", DASH_CSS)


@pytest.mark.parametrize(("css", "sel"), [(APP_CSS, r"\.setup-bar > span"), (DASH_CSS, r"\.bar > span"),
                                          (DASH_CSS, r"\.tbar \.track span")])
def test_a_progress_fill_moves_with_scalex(css, sel):
    body = re.search(sel + r" \{(.*?)\}", css, re.S)[1]
    assert "scaleX(var(--f, 0))" in body and "transition: transform var(--dur-2) var(--ease-out)" in body
    assert "width: 100%" in body


def test_dashboard_highlight_is_a_pseudo_element_the_glide_slides():
    assert re.search(r":is\(\.seg, \.viewswitch, \.presets, \.scrub-span\) button\[aria-pressed=\"true\"\]::before",
                     DASH_CSS)
    assert '.tabs button[aria-selected="true"]::after' in DASH_CSS
    js = (dashboard.ASSETS / "dashboard.js").read_text(encoding="utf-8")
    assert "if (REDUCED.matches) return;" in js and "pseudoElement:" in js


# the section switch: the harness of test_app_js, with matchMedia and startViewTransition set here
GLIDE_PROBE = HARNESS[:HARNESS.index("const out = {};")] + r"""
const out = { errors, vt: 0, boot: null };
globalThis.matchMedia = () => ({ matches: !!DATA.reduced, addEventListener() {} });
if (!DATA.noVT) document.startViewTransition = (fn) => { out.vt++; fn(); return { ready: Promise.resolve() }; };
(async () => {
  try {
    vm.runInThisContext(fs.readFileSync(appPath, 'utf8'), { filename: appPath });
    await tick();
    out.boot = out.vt;  // the first show is never animated
    const lit = () => nav.querySelectorAll('button').filter((b) => b.getAttribute('aria-current') === 'page').map((b) => b.dataset.s);
    const click = async (s) => { nav.querySelectorAll('button').find((b) => b.dataset.s === s).fire('click'); await tick(); };
    await click('vault');
    out.first = [out.vt, lit(), $('s-vault').hidden, $('s-connect').hidden];
    await click('vault');  // the same item: nothing moves
    out.same = out.vt;
    await click('connect');
    out.second = [out.vt, lit(), $('s-vault').hidden, $('s-connect').hidden];
  } catch (e) { errors.push(String(e && e.stack || e)); }
  process.stdout.write(JSON.stringify(out) + '\n', () => process.exit(0));  // a pipe write is async on POSIX: exit once flushed
})();
"""  # noqa: E501  (JavaScript)


@pytest.mark.parametrize(("flags", "glides"), [({}, True), ({"reduced": True}, False),
                                                ({"noVT": True}, False)],
                         ids=["glide", "reduced-motion", "no-api"])
def test_app_section_switch_glides_only_when_allowed_and_always_switches(
        root_committed, tmp_path, monkeypatch, flags, glides):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = _run(node, tmp_path, "glide", GLIDE_PROBE,
               {"routes": _sweep_routes(root_committed, monkeypatch), "stream": [], **flags})
    assert out["errors"] == [], out["errors"]
    assert out["boot"] == 0
    # one current item after each switch, and the page itself changed
    assert out["first"][1:] == [["vault"], False, True]
    assert out["second"][1:] == [["connect"], True, False]
    assert (out["first"][0], out["same"], out["second"][0]) == ((1, 1, 2) if glides else (0, 0, 0))


COPY_SCRIPT = r"""
const written = [], live = [], timers = [];
globalThis.setTimeout = (f, ms) => { timers.push([f, ms]); return timers.length; };
globalThis.clearTimeout = () => {};
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { clipboard: {
  writeText: async (t) => { written.push(t); } } } });
const icon = (name) => ({ name, classList: { list: [], add(c) { this.list.push(c); } } });
const btn = (text, onclick, cls) => ({ onclick, cls, kids: [], replaceChildren(...k) { this.kids = k; } });
const announce = (m) => live.push(m), toast = { error() {} };
const face = (b) => b.kids.map((k) => (typeof k === 'string' ? k : k.name + (k.classList.list.length ? '.' + k.classList.list : '')));
"""


def test_app_copy_button_draws_a_check_says_copied_and_returns_after_1500_ms(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    js = APP_JS.read_text(encoding="utf-8")
    fn = re.search(r"\n  function copyBtn\(.*?\n  \}\n", js, re.S)[0]
    script = tmp_path / "copy.cjs"
    script.write_text(COPY_SCRIPT + fn + r"""
(async () => {
  const b = copyBtn(() => 'hello', 'failed', 'btn', true);
  const before = face(b);
  await b.onclick();
  await new Promise((r) => setImmediate(r));
  const during = face(b);
  const [again, ms] = timers[timers.length - 1];
  again();
  console.log(JSON.stringify({ before, during, live, written, ms, after: face(b) }));
})();
""", encoding="utf-8")
    r = subprocess.run([node, str(script)], capture_output=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {
        "before": ["copy", "Copy"], "during": ["check.draw", "Copied"], "live": ["Copied"],
        "written": ["hello"], "ms": 1500, "after": ["copy", "Copy"]}


TABS_PROBE = r"""
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  await __L.ready();
  const calls = [], real = Element.prototype.animate;
  Element.prototype.animate = function (k, o) {
    if (o && o.pseudoElement && /^tab-/.test(this.id)) calls.push([this.id, o.pseudoElement, o.duration, k[1].transform]);
    return real.call(this, k, o);
  };
  const tabs = () => [...document.getElementById('tabs').children].map((t) => t.getAttribute('aria-selected'));
  document.getElementById('tab-brain').click();
  await sleep(80);
  const brain = [tabs(), calls.slice()];
  document.getElementById('tab-brain').click();  // the same tab again: nothing moves
  await sleep(80);
  return { brain, again: calls.length };
})()
"""


@pytest.mark.slow
@pytest.mark.xdist_group("browser")
def test_dashboard_tab_underline_glides_from_the_old_tab_and_not_under_reduced_motion(
        tmp_path, demo_page):
    out = browser_run(tmp_path, demo_page, probe=TABS_PROBE)
    assert out["errors"] == []
    selected, calls = out["probe"]["brain"]
    assert selected.count("true") == 1 and selected.index("true") == 1
    assert calls == [["tab-brain", "::after", 200, "none"]] and out["probe"]["again"] == 1


@pytest.mark.slow
@pytest.mark.xdist_group("browser")
def test_dashboard_tab_underline_does_not_glide_under_reduced_motion(tmp_path, demo_page):
    out = browser_run(tmp_path, demo_page, probe=TABS_PROBE, reduced=True)
    assert out["errors"] == []
    assert out["probe"]["brain"][1] == [] and out["probe"]["again"] == 0
