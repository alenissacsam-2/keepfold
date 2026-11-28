"""R7: Codex CLI and Gemini CLI get Keepfold's prime on each prompt from their own hooks
(`keepfold prime-hook --client codex|gemini`), and `keepfold taint` after another server's tool.
The stdin samples follow each app's documented hook input (made-up content):
Codex: learn.chatgpt.com/docs/hooks and codex-rs/hooks/schema/generated/*.schema.json;
Gemini: github.com/google-gemini/gemini-cli docs/hooks/reference.md."""

import json
import re
import statistics
import sys
import time

import pytest
from conftest import note

from keepfold import delta, prime_hook, taint, xray
from keepfold.app import targets
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

ENTRY = {"command": "C:\\tools\\keepfold.exe", "args": ["mcp", "--vault", "C:\\notes"]}
DBMS = "".join(f"- DBMS rule {i}: tables first, then the SQL query, one worked example.\n"
               for i in range(14))  # past the 800-char preferences cap: Email waits
CODEX_SID = "019a7c3e-55b1-7d42-9e0a-2f6c1b8d4e21"
GEMINI_SID = "5f0c2a9e-3b7d-4c1e-8a6f-0d2e4b9c7a13"


@pytest.fixture
def vault(root, monkeypatch):
    (root / "me/preferences.md").write_text(
        "# Preferences\n\n## Learning\n" + DBMS
        + "\n## Communication\n- Email: formal greeting, at most five sentences.\n",
        encoding="utf-8")
    (root / ".keepfold").mkdir(exist_ok=True)
    (root / ".keepfold/profiles.json").write_text(json.dumps({"profiles": [
        {"match": "codex", "scopes": ["all"]}, {"match": "gemini", "scopes": ["all"]}],
        "unknown": ["core"]}), encoding="utf-8")
    monkeypatch.setenv("KEEPFOLD_VAULT", str(root))
    return root


def codex(prompt, cwd, event="UserPromptSubmit", **kw):
    return json.dumps({"session_id": CODEX_SID, "transcript_path": None, "cwd": str(cwd),
                       "hook_event_name": event, "model": "gpt-5-codex",
                       "permission_mode": "default", "turn_id": "turn-1", "prompt": prompt,
                       **kw})


def gemini(prompt, cwd, event="BeforeAgent", **kw):
    return json.dumps({"session_id": GEMINI_SID, "transcript_path": str(cwd / "t.json"),
                       "cwd": str(cwd), "hook_event_name": event,
                       "timestamp": "2026-11-23T10:00:00.000Z", "prompt": prompt, **kw})


def context(out, event):
    got = json.loads(out)["hookSpecificOutput"]
    assert got["hookEventName"] == event
    return got["additionalContext"]


@pytest.mark.parametrize("client,make,event,mcp_name", [
    ("codex", codex, "UserPromptSubmit", "codex-mcp-client"),
    ("gemini", gemini, "BeforeAgent", "gemini-cli-mcp-client")])
def test_first_prompt_primes_then_only_deltas(vault, tmp_path, client, make, event, mcp_name):
    first = context(prime_hook.run(make("Revise DBMS normalisation", tmp_path), client), event)
    assert first.startswith(prime_hook.HEAD) and first.endswith(prime_hook.NEXT)
    assert "DBMS rule 13" in first and "Email" not in first
    second = context(prime_hook.run(make("Write an email to my professor", tmp_path), client),
                     event)
    assert second.startswith(delta.HEAD) and "Email: formal greeting" in second
    assert "DBMS" not in second and prime_hook.HEAD not in second
    assert prime_hook.run(make("Another email for the dean", tmp_path), client) == ""
    sid = CODEX_SID if client == "codex" else GEMINI_SID
    assert json.loads((vault / prime_hook.SERVED).read_text("utf-8"))[sid]["n"] == 3
    rows = [r for r in xray.rows(Vault(vault)) if r["tool"] == "prime-hook"]
    assert [r["client"] for r in rows] == [mcp_name, mcp_name]


def test_tags_and_session_start(vault, tmp_path):
    out = context(prime_hook.run(gemini("hi //pref: answers in tables", tmp_path), "gemini"),
                  "BeforeAgent")
    assert "Keepfold saved the user's //pref" in out and "answers in tables" in out
    assert "answers in tables" in (vault / "me/preferences.md").read_text("utf-8")
    assert prime_hook.run(gemini("", tmp_path, "SessionStart", source="startup"), "gemini") == ""
    again = context(prime_hook.run(codex("", tmp_path, "SessionStart", source="compact"),
                                   "codex"), "SessionStart")
    assert again.startswith(prime_hook.AGAIN.format(source="compacted"))
    assert prime_hook.run(codex("x", tmp_path), "cursor") == ""  # no such client: nothing


def test_console_entry_takes_the_client(vault, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["keepfold", "prime-hook", "--client", "gemini"])
    monkeypatch.setattr(taint, "read_stdin", lambda: gemini("Revise DBMS please", tmp_path))
    taint.main()
    assert context(capsys.readouterr().out, "BeforeAgent").startswith(prime_hook.HEAD)


def test_taint_maps_each_apps_tool_names(vault, tmp_path):
    marker = vault / taint.MARKER
    for app, tool in (("gemini", "web_fetch"), ("gemini", "google_web_search"),
                      ("gemini", "mcp_fetch_get"), ("codex", "mcp__fetch__get")):
        marker.unlink(missing_ok=True)
        assert taint.run(json.dumps({"session_id": "s", "hook_event_name": "AfterTool",
                                     "tool_name": tool})), (app, tool)
        assert marker.is_file()
    marker.unlink()
    for tool in ("mcp_keepfold_keepfold_prime", "mcp__keepfold__keepfold_patch"):
        assert not taint.run(json.dumps({"session_id": "s", "tool_name": tool}))
    assert not marker.exists()
    m = re.compile(targets.Gemini.TAINT)  # Gemini tests it as a JS RegExp: same syntax here
    assert all(m.search(t) for t in ("web_fetch", "google_web_search", "mcp_fetch_get"))
    assert not any(m.search(t) for t in ("mcp_keepfold_keepfold_prime", "read_file",
                                         "run_shell_command"))


def test_hook_command_quotes_for_each_shell(monkeypatch):
    monkeypatch.setattr(targets.sys, "platform", "win32")
    exe = {"command": "C:\\Program Files\\O'Neil\\keepfold.exe"}
    assert targets.hook_command(exe, "taint --client gemini", powershell=True) == \
        "& 'C:\\Program Files\\O''Neil\\keepfold.exe' taint --client gemini"
    assert targets.hook_command(exe, "prime-hook --client codex") == \
        '"C:\\Program Files\\O\'Neil\\keepfold.exe" prime-hook --client codex'
    monkeypatch.setattr(targets.sys, "platform", "linux")
    assert targets.hook_command({"command": "/opt/my tools/keepfold"}, "taint --client codex") \
        == "'/opt/my tools/keepfold' taint --client codex"


def _baks(p):
    return sorted(p.parent.glob(p.name + ".keepfold-*.bak"))


def test_gemini_add_then_remove_keeps_the_owners_hooks(home):
    p = home / ".gemini" / "settings.json"
    p.parent.mkdir()
    mine = {"type": "command", "command": "node lint.js", "name": "lint"}
    old = {"theme": "dark", "hooks": {"BeforeAgent": [{"hooks": [mine]}]}}
    p.write_text(json.dumps(old, indent=4), encoding="utf-8")
    t = targets.by_id()["gemini-cli"]
    assert {"file": str(p), "change": "add", "detail": "the Keepfold hooks"} in t.plan(True, ENTRY)
    t.apply(ENTRY)
    hooks = json.loads(p.read_text("utf-8"))["hooks"]
    assert hooks["BeforeAgent"][0] == {"hooks": [mine]} and len(hooks["BeforeAgent"]) == 2
    ours = hooks["BeforeAgent"][1]["hooks"][0]
    assert ours["command"].endswith(" prime-hook --client gemini") and ours["timeout"] == 30000
    assert hooks["AfterTool"][0]["matcher"] == targets.Gemini.TAINT
    assert hooks["SessionStart"][0]["hooks"][0]["command"] == ours["command"]
    assert t.plan(True, ENTRY) == []  # nothing left to do
    moved = {**ENTRY, "command": "D:\\new\\keepfold.exe"}
    assert {"file": str(p), "change": "update", "detail": "the Keepfold hooks"} in t.plan(True,
                                                                                         moved)
    t.apply(moved)
    hooks = json.loads(p.read_text("utf-8"))["hooks"]
    assert len(hooks["BeforeAgent"]) == 2 and "new" in hooks["BeforeAgent"][1]["hooks"][0][
        "command"]
    assert {"file": str(p), "change": "remove", "detail": "the Keepfold hooks"} in t.plan(False)
    t.remove()
    data = json.loads(p.read_text("utf-8"))
    assert data == {**old, "mcpServers": {}} and _baks(p)


def test_codex_add_then_remove_leaves_hooks_json_as_it_was(home):
    d = home / ".codex"
    d.mkdir()
    p = d / "hooks.json"
    t = targets.by_id()["codex"]
    rows = t.plan(True, ENTRY)
    assert {"file": str(p), "change": "add", "detail": "the Keepfold hooks"} in rows
    t.apply(ENTRY)
    hooks = json.loads(p.read_text("utf-8"))["hooks"]
    assert set(hooks) == {"UserPromptSubmit", "SessionStart", "PostToolUse"}
    assert hooks["SessionStart"][0]["matcher"] == "compact|resume|clear"
    assert hooks["PostToolUse"][0]["matcher"] == "^mcp__"
    assert hooks["UserPromptSubmit"][0]["hooks"][0]["timeout"] == 30
    t.remove()
    assert not p.exists() and _baks(p)  # only Keepfold's hooks were in it: gone, backed up

    old = {"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "python3 log.py", "timeout": 5}]}]}}
    p.write_text(json.dumps(old), encoding="utf-8")
    t.apply(ENTRY)
    assert len(json.loads(p.read_text("utf-8"))["hooks"]["PostToolUse"]) == 2
    t.remove()
    assert json.loads(p.read_text("utf-8")) == old


@pytest.mark.parametrize("bad", ["{not json", "[1]", '{"hooks": []}',
                                 '{"hooks": {"UserPromptSubmit": {}}}'])
def test_an_unparseable_hooks_file_is_left_alone(home, bad):
    d = home / ".codex"
    d.mkdir()
    p, toml = d / "hooks.json", d / "config.toml"
    p.write_text(bad, encoding="utf-8")
    toml.write_text('model = "o3"\n', encoding="utf-8")
    t = targets.by_id()["codex"]
    rows = [r for r in t.plan(True, ENTRY) if r["file"] == str(p)]
    assert rows[0]["change"] == "error" and "untouched" in rows[0]["detail"]
    with pytest.raises(ValueError, match="untouched"):
        t.apply(ENTRY)
    assert p.read_text("utf-8") == bad and toml.read_text("utf-8") == 'model = "o3"\n'
    assert not _baks(p) and not _baks(toml)


def test_each_apps_hook_serves_its_own_guesses_not_another_apps(vault, tmp_path):
    """R5d through the hooks: a guess made in a Codex session reaches Codex's hook, and
    neither Gemini's nor Claude Code's, until the owner approves it."""
    from keepfold.mcp_server import Keepfold
    (vault / "me/preferences.md").write_text(  # short: within the preferences cap
        "# Preferences\n\n## Communication\n- Email: formal greeting.\n", encoding="utf-8")
    c = Keepfold(Vault(vault))
    c.over_mcp, c.sessions.client = True, "codex-mcp-client"
    out = c._call("keepfold_patch", c.keepfold_patch, {
        "id": "me/preferences", "op": "append_to_section", "target": "Communication",
        "text": "Likes tables over prose."})
    assert "error" not in out and out.get("changed"), out
    c.sessions.close(summary="test")
    assert "Likes tables over prose" in context(
        prime_hook.run(codex("plan the DBMS email", tmp_path), "codex"), "UserPromptSubmit")
    assert "Likes tables over prose" not in context(
        prime_hook.run(gemini("plan the DBMS email", tmp_path), "gemini"), "BeforeAgent")
    claude = codex("plan the DBMS email", tmp_path).replace(CODEX_SID, GEMINI_SID[::-1])
    assert "Likes tables over prose" not in context(prime_hook.run(claude), "UserPromptSubmit")


def _snapshot(root):
    """The Index's snapshot, as a server run leaves it for the hook."""
    from keepfold.catalog import Catalog
    from keepfold.index import Index

    Index(Vault(root), Catalog.load(Vault(root))).save_snapshot()


def related(ctx):
    return re.findall(r"\[\[([^\]]+)\]\]", ctx.split("## Related\n", 1)[1]) if (
        "## Related\n" in ctx) else []


def test_hook_serves_related_and_the_repo_project_first(vault, tmp_path):
    """L9: the hook serves a short Related block itself, keyword hits from the Index's
    snapshot (no model, the index never built), with the project of the repo the app runs in
    first; a page made private since the snapshot is left out; no snapshot, no block; a
    core-only app gets none. The related_only line for the server stays."""
    note(vault, "projects/messmate.md", "title: MessMate\nsummary: Flutter app for the mess\n",
         "# MessMate\n\nFlutter and FastAPI.")
    note(vault, "projects/portfolio-site.md", "title: Portfolio site\n", "# Portfolio\n\nAstro.")
    note(vault, "decisions/dec-postgres.md", 'title: "MessMate database: PostgreSQL"\n',
         "# DB\n\nPostgreSQL for the analytics database.")
    note(vault, "decisions/dec-diary.md", "title: Database diary\n", "# Diary\n\nA database.")
    repo = tmp_path / "code" / "Portfolio-Site"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    ask = "Which database for the analytics feature?"
    first = context(prime_hook.run(codex(ask, repo / "src"), "codex"), "UserPromptSubmit")
    assert "## Related" not in first and first.endswith(prime_hook.NEXT)  # no snapshot yet
    _snapshot(vault)
    note(vault, "decisions/dec-diary.md", "title: Database diary\nprivate: true\n", "# Diary\n")
    out = context(prime_hook.run(codex(ask, repo / "src", session_id="chat-2"), "codex"),
                  "UserPromptSubmit")
    assert related(out) == ["portfolio-site", "dec-postgres"], out
    block = out.split("## Related\n", 1)[1]
    assert block.startswith('<untrusted_note id="related">\n- [[portfolio-site]] — Portfolio')
    assert out.endswith(prime_hook.NEXT) and "related_only=true" in out
    row = [r for r in xray.rows(Vault(vault)) if r["tool"] == "prime-hook"][-1]
    assert row["served"]["related"] == ["portfolio-site", "dec-postgres"]
    elsewhere = codex(ask, tmp_path, session_id="chat-3")  # no project's repo: keyword only
    assert related(context(prime_hook.run(elsewhere, "codex"), "UserPromptSubmit")) == [
        "dec-postgres"]
    claude = codex(ask, repo, session_id="chat-4")  # this vault's profiles: Claude Code core
    assert related(context(prime_hook.run(claude), "UserPromptSubmit")) == []


def test_hook_related_keeps_out_projects_a_profile_does_not_allow(vault, tmp_path):
    """The hook's Related follows profiles.withheld as the server's index does: an app whose
    profile lacks the projects scope gets no project page, not even the repo's own."""
    (vault / ".keepfold/profiles.json").write_text(json.dumps({"profiles": [
        {"match": "codex", "scopes": ["coding"]}], "unknown": ["core"]}), encoding="utf-8")
    note(vault, "projects/portfolio-site.md", "title: Portfolio site\n",
         "# Portfolio\n\nThe analytics database.")
    note(vault, "decisions/dec-postgres.md", 'title: "Analytics database: PostgreSQL"\n',
         "# DB\n\nPostgreSQL for the analytics database.")
    repo = tmp_path / "code" / "Portfolio-Site"
    (repo / ".git").mkdir(parents=True)
    _snapshot(vault)
    out = context(prime_hook.run(codex("Which database for the analytics feature?", repo),
                                 "codex"), "UserPromptSubmit")
    assert related(out) == ["dec-postgres"], out


@pytest.mark.perf
def test_hook_related_at_5k_pages_costs_under_100_ms(vault, tmp_path, monkeypatch):
    """L9's bound: on a 5,000-page snapshot (100 decisions on disk, the rest notes in it
    only) the Related block adds at most 100 ms to the hook's light prime."""
    for i in range(100):
        note(vault, f"decisions/dec-{i}.md", f"title: Decision {i} on topic t{i % 40}\n",
             f"# Decision {i}\n\n" + f"Chose option {i} for the database of t{i % 40}.\n" * 10)
    _snapshot(vault)
    snap = vault / ".keepfold/index-snapshot.json"
    data = json.loads(snap.read_bytes())
    body = "".join(f"Page text about topic t{i % 40} and the database.\n" for i in range(25))
    data["files"].update({f"notes/n-{i}.md": [1, 1, "note", f"n-{i}", {"title": f"Page {i}"},
                                              body] for i in range(5000 - len(data["files"]))})
    snap.write_text(json.dumps(data), encoding="utf-8")

    def runs(n=7):
        out = []
        for _ in range(n):
            t = time.perf_counter()
            assert prime_hook._light(vault, "Which database for topic t7?", None,
                                     "codex-mcp-client", str(tmp_path)) is not None
            out.append(time.perf_counter() - t)
        return statistics.median(out)

    runs(2)  # warm the imports and the file cache
    new = runs()
    with monkeypatch.context() as m:
        m.setattr(prime_hook, "_related", lambda *a: [])  # the hook before L9
        old = runs()
    print(f"hook light prime at 5k pages: {old * 1000:.0f} ms before, {new * 1000:.0f} ms "
          f"with Related (+{(new - old) * 1000:.0f} ms)")
    assert new - old <= 0.1, (old, new)
