"""UX-E: Home follows what the vault is mostly about (dashboard.signals: study, build,
research), Pin layout (POST /api/home/layout, config.yaml app.home_layout, baked into a
snapshot), and a new vault's Get started board (dashboard.get_started, POST /api/home/fact,
app.first_run_hidden). Made-up vaults only; the config is the test's temp home."""

import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_app import _serve, call
from test_dashboard_js import _run
from test_home import FIXTURES, _note

from keepfold import dashboard, localllm, taint
from keepfold.app import server, status, targets
from keepfold.cli import ME_PAGES
from keepfold.vault import Vault

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
GS = ["Tell Keepfold one thing about you", "Connect an AI app", "Bring in what you already have"]


def _row(i, kind, *, source=None, body="", links=(), day="2026-11-10"):
    return {"id": i, "kind": kind, "source": source, "body": body, "links": list(links),
            "date": day, "updated": day}


def test_signals_count_each_kind_and_the_last_30_days_double():
    rows = [_row("c", "concept"), _row("p-old", "project", day="2025-01-01"), _row("p", "project"),
            _row("chat", "resource", source="Claude Code", day="2025-01-01"),
            _row("gpt", "resource", source="ChatGPT"),  # an AI chat: no signal
            _row("cal", "resource", body="Source: Calendar · Date: 2026-11-01\n"),  # nor this
            _row("paper", "resource"),  # saved by hand
            _row("z", "resource",
                 body="# Z\n\nTimeline: [[2026-11-01]] · Source: Zotero · Date: 2026-11-01\n"),
            _row("repo", "resource", body="Source: Git · Date: 2026-11-01\n"),
            _row("n", "note", links=["paper"]), _row("n2", "note", links=["gpt"]),
            _row("me", "me"), _row("2026-11-10-ambient-abcd1234", "session")]
    events = [{"kind": "grade", "day": "2026-11-12"}, {"kind": "taught", "day": "2025-01-01"},
              {"kind": "log", "day": "2026-11-12", "payload": {"kind": "self_report"}},
              {"kind": "log", "day": "2026-11-12", "payload": {"kind": "note"}},
              {"kind": "open", "day": "2026-11-12", "payload": {"client": "Cursor"}},
              {"kind": "open", "day": "2026-11-12", "payload": {"client": "claude-ai"}},
              {"kind": "open", "day": "2026-11-12", "payload": {"client": 3}},
              {"kind": "open", "day": "2026-11-12", "payload": "x"}, {"kind": "close"}]
    assert dashboard.signals(rows, events, "2026-11-13", True) == {
        "study": 2 + 2 + 1 + 2, "build": 1 + 2 + 1 + 2 + 2, "research": 2 + 2 + 2}
    assert dashboard.signals(rows, events, "2026-11-13", False)["study"] == 0
    assert dashboard.signals([], [], "2026-11-13", True) == dict.fromkeys(
        dashboard.HOME_GROUPS, 0)


def test_get_started_is_for_a_vault_with_under_10_pages_of_its_own():
    known = {"total": {"live": 0}}
    rows = [_row(f"me-{i}", "me") for i in range(9)] + [_row("2026-11-10-ambient-abcd1234",
                                                              "session")]
    rows += [_row(f"n{i}", "note") for i in range(9)]
    assert dashboard.get_started(rows, known) == {"fact": False, "imported": False}
    imported = _row("c", "resource", body="Source: Notes · Date: 2026-11-01\n")
    assert dashboard.get_started(rows[:-1] + [imported], {"total": {"live": 2}}) == {
        "fact": True, "imported": True}
    assert dashboard.get_started(rows + [_row("n9", "note")], known) is None


@pytest.mark.perf
def test_signals_cost_under_10_ms_on_5k_notes(tmp_path, capsys):
    from test_home import _big

    from keepfold.events import EventLog
    root = tmp_path / "big"
    _big(root)
    v = Vault(root)
    pages = dashboard.build(v, now=NOW)["pages"]
    events = list(EventLog(v).iter_events())
    took = []
    for _ in range(7):
        t = time.perf_counter()
        dashboard.signals(pages, events, "2026-11-13", True)
        dashboard.get_started(pages, {"total": {"live": 0}})
        took.append((time.perf_counter() - t) * 1000)
    with capsys.disabled():
        print(f"\nsignals + get_started on {len(pages)} pages, {len(events)} events: "
              f"{min(took):.1f} ms (best of 7)")
    # ~6 ms on the lead's laptop (a pure fold); a scan of every body would cost 20 ms or more
    assert min(took) < 10


def _vault(tmp_path, name: str) -> Path:
    root = tmp_path / name
    (root / ".keepfold").mkdir(parents=True)
    (root / "catalog.yaml").write_text("schema: 1\ndomains: []\n", encoding="utf-8")
    return root


def _builder(tmp_path) -> Path:
    """Someone who builds: projects, decisions, a loop, coding-app chats, two saved links; no
    concept, track or grade. 12 pages of its own, so no Get started."""
    root = _vault(tmp_path, "builder")
    for i in range(4):
        _note(root, "projects", f"proj-{i}", {"kind": "project", "title": f"Project {i}",
                                              "status": "active"})
    for i in range(3):
        _note(root, "decisions", f"dec-{i}", {"kind": "decision", "title": f"Use tool {i}",
                                              "status": "accepted", "date": f"2026-11-0{i + 1}"})
    _note(root, "loops", "loop-0", {"kind": "loop", "title": "Ship the parser",
                                    "loop_type": "commitment", "status": "open"})
    for i in range(2):
        _note(root, "resources", f"code-chat-{i}", {"kind": "resource", "title": f"Chat {i}"},
              f"# Chat {i}\n\nTimeline: [[2026-11-0{i + 1}]] · Source: Claude Code · "
              f"Date: 2026-11-0{i + 1}\n")
    for i in range(2):
        _note(root, "resources", f"link-{i}", {"kind": "resource", "title": f"Saved link {i}",
                                               "summary": f"A post worth keeping {i}"})
    return root


def _researcher(tmp_path) -> Path:
    root = _vault(tmp_path, "researcher")
    for i in range(4):
        _note(root, "resources", f"paper-{i}", {"kind": "resource", "title": f"Paper {i}"},
              f"Source: Zotero · Date: 2026-11-0{i + 1}\n\nJournal article.\n")
    _note(root, "notes", "reading", {"kind": "note", "title": "Reading notes"},
          "On [[paper-0]].\n")
    _note(root, "projects", "thesis", {"kind": "project", "title": "Thesis"})
    return root


def test_a_builder_ranks_build_first_and_never_studies(tmp_path):
    d = dashboard.build_data(Vault(_builder(tmp_path)), now=NOW)
    s = d["signals"]
    assert d["studies"] is False and s["study"] == 0 and s["build"] > s["research"] > 0, s
    assert d["get_started"] is None


def test_a_researcher_ranks_research_first(tmp_path):
    s = dashboard.build_data(Vault(_researcher(tmp_path)), now=NOW)["signals"]
    assert s["study"] == 0 and s["research"] > s["build"] > 0, s


def test_a_student_still_studies(tmp_path):
    root = tmp_path / "mini"
    shutil.copytree(FIXTURES / "vault_mini", root)
    d = dashboard.build_data(Vault(root), now=NOW)
    assert d["studies"] is True and d["signals"]["study"] > 0


def test_an_empty_vault_scores_nothing_and_gets_started(tmp_path):
    d = dashboard.build_data(Vault(_vault(tmp_path, "empty")), now=NOW)
    assert d["signals"] == {"study": 0, "build": 0, "research": 0}
    assert d["get_started"] == {"fact": False, "imported": False}


@pytest.fixture
def builder_app(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    srv, a = _serve(_builder(tmp_path))
    yield a
    srv.shutdown()
    srv.server_close()


def test_pin_layout_is_kept_in_the_config(builder_app):
    app = builder_app
    taint.write_config({"app": {"notify": {"exams": False}}})
    for token in (False, "wrong"):
        assert call(app, "POST", "/api/home/layout", {"order": ["build"]},
                    token=token)[0] in (401, 403)
    for bad in ({}, {"order": ["build", "fun"]}, {"order": ["build", "build"]},
                {"order": "build"}, {"order": []}, {"first_run_hidden": False}, {"pin": True}):
        code, out, _, _ = call(app, "POST", "/api/home/layout", bad)
        assert code == 400 and out["error"], bad
    assert taint.config()["app"] == {"notify": {"exams": False}}
    code, out, _, _ = call(app, "POST", "/api/home/layout",
                           {"order": ["research", "build", "study"]})
    assert code == 200 and out["home_layout"] == ["research", "build", "study"]
    assert taint.config()["app"] == {"notify": {"exams": False},
                                     "home_layout": ["research", "build", "study"]}
    home = call(app, "GET", "/api/home")[1]
    assert home["home_layout"] == ["research", "build", "study"]
    assert home["first_run_hidden"] is False and "connected" not in home  # not a new vault
    code, out, _, _ = call(app, "POST", "/api/home/layout", {"order": None})
    assert code == 200 and out["home_layout"] is None
    assert taint.config()["app"] == {"notify": {"exams": False}}


def _new_vault(tmp_path) -> Path:
    root = _vault(tmp_path, "new")
    (root / "me").mkdir()
    (root / "me" / "profile.md").write_text(ME_PAGES["me/profile.md"], encoding="utf-8")
    return root


def test_get_started_ticks_from_the_vault_and_hides_for_good(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    root = _new_vault(tmp_path)
    srv, app = _serve(root)
    try:
        home = call(app, "GET", "/api/home")[1]
        assert home["get_started"] == {"fact": False, "imported": False}
        assert home["connected"] is False and home["first_run_hidden"] is False
        for bad in ({}, {"text": "  "}, {"text": "x" * 301}, {"text": 3}):
            code, out, _, _ = call(app, "POST", "/api/home/fact", bad)
            assert code == 400 and out["error"], bad
        assert call(app, "POST", "/api/home/fact", {"text": "x"}, token=False)[0] in (401, 403)
        code, out, _, _ = call(app, "POST", "/api/home/fact",
                               {"text": "I work night shifts as a nurse"})
        assert code == 200 and out["saved"], out
        assert "I work night shifts as a nurse" in (root / "me" / "profile.md").read_text(
            encoding="utf-8")
        _note(root, "resources", "chat-1", {"kind": "resource", "title": "A chat"},
              "Timeline: [[2026-11-01]] · Source: ChatGPT · Date: 2026-11-01\n")
        monkeypatch.setattr(targets, "describe", lambda base: [{"connected": True}])
        status.forget()
        home = call(app, "GET", "/api/home")[1]
        assert home["get_started"] == {"fact": True, "imported": True}
        assert home["connected"] is True
        assert call(app, "POST", "/api/home/layout", {"first_run_hidden": True})[0] == 200
        assert taint.config()["app"] == {"first_run_hidden": True}
        home = call(app, "GET", "/api/home")[1]
        assert home["first_run_hidden"] is True and "connected" not in home
    finally:
        srv.shutdown()
        srv.server_close()
    for i in range(10):
        _note(root, "notes", f"n{i}", {"kind": "note", "title": f"Note {i}"})
    assert dashboard.build_data(Vault(root), now=NOW)["get_started"] is None


# ---- in a real headless browser (skipped without node and Chrome or Edge) ----

SHOT = r"""(async () => {
  if (document.documentElement.dataset.theme !== 'THEME') document.getElementById('theme').click();
  await new Promise((r) => setTimeout(r, 150));
  SCROLL;
  await new Promise((r) => setTimeout(r, 150));
})()"""


def _shots(tmp_path, name: str, width: int, scroll: str) -> list:
    return [[SHOT.replace("THEME", t).replace("SCROLL", scroll),
             str(tmp_path / f"ux-e-{name}-{width}-{t}.png")] for t in ("night", "day")]


def _quiet(monkeypatch):
    """Nothing real runs or answers: no LLM runner, no AI app found, no browser opened."""
    def no_runner(*a, **k):
        raise OSError("no runner in tests")
    monkeypatch.setattr(localllm, "_request", no_runner)
    monkeypatch.setattr(localllm, "_lms_exe", lambda cfg: None)
    monkeypatch.setattr(server, "_open", lambda url: None)
    monkeypatch.setattr(targets, "_which", lambda c: None)
    monkeypatch.setattr(targets, "_run", lambda args, timeout=300: (False, "faked"))


HOME_PROBE = r"""(async () => {
  const $ = (id) => document.getElementById(id), sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const link = () => document.querySelector('.home-pin .linkbtn');
  for (let i = 0; i < 200 && !link(); i++) await sleep(50);
  const rowB = () => [...$('home-row-b').querySelectorAll('h2')].map((h) => h.textContent);
  const out = { heads: [...$('view-home').querySelectorAll('.board h2')].map((h) => h.textContent),
    rowB: rowB(), pin: link().textContent, last: $('view-home').lastElementChild === link().parentElement,
    gs: !!document.querySelector('.get-started'), study: !!$('tab-study'),
    overflow: document.documentElement.scrollWidth > innerWidth };
  link().click();
  for (let i = 0; i < 200 && link().textContent !== 'Unpin layout'; i++) await sleep(50);
  out.pinned = [link().textContent, document.activeElement === link(), $('toast').textContent];
  out.after = rowB();
  return out;
})()"""  # noqa: E501  (JavaScript)


@pytest.mark.slow
@pytest.mark.parametrize("width", [1280, 375])
@pytest.mark.xdist_group("browser")
def test_a_builders_home_puts_build_first_and_pins(tmp_path, monkeypatch, width):
    """Open loops (build) before Back on your mind (research), no Exams board (study is 0), no
    sideways scroll; Pin layout saves the order and turns into Unpin layout."""
    _quiet(monkeypatch)
    srv, app = _serve(_builder(tmp_path))
    try:
        out = _run(tmp_path, None, url=f"http://127.0.0.1:{app.port}/?t={app.token}",
                   probe=HOME_PROBE, narrow=width,
                   shots=_shots(tmp_path, "home", width, "document.querySelector("
                                "'.home-pin').scrollIntoView({ block: 'end' })"))
    finally:
        srv.shutdown()
        srv.server_close()
    assert out["errors"] == []
    p = out["probe"]
    assert "Exams and deadlines" not in p["heads"] and not p["study"], p
    assert p["rowB"] == ["Open loops", "Back on your mind"], p
    assert p["pin"] == "Pin layout" and p["last"] and not p["gs"] and not p["overflow"], p
    assert p["pinned"] == ["Unpin layout", True, "Home keeps this layout."], p
    assert p["after"] == p["rowB"]
    assert taint.config()["app"]["home_layout"] == ["build", "research", "study"]
    for t in ("night", "day"):
        assert (tmp_path / f"ux-e-home-{width}-{t}.png").stat().st_size > 10_000


SNAP_PROBE = r"""(async () => ({
  heads: [...document.querySelectorAll('#home-row-a h2')].map((h) => h.textContent),
  pin: !!document.querySelector('.home-pin'), gs: !!document.querySelector('.get-started') }))()"""


@pytest.mark.slow
@pytest.mark.xdist_group("browser")
def test_a_snapshot_bakes_the_pinned_order_and_has_no_pin_link(tmp_path):
    v = Vault(_researcher(tmp_path))
    assert '"home_layout":null' in dashboard.snapshot(v, now=NOW)
    taint.write_config({"app": {"home_layout": ["build", "research", "study"]}})
    html = dashboard.snapshot(v, now=NOW)
    assert '"home_layout":["build","research","study"]' in html
    page = tmp_path / "pinned.html"
    page.write_text(html, encoding="utf-8")
    out = _run(tmp_path, page, probe=SNAP_PROBE)
    assert out["errors"] == []
    # pinned: build before research, though research scores higher; no study, no link
    assert out["probe"] == {"heads": ["Open loops", "Back on your mind"], "pin": False,
                            "gs": False}


START_PROBE = r"""(async () => {
  const $ = (id) => document.getElementById(id), sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  for (let i = 0; i < 200 && !document.querySelector('.get-started'); i++) await sleep(50);
  const b = document.querySelector('.get-started');
  const read = () => ({ note: b.querySelector('.band-note').textContent,
    codes: [...b.querySelectorAll('.brow .code')].map((c) => [c.textContent, c.getAttribute('aria-label')]) });
  const shown = (x) => { const r = x.getBoundingClientRect(); return r.width > 0 && r.left >= 0 && r.right <= innerWidth; };
  const out = { first: b === $('view-home').firstElementChild, title: b.querySelector('h2').textContent, ...read(),
    steps: [...b.querySelectorAll('.brow .ttl')].map((t) => t.textContent),
    links: [...b.querySelectorAll('a.btn')].map((a) => [a.textContent, a.getAttribute('href')]),
    hide: [...b.querySelectorAll('.foot .linkbtn')].map((x) => [x.textContent, x.getAttribute('aria-label')]),
    input: [$('gs-fact').getAttribute('aria-label'), $('gs-fact').maxLength],
    startHere: !!document.querySelector('.first-run'),
    inside: [...b.querySelectorAll('input, button, a')].every(shown),
    overflow: document.documentElement.scrollWidth > innerWidth };
  $('gs-fact').value = 'I work night shifts as a nurse';
  $('gs-fact').closest('form').requestSubmit();
  for (let i = 0; i < 200 && read().codes[0][0] !== '✓'; i++) await sleep(50);
  out.after = read();
  out.said = $('announce').textContent;
  out.toast = $('toast').textContent;
  out.cleared = $('gs-fact').value;
  return out;
})()"""  # noqa: E501  (JavaScript)
HIDE = r"""(async () => {
  document.querySelector('.get-started .foot .linkbtn').click();
  for (let i = 0; i < 100 && document.querySelector('.get-started'); i++) await new Promise((r) => setTimeout(r, 50));
})()"""  # noqa: E501  (JavaScript)


@pytest.mark.slow
@pytest.mark.parametrize("width", [1280, 375])
@pytest.mark.xdist_group("browser")
def test_a_new_vault_gets_started_in_three_steps(tmp_path, monkeypatch, width):
    """0 of 3 at first, Get started first on Home and the only first-run board; a line about
    you ticks step 1 (said in the live region); Hide closes it and sets first_run_hidden."""
    _quiet(monkeypatch)
    root = _new_vault(tmp_path)
    srv, app = _serve(root)
    try:
        out = _run(tmp_path, None, url=f"http://127.0.0.1:{app.port}/?t={app.token}",
                   probe=START_PROBE, narrow=width,
                   shots=_shots(tmp_path, "start", width, "window.scrollTo(0, 0)")
                   + [[HIDE, str(tmp_path / "hidden.png")]])
    finally:
        srv.shutdown()
        srv.server_close()
    assert out["errors"] == []
    p = out["probe"]
    assert p["first"] and p["title"] == "Get started" and p["note"] == "0 of 3 done", p
    assert p["codes"] == [["1", "Step 1"], ["2", "Step 2"], ["3", "Step 3"]], p
    assert p["steps"] == GS and p["input"] == ["One thing about you", 300], p
    assert p["links"] == [["Connect", "/manage#connect"], ["Import", "/manage#setup"]], p
    assert p["hide"] == [["Hide", "Hide Get started"]] and not p["startHere"], p
    assert p["inside"] and not p["overflow"], p
    assert p["after"] == {"note": "1 of 3 done", "codes": [["✓", "Done"], ["2", "Step 2"],
                                                           ["3", "Step 3"]]}, p
    assert p["said"] == f"Step 1 of 3 done: {GS[0]}." and p["toast"] == "Saved to your profile."
    assert p["cleared"] == ""
    assert "I work night shifts as a nurse" in (root / "me" / "profile.md").read_text(
        encoding="utf-8")
    assert taint.config()["app"] == {"first_run_hidden": True}
    for t in ("night", "day"):
        assert (tmp_path / f"ux-e-start-{width}-{t}.png").stat().st_size > 10_000


def test_a_changed_body_gets_its_source_label_again():
    """signals keeps each page's `Source:` label until the row's body is a new string."""
    p = {"id": "r", "body": "# R\n\nSource: Zotero · Date: 2026-01-01\n"}
    assert dashboard._label(p) == "Zotero"
    assert dashboard._label(p) == "Zotero"  # the same body: from the cache
    edited = {"id": "r", "body": "# R\n\nSource: Cursor · Date: 2026-01-01\n"}
    assert dashboard._label(edited) == "Cursor"
