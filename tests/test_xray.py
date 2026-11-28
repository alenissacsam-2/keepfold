"""Agent X-ray (v9 plan item 6): the ring of what keepfold_prime and keepfold_context served (ids
and counts, no text, bounded, trimmed ids), the app's view (private pages never by name), and
the owner's Pin and Mute (prime and context obey them; only the app sets them)."""

import json
import shutil
import time
from datetime import UTC, datetime

import anyio
import pytest
from conftest import FakeEmbed, note
from mcp import Client
from test_app import _serve, call
from test_app_js import HARNESS, _run

from keepfold import prime, semantic, xray
from keepfold.mcp_server import Keepfold, build_server, dry_prime
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

PREFS = ("# Preferences\n\n## Coding\n- Python first, type hints.\n\n## Personal\n"
         "- Weekend motor racing fan.\n")


@pytest.fixture
def vault(root, monkeypatch):
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    (root / ".keepfold").mkdir(exist_ok=True)
    note(root, "notes/engines.md", "title: Engines\nsummary: how a motor works\n",
         "# Engines\n\nAutomobile car motor basics.")
    note(root, "notes/pasta.md", "title: Pasta\n", "# Pasta\n\nA recipe from the kitchen.")
    (root / "me/preferences.md").write_text(PREFS, encoding="utf-8")
    return root


def claim_id(root, text):
    ids = prime.claim_ids(Vault(root))
    return next(c for (p, _, t), c in ids.items() if t == text)


def related(ctx):
    return ctx.split("## Related\n", 1)[1].split("\n## ", 1)[0] if "## Related\n" in ctx else ""


def test_ring_records_ids_only_and_stays_bounded(vault, monkeypatch):
    monkeypatch.setattr(xray, "CAP", 3)
    c = Keepfold(Vault(vault))
    c.sessions.client = "claude-ai"
    for n in range(4):
        c.keepfold_prime(f"How does a car motor work? take {n}")
    c.keepfold_context(topic="car motor")
    rows = xray.rows(Vault(vault))
    assert len(rows) == 3 and rows[-1]["tool"] == "keepfold_context"
    first = rows[0]
    assert first["client"] == "claude-ai" and first["chars"] > 0
    assert "engines" in first["served"]["related"]
    # A21: profile lines are served ranked by the prompt, so each is recorded by its claim id
    assert first["served"]["profile"] and all(i.startswith("^c-")
                                              for i in first["served"]["profile"])
    assert claim_id(vault, "Weekend motor racing fan.") in first["served"]["preferences"]
    raw = (vault / xray.RING).read_text("utf-8")
    for text in ("car motor", "take", "motor racing", "Python first", "Engines", "how a motor"):
        assert text not in raw  # no prompt, no page or claim text
    assert set(rows[-1]["served"]) >= {"profile", "related", "claims"}


def test_trimmed_ids_are_recorded(vault, monkeypatch):
    lines = "".join(f"- Preference number {n} about coding style and habits.\n"
                    for n in range(30))
    (vault / "me/preferences.md").write_text(f"# Preferences\n\n## Coding\n{lines}",
                                             encoding="utf-8")
    xr: dict = {}
    parts, _ = prime.core(Vault(vault), "coding style", [], datetime.now(UTC), xr=xr)
    served, cut = xr["served"]["preferences"], xr["trimmed"]["preferences"]
    assert served and cut and len(served) + len(cut) == 30
    assert all(c.startswith("^c-") for c in served + cut)
    every = set(prime.claim_ids(Vault(vault)).values())
    assert set(served) | set(cut) <= every


def test_view_never_names_a_private_page(vault):
    v = Vault(vault)
    xray.record(v, "keepfold_prime", "claude-code", {"related": ["secret-diary", "engines"]},
                {"related": ["secret-diary"]}, 120)
    xray.set_flag(v, "mute", "secret-diary", True)
    view = xray.view(v)
    dumped = json.dumps(view)
    assert "secret-diary" not in dumped and "Private concept" not in dumped
    (g,) = view["clients"]
    assert g["client"] == "Claude Code"
    (served, *_), = [c["served"] for c in g["calls"]]
    assert [x["title"] for x in served["items"]] == ["a private page", "Engines"]
    (m,) = view["muted"]
    assert m["title"] == "a private page" and "id" not in m
    xray.set_flag(v, "mute", xray.resolve_token(v, m["token"]), False)  # undo by its token
    assert xray.settings(v)["mute"] == set()


def test_pin_serves_a_page_and_a_claim_in_prime(vault):
    v = Vault(vault)
    prompt = "How does a car motor work?"
    ctx = Keepfold(v).keepfold_prime(prompt)["context"]
    assert "[[pasta]]" not in ctx
    python = claim_id(vault, "Python first, type hints.")
    xray.set_flag(v, "pin", "pasta", True)
    xray.set_flag(v, "pin", python, True)
    ctx = Keepfold(v).keepfold_prime(prompt)["context"]
    assert related(ctx).index("[[pasta]]") < related(ctx).index("[[engines]]")  # pins lead
    prefs = ctx.split("## Preferences\n", 1)[1]
    assert prefs.index("Python first") < prefs.index("motor racing")
    assert "pasta" in xray.rows(v)[-1]["served"]["related"]


def test_mute_keeps_a_page_and_a_claim_out_of_prime_and_context(vault):
    v = Vault(vault)
    racing = claim_id(vault, "Weekend motor racing fan.")
    xray.set_flag(v, "mute", "engines", True)
    xray.set_flag(v, "mute", racing, True)
    c = Keepfold(v)
    ctx = c.keepfold_prime("How does a car motor work?")["context"]
    assert "engines" not in ctx and "motor racing" not in ctx and "Python first" in ctx
    ctx = c.keepfold_context(topic="car motor")["context"]
    assert "engines" not in ctx and "motor racing" not in ctx and "Python first" in ctx
    assert all("engines" not in json.dumps(r["served"]) for r in xray.rows(v))
    xray.set_flag(v, "pin", "engines", True)  # pinning unmutes
    assert xray.settings(v) == {"pin": {"engines"}, "mute": {racing}}


def test_pin_and_mute_cannot_be_set_over_mcp(vault):
    server = build_server(Vault(vault), "owner")

    async def main():
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            for t in tools:  # no tool takes a pin or mute argument
                props = set(t.input_schema.get("properties", {}))
                assert not props & {"pin", "mute", "pinned", "muted", "xray"}, t.name
            r = await client.call_tool("keepfold_patch", {
                "id": ".keepfold/xray", "op": "append_to_section", "target": "x", "text": "pin"})
            assert r.is_error or "error" in (r.structured_content or {})

    anyio.run(main)
    assert not (vault / xray.SETTINGS).exists()
    with pytest.raises(ValueError):
        xray.set_flag(Vault(vault), "pin", "../me/profile", True)


def test_endpoints_are_gated_and_pin_round_trips(vault):
    srv, app = _serve(vault)
    try:
        assert call(app, "GET", "/api/xray", token=False)[0] == 401
        assert call(app, "POST", "/api/xray/pin", {"id": "pasta"}, token=False)[0] == 401
        assert call(app, "POST", "/api/xray/mute", {"id": "pasta"}, token="nope")[0] == 401
        assert not (vault / xray.SETTINGS).exists()
        status, js, *_ = call(app, "POST", "/api/xray/pin", {"id": "pasta"})
        assert status == 200 and [x["id"] for x in js["pinned"]] == ["pasta"]
        assert call(app, "POST", "/api/xray/pin", {"id": "Bad Id!"})[0] == 400
        status, js, *_ = call(app, "GET", "/api/xray")
        assert status == 200 and js["pinned"][0]["title"] == "Pasta" and js["clients"] == []
    finally:
        srv.shutdown()
        srv.server_close()


XRAY_JS = HARNESS[:HARNESS.index("const out = {};")] + r"""
const out = { errors };
(async () => {
  try {
    vm.runInThisContext(fs.readFileSync(appPath, 'utf8'), { filename: appPath });
    await tick();
    nav.querySelectorAll('button').find((b) => b.dataset.s === 'xray').fire('click');
    await tick();
    out.boards = $('xray').querySelectorAll('h2').map((x) => x.textContent);
    out.rows = $('xray').querySelectorAll('.brow').map((r) => [r.querySelector('.ttl').textContent,
      r.querySelectorAll('button').map((b) => b.textContent)]);
    const boards = () => $('xray').querySelectorAll('.board').slice(1)
      .filter((b) => !b.className.split(' ').includes('xray-compare'));  // R27b: its own test
    out.chips = boards().map((b) => b.querySelectorAll('.chip').map((c) => [c.textContent,
      c.getAttribute('aria-pressed'), c.getAttribute('aria-disabled'), c.getAttribute('title')]));
    out.help = boards().map((b) => b.querySelectorAll('.help').map((x) => x.textContent));
    out.labels = $('xray').querySelectorAll('label').map((x) => x.textContent);
    $('xray').querySelectorAll('button').find((b) => b.textContent === 'Mute').fire('click');
    await tick();
    boards()[0].querySelectorAll('.chip').find((c) => c.textContent === 'coding').fire('click');  // locked
    await tick();
    boards()[1].querySelectorAll('.chip').find((c) => c.textContent === 'coding').fire('click');
    await tick();
    out.toast = $('toast-msg').textContent;
    const input = boards()[1].querySelector('input');
    const show = () => boards()[1].querySelectorAll('button').find((b) => b.textContent === 'Show');
    out.emptyDisabled = show().disabled;
    input.value = 'car motor';
    input.fire('input');
    out.typedDisabled = show().disabled;
    show().fire('click');
    await tick();
    const dry = () => [boards()[1].querySelectorAll('p').filter((p) => p.className !== 'help').map((p) => p.textContent),
      boards()[1].querySelectorAll('.brow').map((r) => [r.querySelector('.ttl').textContent,
        r.querySelectorAll('button').map((b) => b.textContent)])];
    out.dry = dry();
    boards()[1].querySelectorAll('button').find((b) => b.textContent === 'Pin').fire('click');
    await tick();
    out.dryAfterPin = dry();
    out.posted = fetched.filter((f) => f.method === 'POST').map((f) => [f.path, f.body]);
  } catch (e) { errors.push(String(e && e.stack || e)); }
  out.sinks = sinks;
  process.stdout.write(JSON.stringify(out) + '\n', () => process.exit(0));  // a pipe write is async on POSIX: exit once flushed
})();
"""  # noqa: E501  (JavaScript)


def test_the_xray_page(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    item = {"id": "engines", "kind": "page", "title": "<b>Engines</b>", "pinned": False,
            "muted": False}
    private = {"kind": "page", "title": "a private page", "token": "t1", "pinned": False,
               "muted": True}
    call_ = {"ts": "2026-11-20T10:00:00Z", "tool": "keepfold_prime", "chars": 900,
             "served": [{"block": "related", "items": [item]}],
             "trimmed": [{"block": "profile", "items": [
                 {"id": "me/profile", "kind": "me", "title": "Profile"}]}]}
    profiles = {"profiles": [{"match": "claude", "scopes": ["all"]}], "unknown": ["core"]}
    view = {"pinned": [], "muted": [private], "profiles": profiles, "profiles_max": 30,
            "all_scopes": ["all", "core", "coding"],
            "clients": [{"client": "Claude Code", "name": "claude-code", "total": 1,
                         "scopes": ["all"], "rule": "claude", "shared": ["Claude Desktop"],
                         "often": [{**item, "count": 3}], "calls": [call_]},
                        {"client": "mystery-app", "name": "mystery-app", "total": 1,
                         "scopes": ["core"], "rule": None, "shared": [], "often": [],
                         "calls": []}]}
    pinned = {**view, "pinned": [{**item, "pinned": True}]}
    dry = {"served": [{"block": "related", "items": [item]}], "trimmed": [], "chars": 420}
    status = {"first_run": False, "rows": [], "vault": "V", "land": "status",
              "update": {"current": "0.1.0", "mode": "installer"}}
    out = _run(node, tmp_path, "xray", XRAY_JS, {
        "routes": {"/api/xray": view, "/api/xray/mute": view, "/api/xray/pin": pinned,
                   "/api/xray/dryrun": dry, "/api/profiles": {"profiles": profiles},
                   "/api/status": status, "/api/review": {"count": 0}}, "stream": []})
    assert out["errors"] == [] and out["sinks"] == [], out["errors"]
    assert out["boards"] == ["Pinned and muted", "Compare two apps", "Claude Code", "mystery-app"]
    assert out["rows"] == [["a private page", ["Unmute"]],
                           ["<b>Engines</b>", ["Pin", "Mute"]],  # served most often
                           ["<b>Engines</b>", ["Pin", "Mute"]],  # text, never markup
                           ["Profile", []]]
    locked = "Turn off Everything first"
    assert out["chips"] == [
        [["Everything except private", "true", None, None], ["core", "true", "true", locked],
         ["coding", "true", "true", locked]],
        [["Everything except private", "false", None, None], ["core", "true", None, None],
         ["coding", "false", None, None]]]
    assert out["help"] == [
        ["Set by the rule for apps named “claude”, shared with Claude Desktop."],
        ["No rule names this app, so it gets what “Any other app” gets. Changing a chip adds "
         "a rule for “mystery-app”."]]
    assert out["labels"] == ["Prompt", "First app", "Second app",
                             "What would Claude Code get for this prompt?",
                             "Paste a reply from Claude Code that surprised you",
                             "What would mystery-app get for this prompt?",
                             "Paste a reply from mystery-app that surprised you"]
    assert out["toast"] == "Saved. mystery-app reads: core, coding."
    assert (out["emptyDisabled"], out["typedDisabled"]) == (True, False)
    heading = "Dry run · 420 characters · nothing was sent or logged"
    assert out["dry"] == [[heading], [["<b>Engines</b>", ["Pin", "Mute"]]]]
    assert out["dryAfterPin"] == [[heading], [["<b>Engines</b>", ["Unpin", "Mute"]]]]
    assert out["posted"] == [
        ["/api/xray/mute", {"id": "engines", "on": True}],
        # the unmatched app gets its own rule at the end; "Any other app" stays as it was
        ["/api/profiles", {"profiles": [{"match": "claude", "scopes": ["all"]},
                                        {"match": "mystery-app", "scopes": ["core", "coding"]}],
                           "unknown": ["core"]}],
        ["/api/xray/dryrun", {"client": "mystery-app", "prompt": "car motor"}],
        ["/api/xray/pin", {"id": "engines", "on": True}]]


# ---- P2 Agent Aperture: the scope bar, served most often, the dry run ----------------------
PROMPT = "How does a car motor work? I love motor racing and Python."


def _aperture(root):
    note(root, "me/core.md", "id: core\n", "# Core\n\n## Core\n- Call me Sam.\n")
    note(root, "notes/secret-motor.md", "title: Secret motor diary\nprivate: true\n",
         "# Secret motor diary\n\nCar motor racing secrets.")
    note(root, "journal/2026-11-20.md", "id: 2026-11-20\ntitle: Day\n",
         "- Watched car motor racing today.")


def _blocks(d):
    return {k: list(v) for k, v in d.items() if v}


@pytest.mark.parametrize("client", ["claude-ai", "cursor", "mcp"])
def test_dry_run_serves_what_a_real_prime_serves(vault, client):
    _aperture(vault)
    v = Vault(vault)
    c = Keepfold(v)
    c.over_mcp, c.sessions.client = True, client  # as over MCP: the client's profile applies
    ctx = c.keepfold_prime(PROMPT)["context"]
    real = xray.rows(v)[-1]
    dry = dry_prime(v, client, PROMPT)
    assert _blocks(dry["served"]) == real["served"] and real["served"]
    assert _blocks(dry["trimmed"]) == real["trimmed"]
    assert dry["chars"] == real["chars"] == len(ctx)
    ids = {i for b in dry["served"].values() for i in b} | {
        i for b in dry["trimmed"].values() for i in b}
    assert "secret-motor" not in ids  # a private page never goes to an AI app
    core = {cid for (p, _, _), cid in prime.claim_ids(v).items() if p == "core"}
    if client == "mcp":  # an app no rule names: `unknown`, the core only
        assert not set(_blocks(dry["served"])) & {"profile", "preferences", "contract"}
        assert {i for i in ids if i.startswith("^")} <= core and core <= ids
        assert "2026-11-20" not in ids  # journal pages are `personal`
    if client == "cursor":  # coding: the Coding line, never the Personal one
        assert claim_id(vault, "Python first, type hints.") in ids
        assert claim_id(vault, "Weekend motor racing fan.") not in ids


def test_dry_run_writes_nothing(vault, home, monkeypatch):
    from keepfold import index
    _aperture(vault)
    v = Vault(vault)
    c = Keepfold(v)
    c.sessions.client = "claude-ai"
    c.keepfold_prime(PROMPT)  # the caches a real call makes exist now
    c._call("keepfold_log", c.keepfold_log, {"kind": "touch", "payload": {"ids": ["engines"]}})
    c.sessions.close(summary="test")  # an events file
    note(vault, "notes/pasta.md", "title: Pasta\n", "# Pasta\n\nA car motor kitchen recipe.")
    monkeypatch.setattr(index, "SNAPSHOT_MIN", 0)  # any refresh would save the snapshot

    def files():
        return {p: p.read_bytes() for d in (vault, home) for p in d.rglob("*") if p.is_file()}

    before = files()
    assert any("events" in p.parts for p in before) and (vault / semantic.CACHE).is_file()
    for client in ("claude-ai", "cursor", "mcp", "", "unknown"):
        dry_prime(v, client, PROMPT + " //note remember this")
    assert files() == before


def _withheld_pages(root):
    """_aperture, plus a private browsing page and a playbook only `personal` may get."""
    _aperture(root)
    note(root, "browse/browse-private-2026-11-20.md", "id: browse-private-2026-11-20\n"
         "title: Private browsing\nprivate: true\n", "# Private browsing\n\nCar motor racing.")
    note(root, "notes/race-steps.md", "title: Race day steps\nkind: note\ntags: [playbook]\n"
         "scope: personal\nsummary: car motor racing\n", "# Race day steps\n\nCar motor race.")


def _ids(d):
    return {i for part in ("served", "trimmed") for b in d[part].values() for i in b}


def _same(a, b):
    return (_blocks(a["served"]), _blocks(a["trimmed"]), a["chars"]) == (
        _blocks(b["served"]), _blocks(b["trimmed"]), b["chars"])


def test_a_shared_index_serves_each_client_what_a_fresh_one_does(vault):
    """One read-only index across alternating clients (all scopes, core only, coding): each
    run equals a fresh index's and a real keepfold_prime's, and nothing one client may get is
    left in the index for the next."""
    from keepfold.catalog import Catalog
    from keepfold.index import Index
    _withheld_pages(vault)
    v = Vault(vault)
    shared = Index(v, Catalog.load(v), save=False)
    personal = claim_id(vault, "Weekend motor racing fan.")
    try:
        for client in ("claude-ai", "mcp", "cursor", "claude-ai", "cursor", "mcp", "claude-ai"):
            c = Keepfold(v)
            c.over_mcp, c.sessions.client = True, client
            ctx = c.keepfold_prime(PROMPT)["context"]
            real = xray.rows(v)[-1]
            got, fresh = dry_prime(v, client, PROMPT, index=shared), dry_prime(v, client, PROMPT)
            assert _same(got, fresh), client
            assert (_blocks(got["served"]), _blocks(got["trimmed"]), got["chars"]) == (
                real["served"], real["trimmed"], len(ctx)), client
            ids = _ids(got)
            assert not ids & {"secret-motor", "browse-private-2026-11-20"}, client  # private
            if client == "claude-ai":  # every scope: the journal, the playbook, the line
                assert {"2026-11-20", "race-steps", personal} <= ids
            else:  # core only, or coding: none of them, whoever ran before
                assert not ids & {"2026-11-20", "race-steps", personal}, client
            if client == "mcp":
                assert not set(_blocks(got["served"])) & {"profile", "preferences", "related"}
    finally:
        shared.db.close()


def test_a_shared_index_sees_pages_added_edited_and_deleted(vault):
    from keepfold.catalog import Catalog
    from keepfold.index import Index
    v = Vault(vault)
    shared = Index(v, Catalog.load(v), save=False)
    try:
        for client in ("claude-ai", "mcp"):  # its withhold switches between the edits too
            dry_prime(v, client, PROMPT, index=shared)
        assert "pasta" not in _ids(dry_prime(v, "claude-ai", PROMPT, index=shared))
        note(vault, "notes/turbo.md", "title: Turbo\n", "# Turbo\n\nA car motor turbo.")
        note(vault, "notes/pasta.md", "title: Pasta\n", "# Pasta\n\nA car motor kitchen recipe.")
        dry_prime(v, "mcp", PROMPT, index=shared)
        got = dry_prime(v, "claude-ai", PROMPT, index=shared)
        assert "turbo" in _ids(got) and _same(got, dry_prime(v, "claude-ai", PROMPT))
        assert "kitchen recipe" in shared._notes["pasta"]["body"]  # the edit, by its stat
        (vault / "notes/turbo.md").unlink()
        got = dry_prime(v, "claude-ai", PROMPT, index=shared)
        assert "turbo" not in _ids(got) and _same(got, dry_prime(v, "claude-ai", PROMPT))
    finally:
        shared.db.close()


def test_a_shared_index_takes_each_runs_catalog(vault):
    """A concept added to catalog.yaml under a private domain after the shared index was built
    is hidden in the next run, as in a fresh index's (a public one beside it is served)."""
    from keepfold.catalog import Catalog
    from keepfold.index import Index

    def domains(with_concepts):
        out = ""
        for d, private in (("racing", True), ("cars", False)):
            note(vault, f"domains/{d}.md", f"id: {d}\ntitle: {d.title()}\nkind: domain\n"
                 + ("private: true\n" if private else ""), f"# {d.title()}")
            note(vault, f"areas/{d}.motors.md", f"id: {d}.motors\ntitle: Motors\nkind: area\n"
                 f"domain: {d}\n", "# Motors")
            out += (f'  - id: "{d}"\n    title: "{d.title()}"\n    areas:\n'
                    f'      - id: "{d}.motors"\n        title: "Motors"\n        concepts:'
                    + (f'\n          - id: "{d}-turbo"\n            title: "Turbo"\n'
                       if with_concepts else " []\n"))
        return out

    cat = (vault / "catalog.yaml").read_text(encoding="utf-8")
    (vault / "catalog.yaml").write_text(cat + domains(False), encoding="utf-8")
    v = Vault(vault)
    shared = Index(v, Catalog.load(v), save=False)
    try:
        dry_prime(v, "claude-ai", PROMPT, index=shared)
        for d in ("racing", "cars"):
            note(vault, f"concepts/{d}-turbo.md", f'id: {d}-turbo\ntitle: "Turbo car motor"\n'
                 f'parent: "[[{d}.motors]]"\ndomain: {d}\n',
                 "# Turbo car motor\n\nHow a car motor turbo works, for motor racing.")
        (vault / "catalog.yaml").write_text(cat + domains(True), encoding="utf-8")
        got = dry_prime(v, "claude-ai", PROMPT, index=shared)
        assert "racing-turbo" not in _ids(got) and "cars-turbo" in _ids(got)
        assert _same(got, dry_prime(v, "claude-ai", PROMPT))
    finally:
        shared.db.close()


def test_a_shared_index_writes_nothing(vault, home, monkeypatch):
    from keepfold import index
    from keepfold.catalog import Catalog
    _withheld_pages(vault)
    v = Vault(vault)
    c = Keepfold(v)
    c.sessions.client = "claude-ai"
    c.keepfold_prime(PROMPT)  # the caches a real call makes exist now
    c._call("keepfold_log", c.keepfold_log, {"kind": "touch", "payload": {"ids": ["engines"]}})
    c.sessions.close(summary="test")  # an events file
    monkeypatch.setattr(index, "SNAPSHOT_MIN", 0)  # any refresh would save the snapshot

    def files():
        return {p: p.read_bytes() for d in (vault, home) for p in d.rglob("*") if p.is_file()}

    before = files()
    shared = index.Index(v, Catalog.load(v), save=False)
    try:
        for client in ("claude-ai", "mcp", "cursor", "", "unknown", "claude-ai"):
            dry_prime(v, client, PROMPT + " //note remember this", index=shared)
        assert not shared.save  # a core-only run never turns saving back on
    finally:
        shared.db.close()
    assert files() == before


def test_the_app_keeps_one_dry_run_index_and_lets_it_go(vault, monkeypatch):
    from keepfold.app import server
    _aperture(vault)
    srv, app = _serve(vault)
    try:
        body = {"client": "claude-ai", "prompt": PROMPT}
        first = call(app, "POST", "/api/xray/dryrun", body)[1]
        held = app.dry_index[1]
        assert call(app, "POST", "/api/xray/dryrun", {**body, "client": "mcp"})[0] == 200
        assert call(app, "POST", "/api/xray/compare", {"prompt": PROMPT, "a": "claude-ai",
                                                       "b": "cursor"})[0] == 200
        assert app.dry_index[1] is held  # one index for dry runs and compares
        assert call(app, "POST", "/api/xray/dryrun", body)[1] == first
        assert not app.drop_dry()  # used just now
        assert app.drop_dry(time.monotonic() + server.DRY_IDLE_S + 1)  # idle: it goes
        assert app.dry_index is None
        call(app, "POST", "/api/xray/dryrun", body)
        assert app.dry_index is not None and app.dry_index[1] is not held
        monkeypatch.setattr(app, "root", vault.parent)  # another vault open: it goes
        assert app.drop_dry() and app.dry_index is None
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.slow
@pytest.mark.perf
def test_dry_runs_on_a_shared_index_stay_quick_on_5k_notes(tmp_path, capsys):
    from test_home import _big

    from keepfold.catalog import Catalog
    from keepfold.index import Index
    root = tmp_path / "big"
    _big(root)
    v = Vault(root)
    shared = Index(v, Catalog.load(v), save=False)
    took = []
    try:
        for client in ("claude-ai", "unknown", "claude-ai", "unknown"):  # the first builds
            t = time.perf_counter()
            dry_prime(v, client, PROMPT, index=shared)
            took.append(time.perf_counter() - t)
    finally:
        shared.db.close()
    with capsys.disabled():
        print("\nX-ray dry runs on a shared index, 5k notes: " + ", ".join(
            f"{c} {s:.2f} s" for c, s in zip(("claude-ai", "unknown") * 2, took, strict=True)))
    # ~0.5-1.0 s each here (a fresh index per run: ~1.3 s for claude-ai, ~6 s for unknown);
    # the bound guards against a regression
    assert max(took[1:]) < 1.5


def test_scope_bar_and_served_most_often(vault):
    from keepfold import profiles
    v = Vault(vault)
    for client, ids in (("claude-code", ["engines", "pasta"]), ("claude-code", ["engines"]),
                        ("claude-ai", ["engines"]), ("mystery-app", ["pasta"]),
                        (None, ["pasta"]), ("cursor", ["engines"])):
        xray.record(v, "keepfold_prime", client, {"related": ids, "profile": ["me/profile"]},
                    {}, 10)
    data = profiles.load(v)
    view = xray.view(v)
    g = {c["client"]: c for c in view["clients"]}
    assert (g["Claude Code"]["rule"], g["Claude Code"]["scopes"]) == ("claude", ["all"])
    assert g["Claude Code"]["shared"] == ["Claude Desktop"]
    assert g["Claude Desktop"]["shared"] == ["Claude Code"]
    assert (g["Cursor"]["rule"], g["Cursor"]["scopes"], g["Cursor"]["shared"]) == (
        "cursor", profiles.CODING, [])
    m = g["mystery-app"]
    assert (m["name"], m["rule"], m["scopes"], m["shared"]) == ("mystery-app", None, ["core"],
                                                                 [])
    u = g["unknown"]
    assert (u["name"], u["rule"], u["scopes"]) == ("", None, ["core"])
    assert [(x["id"], x["count"]) for x in g["Claude Code"]["often"]] == [
        ("engines", 2), ("pasta", 1)]  # me/ pages, served every time, are left out
    assert view["profiles"] == data and view["profiles_max"] == profiles.MAX_PROFILES
    assert view["all_scopes"][0] == "all"
    # one matcher: scopes_for agrees with the rule the view shows
    assert profiles.scopes_for(data, "mystery-app") == frozenset(["core"])
    assert profiles.match(data, "Claude Code") == 0 and profiles.match(data, "") is None
    # the coding apps' own MCP client names get the coding profile, not the core only
    for name in ("windsurf-client", "Windsurf", "antigravity-client", "Cline", "opencode",
                 "JetBrains-IU-copilot-intellij", "gemini-cli-mcp-client", "cursor-vscode"):
        assert profiles.scopes_for(profiles.DEFAULT, name) == frozenset(profiles.CODING), name


def test_dry_run_endpoint(vault):
    _aperture(vault)
    srv, app = _serve(vault)
    try:
        body = {"client": "claude-ai", "prompt": PROMPT}
        assert call(app, "POST", "/api/xray/dryrun", body, token=False)[0] == 401
        for bad in ({"client": 7, "prompt": PROMPT}, {"client": "x" * 101, "prompt": PROMPT},
                    {"client": "cursor", "prompt": 7}, {"client": "cursor", "prompt": " "},
                    {"client": "cursor", "prompt": "a" * 2001}, {"prompt": PROMPT}):
            assert call(app, "POST", "/api/xray/dryrun", bad)[0] == 400, bad
        status, js, *_ = call(app, "POST", "/api/xray/dryrun", body)
        assert status == 200 and js["chars"] > 0 and set(js) == {"served", "trimmed", "chars"}
        related = next(b for b in js["served"] if b["block"] == "related")
        assert "Engines" in [x["title"] for x in related["items"]]
        dumped = json.dumps(js)
        for text in ("Secret motor", "secret-motor", "Automobile", "how a motor"):
            assert text not in dumped  # titles only, never text, never a private page
        assert not (vault / xray.RING).exists()  # nothing logged
    finally:
        srv.shutdown()
        srv.server_close()
