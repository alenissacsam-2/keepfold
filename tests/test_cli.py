import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from keepfold import doctor
from keepfold.cli import app
from keepfold.events import EventLog
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

FIXTURES = Path(__file__).parent / "fixtures"
runner = CliRunner()


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    shutil.copytree(FIXTURES / "vault_mini", root)
    return root


def run(*args, ok=True):
    r = runner.invoke(app, [str(a) for a in args])
    if ok:
        assert r.exit_code == 0, r.output
    return r


def events(root):
    return list(EventLog(Vault(root)).iter_events())


def snapshot(root):
    return {p: p.read_bytes() for p in root.rglob("*.md")}


# ---- vault resolution, init, doctor ---------------------------------------------

def test_no_vault_is_a_clear_error():
    r = run("brief", ok=False)
    assert r.exit_code == 1 and "--vault" in r.output and "KEEPFOLD_VAULT" in r.output
    assert "keepfold app" in r.output
    assert "fix: Run `keepfold app`" in run("doctor", ok=False).output


def test_init_is_idempotent_and_touches_no_notes(vault, home):
    before = snapshot(vault)
    first = run("init", vault).output
    assert "created  git init" in first and "created  pre-push hook" in first
    assert (vault / ".keepfold/config.yaml").read_text() == "schema: 1\n"
    assert "!.keepfold/config.yaml" in (vault / ".gitignore").read_text()
    assert (vault / "sessions").is_dir()
    assert str(vault.resolve()) in (home / ".keepfold/config.yaml").read_text(encoding="utf-8")
    after_first = snapshot(vault)
    assert all(after_first[p] == b for p, b in before.items())  # existing notes untouched
    second = run("init", vault).output
    assert "created  git init" not in second and "skipped  pre-push hook" in second
    assert snapshot(vault) == after_first
    # the pointer now resolves the vault without --vault
    assert "Synthetic brief" in run("brief").output


def test_init_keeps_crash_leftover_temp_files_out_of_git(vault, tmp_path):
    """D1 §2.2: vault._atomic_write's `.<name>.<random>.tmp` never enters a session commit."""
    fresh = tmp_path / "fresh"
    run("init", fresh)
    assert ".*.tmp" in (fresh / ".gitignore").read_text(encoding="utf-8").splitlines()
    # an older vault's .gitignore
    (vault / ".gitignore").write_text(".keepfold/*\n!.keepfold/config.yaml", encoding="utf-8")
    run("init", vault)
    run("init", vault)
    lines = (vault / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines.count(".*.tmp") == 1 and lines[:2] == [".keepfold/*", "!.keepfold/config.yaml"]
    if shutil.which("git") and (vault / ".git").is_dir():
        (vault / "concepts" / ".two-pointers.md.x1y2.tmp").write_text("leftover")
        assert subprocess.run(["git", "-C", str(vault), "check-ignore", "-q",
                               "concepts/.two-pointers.md.x1y2.tmp"]).returncode == 0


def test_init_moves_the_default_vault_only_when_unset_or_asked(vault, home, tmp_path):
    from keepfold import taint

    other = tmp_path / "other"
    run("init", vault)  # no config yet: this becomes the default
    assert taint.config()["vault"] == str(vault.resolve())
    out = run("init", other).output  # a second vault keeps the first as the default
    assert "kept the default vault" in out and "--default" in out
    assert taint.config()["vault"] == str(vault.resolve())
    assert "skipped  pointer" in run("init", vault).output
    run("init", other, "--default")
    assert taint.config()["vault"] == str(other.resolve())


def test_init_writes_obsidian_config_and_upgrades_old_default_groups(vault):
    from keepfold.cli import COLOR_GROUPS

    graph = vault / ".obsidian/graph.json"
    graph.parent.mkdir()
    graph.write_text(json.dumps({"scale": 2, "colorGroups": [  # only old Keepfold defaults
        {"query": "path:areas", "color": {"a": 1, "rgb": 1}},
        {"query": "[keepfold_status:solid]", "color": {"a": 1, "rgb": 0x1D9E75}}]}))
    run("init", vault)
    data = json.loads(graph.read_text())
    assert data["scale"] == 2  # other graph settings are kept
    assert [g["query"] for g in data["colorGroups"]] == [q for q, _ in COLOR_GROUPS]
    assert data["colorGroups"][0] == {"query": "file:my-topic-map",
                                      "color": {"a": 1, "rgb": 0xFFFFFF}}
    assert data["colorGroups"][1]["query"] == "tag:#topic"
    assert "keepfold_next <= today()" in (vault / "views/Due.base").read_text()
    assert (vault / "views/Weak.base").is_file()
    before = {p: p.read_bytes() for p in (graph, vault / "views/Due.base")}
    second = run("init", vault).output
    assert "skipped  .obsidian/graph.json colorGroups" in second
    assert {p: p.read_bytes() for p in before} == before


def test_init_graph_scheme_on_empty_file_and_custom_groups_left_alone(vault, tmp_path):
    from keepfold.cli import COLOR_GROUPS

    graph = vault / ".obsidian/graph.json"
    graph.parent.mkdir()
    graph.write_text("{}")
    run("init", vault)
    assert [g["query"] for g in json.loads(graph.read_text())["colorGroups"]] == \
        [q for q, _ in COLOR_GROUPS]
    other = tmp_path / "other"
    (other / ".obsidian").mkdir(parents=True)
    custom = json.dumps({"scale": 3, "colorGroups": [
        {"query": "path:areas", "color": {"a": 1, "rgb": 1}},
        {"query": "tag:#mine", "color": {"a": 1, "rgb": 2}}]})
    (other / ".obsidian/graph.json").write_text(custom)
    out = run("init", other).output
    assert "skipped  .obsidian/graph.json colorGroups (custom colour groups)" in out
    assert (other / ".obsidian/graph.json").read_text() == custom


def test_pre_push_hook_refuses(vault, tmp_path):
    run("init", vault)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git = ["git", "-C", str(vault)]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "x"], check=True)
    r = subprocess.run([*git, "push", str(remote), "HEAD:main"], capture_output=True, text=True)
    assert r.returncode != 0 and "push refused" in r.stderr


def test_doctor_pass_and_fail(vault, monkeypatch):
    monkeypatch.delenv("OneDrive", raising=False)
    monkeypatch.delenv("OneDriveConsumer", raising=False)
    monkeypatch.delenv("OneDriveCommercial", raising=False)
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda c, *a, **k: "keepfold" if c == "keepfold"
                        else real(c, *a, **k))
    from keepfold.app import targets
    monkeypatch.setattr(targets, "_which", lambda c: None)  # never the real `claude`
    r = run("doctor", "--vault", vault, ok=False)  # no git yet
    assert r.exit_code == 1 and "FAIL  Vault git" in r.output
    run("init", vault)
    r = run("doctor", "--vault", vault)
    assert r.output.count("PASS") == 22  # + Excluded, PATH, model, git identity, last commit and
    # index.lock, private browse, the chat archive, old browse files, the two client rows
    # (Claude Desktop, Claude Code: "not installed" here), import shapes ("no imported
    # pages") and sync conflict copies
    assert "PASS  Sync conflict copies: none" in r.output
    assert "WARN  Search model" in r.output  # the tmp home has no model
    subprocess.run(["git", "-C", str(vault), "remote", "add", "origin", "x"], check=True)
    r = run("doctor", "--vault", vault)
    assert "WARN  Vault git" in r.output and "origin" in r.output


def test_doctor_warns_on_duplicate_keepfold_servers(home):
    settings = home / "settings.json"
    desktop = home / "claude_desktop_config.json"
    entry = {"command": r"C:\tools\keepfold.exe", "args": ["mcp", "--vault", "v"]}
    desktop.write_text(json.dumps({"mcpServers": {"keepfold": entry}}), encoding="utf-8")
    settings.write_text(json.dumps({"enabledPlugins": {"keepfold@keepfold": False}}),
                        encoding="utf-8")
    assert doctor.check_duplicate_servers(settings, [desktop])[0] == "PASS"
    settings.write_text(json.dumps({"enabledPlugins": {"keepfold@keepfold": True}}),
                        encoding="utf-8")
    status, _, detail, fix = doctor.check_duplicate_servers(settings, [desktop])
    assert status == "WARN" and "Code tab" in detail and "duplicated" in detail
    assert "Chat needs the desktop entry" in fix and "CLI needs the plugin" in fix
    uv = {"command": "uv", "args": ["run", "keepfold", "mcp"]}  # `uv run keepfold mcp` counts too
    desktop.write_text(json.dumps({"mcpServers": {"x": uv}}), encoding="utf-8")
    assert doctor.check_duplicate_servers(settings, [desktop])[0] == "WARN"
    desktop.write_text(json.dumps({"mcpServers": {"x": {"command": "node"}}}),
                       encoding="utf-8")
    assert doctor.check_duplicate_servers(settings, [desktop])[0] == "PASS"


def test_doctor_sync_folder(vault, monkeypatch, tmp_path):  # name must not say OneDrive
    for var in ("OneDriveConsumer", "OneDriveCommercial"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OneDrive", str(tmp_path))
    assert doctor.check_onedrive(vault)[0] == "WARN"
    monkeypatch.setenv("OneDrive", str(tmp_path / "elsewhere"))
    assert doctor.check_onedrive(vault)[0] == "PASS"
    assert doctor.check_onedrive(tmp_path / "OneDrive" / "v")[0] == "WARN"
    assert doctor.check_python()[0] == "PASS"


def test_doctor_warns_without_the_search_model(home):
    from keepfold import semantic

    status, _, _, fix = doctor.check_search_model()
    assert status == "WARN" and "keepfold model download" in fix
    semantic.models_dir().mkdir(parents=True, exist_ok=True)
    (semantic.models_dir() / "model.onnx").write_bytes(b"x")
    assert doctor.check_search_model()[0] == "PASS"


def test_doctor_warns_when_no_ai_app_is_connected(home):
    """N9: without a keepfold entry in any app the app's Connect section lists, Keepfold is idle."""
    status, _, _, fix = doctor.check_ai_apps()
    assert status == "WARN" and "keepfold app" in fix
    (home / ".cursor").mkdir()
    (home / ".cursor" / "mcp.json").write_text(
        '{"mcpServers": {"keepfold": {"command": "keepfold", "args": ["mcp"]}}}', encoding="utf-8")
    assert doctor.check_ai_apps()[:3] == ("PASS", "AI app connected", "Cursor")


def test_doctor_warns_when_keepfold_is_not_on_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda c, *a, **k: None)
    status, _, detail, fix = doctor.check_on_path()
    assert status == "WARN" and "plugin" in detail and "uv tool update-shell" in fix
    monkeypatch.setattr(shutil, "which", lambda c, *a, **k: "C:\\bin\\keepfold.exe")
    assert doctor.check_on_path()[0] == "PASS"


def test_doctor_warns_when_the_local_model_is_on_but_silent(home, monkeypatch):
    from keepfold import localllm

    assert doctor.check_local_model()[1:3] == ("Local model", "off")
    (home / ".keepfold").mkdir()
    (home / ".keepfold/config.yaml").write_text("localllm:\n  enabled: true\n", encoding="utf-8")
    monkeypatch.setattr(localllm, "find", lambda config=None, **_: None)
    status, _, _, fix = doctor.check_local_model()
    assert status == "WARN" and "keepfold llm start" in fix

    class Fake:
        kind = "ollama"

        def describe(self):
            return "ollama qwen3.5:4b at 127.0.0.1:11434"

    monkeypatch.setattr(localllm, "find", lambda config=None, **_: Fake())
    assert doctor.check_local_model()[0] == "PASS"


def test_init_sets_a_vault_git_identity_so_commits_work(vault, monkeypatch):
    """N11: with no identity anywhere (isolated HOME, no env), init sets a repo-local one, so a
    session commit lands; doctor WARNs on a repo without one."""
    from keepfold import session

    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    bare = vault.parent / "bare"
    subprocess.run(["git", "init", "-q", str(bare)], check=True)
    assert doctor.check_git_identity(bare)[0] == "WARN"
    assert "git identity Keepfold <keepfold@localhost>" in run("init", vault).output
    git = ["git", "-C", str(vault)]
    assert subprocess.run([*git, "config", "--local", "user.email"], capture_output=True,
                          text=True).stdout.strip() == "keepfold@localhost"
    assert doctor.check_git_identity(vault)[0] == "PASS"
    assert session._commit(Vault(vault), "keepfold: test") is not None
    assert subprocess.run([*git, "log", "-1", "--format=%an <%ae>"], capture_output=True,
                          text=True).stdout.strip() == "Keepfold <keepfold@localhost>"
    assert "git identity" not in run("init", vault).output  # set once


def test_init_commits_its_own_starter_files_once(vault, monkeypatch):
    """R3-8: one `keepfold: init` commit of what init wrote (me/ pages, views, .gitignore...)
    under the repo-local identity; the user's own notes stay uncommitted; a rerun adds none."""
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    out = run("init", vault).output
    assert "created  git commit keepfold: init" in out
    assert out.rstrip().endswith("Next: run `keepfold app`")
    git = ["git", "-C", str(vault)]
    log = subprocess.run([*git, "log", "--format=%s|%an"], capture_output=True,
                         text=True).stdout.splitlines()
    assert log == ["keepfold: init|Keepfold"]
    files = subprocess.run([*git, "ls-files"], capture_output=True, text=True).stdout.split()
    assert {".gitignore", ".gitattributes", ".keepfold/config.yaml"} <= set(files)
    assert any(f.startswith("views/") for f in files)
    assert "me/profile.md" not in files and not any(f.startswith("concepts/") for f in files)
    run("init", vault)
    assert subprocess.run([*git, "rev-list", "--count", "HEAD"], capture_output=True,
                          text=True).stdout.strip() == "1"


# ---- brief ------------------------------------------------------------------------

def test_brief_prints_contract_verbatim_and_today(vault):
    contract = "# Teaching contract\n\n1. Ask before telling.\n2. One step at a time.\n"
    (vault / "me/teaching-contract.md").write_bytes(contract.encode())
    run("log", "taught", "arrays-basics", "--vault", vault)
    out = run("brief", "--vault", vault).output
    assert "Synthetic brief for tests." in out
    assert contract in out
    today = out.split("## Today", 1)[1]
    lines = today.strip().splitlines()
    assert lines[0] == "◌ Unverified: Array basics (arrays-basics) [Taught today, not yet checked]"
    # v8: a plan's next steps in its order, no day or date; the Owed copy of lc-167 is a repeat
    assert "○ Not started: Drill set A (two-pointers) [Test sprint]" in lines
    assert "- Plan step: Two Sum II (lc-167) [Test sprint]" in lines
    assert "- Plan step: Free text item: read the notes [Test sprint]" in lines
    assert "owed copy" not in today and "Day 2" not in today
    assert "Recap" not in today  # a done step
    (vault / "BRIEF.md").unlink()  # optional (a fresh `keepfold init` vault has none): no note
    out = run("brief", "--vault", vault).output
    assert "BRIEF.md" not in out and contract in out
    assert "Done for today ✓" not in today  # items are open


def test_today_prints_and_writes_today_md(vault):
    out = run("today", "--vault", vault).output
    assert out.startswith("## Today") and "Drill set A" in out
    md = (vault / "Today.md").read_text(encoding="utf-8")
    assert "# Today" in md and "Drill set A" in md and "Day 2" not in md
    assert "Done for today" not in out  # open items: not done


def test_today_says_done_only_after_work_today(vault, monkeypatch):
    from datetime import UTC, datetime

    from keepfold import cli, planner

    monkeypatch.setattr(planner, "today", lambda *a, **k: [])
    out = run("today", "--vault", vault).output  # never graded: first steps, not "done"
    assert out.splitlines()[1:3] == ["Nothing planned yet.", cli.FIRST_STEPS]
    assert "Done for today" not in out
    EventLog(Vault(vault)).append(session="cli", kind="grade", source="external",
                                  now=datetime.now(UTC), fields={
                                      "object": "concept:two-pointers", "verdict": "correct"})
    out = run("today", "--vault", vault).output
    assert out.splitlines()[1:3] == ["Nothing planned yet.", "Done for today ✓"]


def test_today_line_skips_an_id_the_title_already_says():
    from keepfold.cli import _today_line

    row = {"state": "not_started", "why": "New", "title": "Sorting", "id": "sorting"}
    assert _today_line(row) == "○ Not started: Sorting [New]"
    assert _today_line(row | {"id": "sort-algos"}) == "○ Not started: Sorting (sort-algos) [New]"


def test_cli_prints_note_text_without_control_characters(vault, monkeypatch):
    from keepfold import planner

    monkeypatch.setattr(planner, "today", lambda *a, **k: [{
        "state": None, "why": "Sprint plan", "id": None, "kind": "track",
        "title": "evil\x1b]8;;http://x\x07link\x1b[2J ‮esrever"}])
    out = run("today", "--vault", vault).output
    assert "\x1b" not in out and "\x07" not in out and "‮" not in out
    assert "- Plan step: evil]8;;http://xlink[2J esrever [Sprint plan]" in out


def test_every_visible_option_has_help():
    import typer

    def walk(cmd, path):
        if hasattr(cmd, "commands"):
            for n, c in cmd.commands.items():
                if not c.hidden:
                    yield from walk(c, [*path, n])
        else:
            yield from (f"{' '.join(path)} {p.opts[0]}" for p in cmd.params
                        if p.param_type_name == "option" and not p.help and not p.hidden)

    assert list(walk(typer.main.get_command(app), [])) == []


# ---- log --------------------------------------------------------------------------

def test_log_result(vault):
    run("log", "result", "2 pointers", "partial", "--hints", 1, "--confidence", 3,
        "--problem", "lc-1", "--vault", vault)
    (ev,) = events(vault)
    assert ev["kind"] == "grade" and ev["session"] == "cli" and ev["source"] == "external"
    assert (ev["object"], ev["verdict"], ev["hints"], ev["confidence"], ev["problem"]) == (
        "concept:two-pointers", "partial", 1, 3, "problem:lc-1")
    r = run("log", "result", "two-pointers", "great", "--vault", vault, ok=False)
    assert r.exit_code == 1 and "pass, partial or fail" in r.output


def test_log_solved_and_taught(vault):
    run("log", "solved", "lc-167", "--hints", 2, "--vault", vault)
    run("log", "taught", "Array basics", "--vault", vault)
    solved, taught = events(vault)
    assert solved["kind"] == "log" and solved["payload"] == {
        "kind": "problem_status", "problem": "lc-167", "status": "solved", "hints": 2}
    note = Vault(vault).read("problem", "lc-167").fields  # audit F4: the note says so too
    assert (note["status"], note["solved_on"]) == ("solved", solved["day"])
    assert taught["kind"] == "taught" and taught["object"] == "concept:arrays-basics"
    assert run("log", "solved", "nope", "--vault", vault, ok=False).exit_code == 1


def test_log_assessment_existing_and_new(vault):
    run("log", "assessment", "acme-oa-2026", "--outcome", "fail", "--score", "1/2",
        "--weak", "two-pointers, dsa", "--vault", vault)
    note = Vault(vault).read("assessment", "acme-oa-2026")
    assert (note.fields["outcome"], note.fields["score"]) == ("fail", "1/2")
    (ev,) = events(vault)
    assert ev["object"] == "assessment:acme-oa-2026"
    assert ev["payload"]["kind"] == "assessment_result"
    assert ev["payload"]["felt_weak"] == ["two-pointers", "dsa"]
    run("log", "assessment", "beta-mock", "--company", "Beta", "--kind", "mock", "--date",
        "2026-11-10", "--outcome", "pass", "--vault", vault)
    new = Vault(vault).read("assessment", "beta-mock")
    assert (new.fields["company"], new.fields["outcome"], new.fields["date"]) == (
        "Beta", "pass", "2026-11-10")
    assert events(vault)[1]["payload"]["assessment_kind"] == "mock"


def test_unknown_concept_goes_to_inbox(vault):
    out = run("log", "result", "Segment trees", "fail", "--vault", vault).output
    assert "inbox:segment-trees" in out and "inbox.md" in out
    run("log", "taught", "Segment trees", "--vault", vault)
    assert [e["object"] for e in events(vault)] == ["inbox:segment-trees"] * 2
    inbox = (vault / "inbox.md").read_text(encoding="utf-8")
    assert inbox.count("- [ ] Segment trees (") == 1


# ---- concept ----------------------------------------------------------------------

def test_concept_add_alias_merge(vault):
    run("concept", "add", "Sliding window", "--parent", "dsa.arrays", "--vault", vault)
    assert (vault / "concepts/sliding-window.md").is_file()
    r = run("concept", "add", "Sliding windows", "--parent", "dsa.arrays", "--vault", vault,
            ok=False)
    assert r.exit_code == 1 and "--force" in r.output
    run("concept", "alias", "sliding-window", "window technique", "--vault", vault)
    run("concept", "merge", "window technique", "two-pointers", "--vault", vault)
    (ev,) = events(vault)
    assert (ev["kind"], ev["session"], ev["object"], ev["ref"]) == (
        "remap", "cli", "concept:two-pointers", "concept:sliding-window")
    assert "sliding-window" not in (vault / "catalog.yaml").read_text(encoding="utf-8")


# ---- replay, backup, undo ---------------------------------------------------------

def test_replay_dry_run_then_write(vault):
    run("log", "result", "two-pointers", "pass", "--vault", vault)
    note = vault / "concepts/two-pointers.md"
    before = note.read_bytes()
    out = run("replay", "--dry-run", "--vault", vault).output
    assert "◐ Learning: 1" in out and "○ Not started: 2" in out and "would change" in out
    assert note.read_bytes() == before
    out = run("replay", "--vault", vault).output
    assert "3 note(s) updated" in out
    fields = Vault(vault).read("concept", "two-pointers").fields
    assert fields["keepfold_status"] == "learning" and fields["keepfold_next"]
    assert "0 note(s) updated" in run("replay", "--vault", vault).output


def test_replay_dry_run_prints_diff_and_writes_nothing(vault):
    run("log", "result", "two-pointers", "pass", "--vault", vault)
    v = Vault(vault)
    v.set_tool_props("concept", "two-pointers", "solid", "2020-01-01")  # stale stamp
    run("replay", "--vault", vault)
    v.set_tool_props("concept", "two-pointers", "solid", "2020-01-01")
    v.set_tool_props("concept", "arrays-basics", "learning", None)
    mtimes = {p: p.stat().st_mtime_ns for p in vault.rglob("*") if p.is_file()}
    before = snapshot(vault)
    out = run("replay", "--dry-run", "--vault", vault).output
    assert "solid → learning: 1" in out and "learning → not_started: 1" in out
    assert "two-pointers  solid → learning  (next 2020-01-01 → " in out
    assert "no application/transfer pass" in out and "1 pass day < 2" in out
    assert "unchanged: " in out
    assert "calibration:" in out and "Not started: 1 concept-day, 100% passed" in out
    assert snapshot(vault) == before
    assert mtimes == {p: p.stat().st_mtime_ns for p in vault.rglob("*") if p.is_file()}


def test_replay_dry_run_names_the_gap_check(vault):
    from datetime import UTC, datetime, timedelta

    from keepfold.events import local_day
    log, now = EventLog(Vault(vault)), datetime.now(UTC)
    for when in (now - timedelta(days=1), now):  # the old Solid rule, but no 7-day gap
        log.append(session="cli", kind="grade", source="tutor", now=when,
                   fields={"object": "concept:two-pointers", "verdict": "correct",
                           "confidence": 4, "item_type": "application"})
    before = snapshot(vault)
    out = run("replay", "--dry-run", "--vault", vault).output
    first = local_day(now - timedelta(days=1))  # solid_after = first pass day + 7
    after = (datetime.fromisoformat(first) + timedelta(days=7)).date().isoformat()
    assert f"one more check after a break (from {after})" in out
    assert "unstamped → learning" in out and snapshot(vault) == before


def test_backup_creates_zip(vault, tmp_path):
    run("init", vault)
    out = run("backup", "--vault", vault).output
    path = Path(out.split(" (")[0])
    assert path.parent == (vault / ".keepfold/backups").resolve() and "bytes" in out
    names = zipfile.ZipFile(path).namelist()
    assert "concepts/two-pointers.md" in names and ".git/HEAD" in names
    assert not any(n.startswith(".keepfold/backups") for n in names)
    r = run("backup", "--to", tmp_path / "out", "--vault", vault)
    assert "same drive" in r.output  # tmp_path shares the vault's drive
    assert not any(n.startswith(".keepfold/backups") for n in
                   zipfile.ZipFile(Path(r.output.split(" (")[0])).namelist())


def _git_out(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          check=True).stdout


def _repo(vault):
    run("init", vault)
    _git_out(vault, "config", "core.autocrlf", "false")
    run("replay", "--vault", vault)  # stamp every concept, as undo will
    _git_out(vault, "add", "-A")
    _git_out(vault, "commit", "-qm", "init")


def _clean(vault):
    assert _git_out(vault, "status", "--porcelain") == ""
    assert not (vault / ".git/REVERT_HEAD").exists()
    assert not any("<<<<<<<" in p.read_text(encoding="utf-8", errors="replace")
                   for p in vault.rglob("*") if p.is_file() and ".git" not in p.parts)


def _session(vault, when, verdict):
    from keepfold.session import SessionManager
    mgr = SessionManager(Vault(vault), clock=lambda: when)
    s = mgr.start()
    mgr.log.append(session=s.id, kind="grade", source="tutor", mode="learn", now=when,
                   fields={"object": "concept:two-pointers", "verdict": verdict, "hints": 0,
                           "confidence": 3})
    mgr.note("graded", "two-pointers")
    assert mgr.close()["commit"]
    return s.id


def test_undo_reverts_a_session_commit(vault):
    _repo(vault)
    sid = "s_01JD0000000000000000000000"
    note = vault / "concepts/arrays-basics.md"
    original = note.read_bytes()
    note.write_text(note.read_text() + "- session edit\n")
    _git_out(vault, "commit", "-qam", f"keepfold: session {sid} learn")
    out = run("undo", sid, "--vault", vault).output
    assert "reverted" in out and note.read_bytes() == original
    assert _git_out(vault, "log", "-1", "--format=%s").strip() == f"keepfold: undo {sid}"
    _clean(vault)
    assert "already undone" in run("undo", sid, "--vault", vault).output
    assert "nothing to undo" in run("undo", "s_01JD0000000000000000000001", "--vault",
                                    vault).output
    assert run("undo", "--all", "--vault", vault, ok=False).exit_code != 0


def test_undo_older_session_keeps_later_text(vault):
    from datetime import UTC, datetime
    _repo(vault)
    first = _session(vault, datetime(2026, 11, 10, 12, tzinfo=UTC), "correct")
    second = _session(vault, datetime(2026, 11, 11, 12, tzinfo=UTC), "correct")
    first_events = list(vault.glob(f"events/**/{first}.jsonl"))
    assert first_events and list(vault.glob(f"events/**/{second}.jsonl"))
    out = run("undo", first, "--vault", vault).output
    assert "kept later text: Today.md" in out and "concepts/two-pointers.md" in out
    assert not any(p.exists() for p in first_events)
    assert not list(vault.glob(f"sessions/*{first[-8:].lower()}.md"))
    assert {e["session"] for e in events(vault)} == {second}
    _clean(vault)
    assert "0 note(s) would change" in run("replay", "--dry-run", "--vault", vault).output


def test_undo_failure_leaves_head_untouched(vault, monkeypatch):
    from datetime import UTC, datetime

    from keepfold import render
    _repo(vault)
    first = _session(vault, datetime(2026, 11, 10, 12, tzinfo=UTC), "correct")
    _session(vault, datetime(2026, 11, 11, 12, tzinfo=UTC), "wrong")
    head = _git_out(vault, "rev-parse", "HEAD")
    monkeypatch.setattr(render, "today_md", lambda **kw: 1 / 0)
    assert run("undo", first, "--vault", vault, ok=False).exit_code != 0
    assert _git_out(vault, "rev-parse", "HEAD") == head
    _clean(vault)
    (vault / "Today.md").write_text("dirty\n", encoding="utf-8")
    r = run("undo", first, "--vault", vault, ok=False)
    assert "uncommitted" in r.output and _git_out(vault, "rev-parse", "HEAD") == head
    assert "(Today.md)" in r.output  # review r4 L5: the refusal names the files


def test_doctor_lists_id_clashes(vault):
    assert doctor.check_id_clashes(vault)[0] == "PASS"
    shutil.copy(vault / "concepts/two-pointers.md", vault / "problems/two-pointers.md")
    status, _, detail, _ = doctor.check_id_clashes(vault)
    assert status == "WARN" and "two-pointers (concept, problem)" in detail


def test_concept_add_adopts_hand_note(vault):
    (vault / "concepts/prefix-sums.md").write_bytes(
        b"---\ntitle: Prefix sums\nparent: \"[[dsa.arrays]]\"\n---\n# Prefix sums\n")
    r = run("concept", "add", "Prefix sums", "--vault", vault)
    assert "adopted concept prefix-sums" in r.output


def test_help_keeps_brackets_and_names_no_internal_docs():
    """dogfood r4 #15: Rich markup ate [[links]] and [saved]; help named design docs."""
    import typer

    def walk(cmd, path):
        yield path
        for name, sub in getattr(cmd, "commands", {}).items():
            yield from walk(sub, [*path, name])

    for path in walk(typer.main.get_command(app), []):
        out = runner.invoke(app, [*path, "--help"], terminal_width=200).output
        assert "[]" not in out and not any(w in out for w in ("PHASE", "SPEC", "§")), path
        assert "<bool>" not in out, path  # FX4: a flag takes no value
    setup = runner.invoke(app, ["setup", "--help"], terminal_width=200).output
    assert "--dry-run" in setup and "<bool>" not in setup and "<path>" in setup
    assert 'Add the [[links]] Obsidian' in run("relink", "--help").output
    assert 'edges as "[[id]]"' in runner.invoke(app, ["relink", "--help"],
                                                 terminal_width=200).output
    assert "[saved] marks" in runner.invoke(app, ["chats", "--help"],
                                            terminal_width=200).output


def test_claims_marks_sample_lines(vault):
    (vault / "me/profile.md").write_text("---\nsample: true\n---\n# Who I am\n\n## Background\n"
                                         "- Sam is a first-year student.\n", encoding="utf-8")
    (vault / "me/goals.md").write_text("# My goals\n\n## Now\n- Pass the OA.\n",
                                       encoding="utf-8")
    lines = run("claims", "--vault", vault).output.splitlines()
    assert any(ln.endswith("Sam is a first-year student.  (sample)") for ln in lines)
    assert any(ln.endswith("Pass the OA.") for ln in lines)


def test_help_lists_no_stray_why_command():  # §15.2 N1
    out = run("--help").output
    assert "why" not in out and "replay" in out


def test_chats_lists_past_chats(vault, chats_dir):
    lines = run("chats", "--vault", vault).stdout.splitlines()
    assert [ln.split()[1] for ln in lines] == ["D--proj-beta/cccc-3333",
                                                "D--proj-alpha/aaaa-1111"]
    assert lines[0].startswith("2026-11-12T08:01") and lines[0].endswith("Plan my DP revision")
    assert len(run("chats", "--since", "2026-11-11", "--vault", vault).stdout.splitlines()) == 1


def test_help_epilogue_names_real_commands():
    """review r4 S9: `keepfold --help` groups the commands; every name it lists exists."""
    import typer

    from keepfold.cli import EPILOG

    root = typer.main.get_command(app)
    names = [n for group in EPILOG.split(" · ") for n in group.split(": ", 1)[1].split(", ")]
    assert "claims [--unasked|--summary]" in names and "harvest status" in names
    for name in names:
        words, _, flags = name.partition(" [")
        cmd = root
        for part in words.split():
            assert part in getattr(cmd, "commands", {}), name
            cmd = cmd.commands[part]
        opts = {o for p in cmd.params for o in p.opts}
        assert all(f in opts for f in flags.rstrip("]").split("|") if f), name
    out = run("--help").output
    assert "About you: claims" in out and "New here? Run `keepfold app`." in out
    assert EPILOG.split("Setup: ", 1)[1].startswith("app, ")


def test_llm_start_starts_the_configured_runner(home, monkeypatch):
    from keepfold.app import wizard

    seen = []
    monkeypatch.setattr(wizard, "start", lambda r: (seen.append(r), {
        "ok": True, "state": "running", "lines": ["ran: lms server start"]})[1])
    assert "ran: lms server start" in run("llm", "start", "--runner", "lmstudio").output
    run("llm", "start")  # no config: Ollama, the recommended runner
    (home / ".keepfold").mkdir()
    (home / ".keepfold/config.yaml").write_text("localllm:\n  runner: lmstudio\n",
                                              encoding="utf-8")
    run("llm", "start")
    assert seen == ["lmstudio", "ollama", "lmstudio"]
    monkeypatch.setattr(wizard, "start", lambda r: (_ for _ in ()).throw(
        ValueError("Ollama is not installed yet (step 3).")))
    r = run("llm", "start", ok=False)
    assert r.exit_code == 1 and "not installed" in r.output


def test_old_names_are_hidden_aliases():
    """review r4 S1-S3, S8: the old names still run but no help lists them."""
    import typer

    root = typer.main.get_command(app)
    assert root.commands["saved"].hidden and root.commands["know"].hidden
    assert root.commands["eval"].commands["harvest"].hidden
    mcp = next(p for p in root.commands["export"].commands["context"].params
               if "--mcp-config" in p.opts)
    assert mcp.hidden
    top = run("--help").output
    assert not any(line.split()[1:2] in (["saved"], ["know"]) for line in top.splitlines())
    ev = run("eval", "--help").output
    assert all(f" {c} " in ev for c in ("recall", "learning", "all")) and " harvest " not in ev
    assert "--mcp-config" not in run("export", "context", "--help").output
    assert "status" in run("harvest", "--help").output
    r = run("claims", "--unasked", "--summary", ok=False)
    assert r.exit_code == 1 and "Use one of --tier" in r.output


def test_ask_prints_the_answer_sources_and_model(vault, monkeypatch):
    from keepfold import ask, localllm
    seen = []
    monkeypatch.setattr(ask, "ask", lambda v, q, private: seen.append(private) or {
        "answer": "Use two pointers [1].", "model": "ollama gemma3:2b at 127.0.0.1:11434",
        "sources": [{"n": 1, "path": "concepts/two-pointers.md", "title": "Two pointers"}]})
    out = run("ask", "how do I pair numbers?", "--vault", vault).output
    assert out.index("Use two pointers [1].") < out.index("[1] concepts/two-pointers.md")
    assert "ollama gemma3:2b" in out
    # E3: CliRunner's streams are no terminal, so --private is refused before ask runs
    r = run("ask", "x", "--private", "--vault", vault, ok=False)
    assert r.exit_code == 1 and "only in a real terminal" in r.output and seen == [False]
    from keepfold import cli as cli_mod
    monkeypatch.setattr(cli_mod, "_tty", lambda: True)
    run("ask", "x", "--private", "--vault", vault)
    assert seen == [False, True]  # private notes and browsing only with --private, at a TTY
    monkeypatch.setattr(ask, "ask", lambda v, q, private: {"error": "no_local_model",
                                                  "message": "Start Ollama (recommended)."})
    r = run("ask", "x", "--vault", vault, ok=False)
    assert r.exit_code == 1 and "Start Ollama" in r.output

    def cloud(v, q, private):
        raise ValueError(localllm._CLOUD)
    monkeypatch.setattr(ask, "ask", cloud)
    r = run("ask", "x", "--vault", vault, ok=False)
    assert r.exit_code == 1 and "localllm" in r.output and "Traceback" not in r.output


def test_browse_import_takeout_says_how_many_rows_were_skipped(vault, monkeypatch):
    from keepfold import cli, localllm, semantic
    from keepfold.browse import build
    monkeypatch.setattr(cli, "_browse_sources", lambda *a: [])
    monkeypatch.setattr(semantic, "load_embedder", lambda *a, **k: None)
    monkeypatch.setattr(localllm, "enabled", lambda: False)
    monkeypatch.setattr(build, "run", lambda *a, **k: {"takeout": 0})
    html = FIXTURES / "exports" / "takeout_html"
    out = run("browse", "import", "--takeout", html, "--dry-run", "--vault", vault).output
    assert "takeout: 3 activity rows read; 1 row skipped (unreadable date)" in out


def test_tier_on_a_chat_export_names_every_source_that_takes_one(vault):
    from keepfold.app import actions
    r = run("import", FIXTURES / "exports" / "perplexity_thread.md", "--tier", "private",
            "--vault", vault, ok=False)
    assert r.exit_code == 1 and "--tier applies to notes, calendars" in r.output
    for kind in actions.TIERED:
        assert kind in r.output, kind


# ---- uninstall ---------------------------------------------------------------------------

@pytest.fixture
def uninstall_fakes(home, monkeypatch):
    """Recorders for every step `uninstall` takes, so no test reaches the real AI-app configs, login
    entry or Run key: the disconnect step, the receiver's login switch and the app's autostart."""
    from keepfold import cli
    from keepfold.app import actions, desktop

    calls = []
    monkeypatch.setattr(cli, "disconnect", lambda: calls.append("disconnect"))
    monkeypatch.setattr(actions, "login", lambda job, root, on: calls.append(("login", on)))
    monkeypatch.setattr(desktop, "set_autostart", lambda on: calls.append(("autostart", on)))
    auto = home / "autostart" / "io.github.alenissacsam_2.keepfold.desktop"
    auto.parent.mkdir()
    auto.write_text("[Desktop Entry]\n")
    monkeypatch.setattr(desktop, "autostart_file", lambda: auto)
    return calls, auto


def _bin(home):
    b = home / ".local" / "bin"
    b.mkdir(parents=True)
    return b / "keepfold"


def test_uninstall_lists_then_does(home, uninstall_fakes):
    calls, auto = uninstall_fakes
    link = _bin(home)
    try:
        link.symlink_to(home / "elsewhere-keepfold")
    except OSError:
        pytest.skip("this system cannot make symlinks")
    out = run("uninstall", "--yes").output
    assert calls == ["disconnect", ("login", False)]
    assert not auto.exists() and not link.is_symlink()
    assert "kept" in out and "vault" in out and "~/.keepfold" in out


def test_uninstall_keeps_real_file(home, uninstall_fakes):
    real = _bin(home)
    real.write_text("not ours")
    run("uninstall", "--yes")
    assert real.read_text() == "not ours"


def test_uninstall_without_yes_asks_and_no_does_nothing(home, uninstall_fakes):
    calls, auto = uninstall_fakes
    r = runner.invoke(app, ["uninstall"], input="n\n")
    assert r.exit_code == 0, r.output
    assert "Remove Keepfold from every AI app" in r.output and "Nothing changed." in r.output
    assert calls == [] and auto.exists()


def test_uninstall_on_windows_turns_off_the_apps_run_value(home, uninstall_fakes, monkeypatch):
    from keepfold import update
    from keepfold.app import desktop
    calls, _ = uninstall_fakes
    monkeypatch.setattr(desktop, "autostart_file", lambda: None)
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(update, "mode", lambda: "installer")
    out = run("uninstall", "--yes").output
    assert calls == ["disconnect", ("login", False), ("autostart", False)]
    assert "Use Settings > Apps > Keepfold." in out


def test_uninstall_mac_app_unregisters_its_login_item(home, uninstall_fakes, monkeypatch):
    """The Mac app's Start at login is SMAppService, not a file: set_autostart(False) ends it
    (and removes an old LaunchAgent)."""
    from keepfold import update
    calls, _ = uninstall_fakes
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(update, "mode", lambda: "app")
    out = run("uninstall", "--yes").output
    assert calls == ["disconnect", ("login", False), ("autostart", False)]
    assert "no longer starts at login" in out and "Move Keepfold to the Trash" in out


def test_icloud_documents_warns(monkeypatch, tmp_path_factory):
    """Port T17: on a Mac with iCloud Desktop & Documents on, a vault in ~/Documents or
    ~/Desktop is synced though its path never says iCloud."""
    import sys
    home = tmp_path_factory.mktemp("h")  # tmp_path would hold this test's name, "icloud"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    vault = home / "Documents" / "v"
    vault.mkdir(parents=True)
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert doctor.check_onedrive(vault)[0] == "PASS"
    (home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Documents").mkdir(
        parents=True)
    status, _, detail, _ = doctor.check_onedrive(vault)
    assert status == "WARN" and "iCloud (Desktop & Documents)" in detail
    assert doctor.check_onedrive(home / "Desktop" / "v")[0] == "WARN"
    assert doctor.check_onedrive(home / "Keepfold")[0] == "PASS"
    monkeypatch.setattr(sys, "platform", "linux")
    assert doctor.check_onedrive(vault)[0] == "PASS"


def test_app_hint_mac_app_mode(monkeypatch):
    """Port T17: the frozen app says where to open it per OS, never the Start menu off Windows."""
    import sys

    from keepfold import update
    monkeypatch.setattr(update, "mode", lambda: "app")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert doctor.app_hint() == "open Keepfold from Applications or Launchpad"
    assert "Applications or Launchpad" in doctor.check_ai_apps()[3]
    assert "Applications or Launchpad" in doctor.check_search_model()[3]
    monkeypatch.setattr(sys, "platform", "linux")
    assert doctor.app_hint() == "open Keepfold from the app menu"
    monkeypatch.setattr(update, "mode", lambda: "installer")
    monkeypatch.setattr(sys, "platform", "win32")
    assert doctor.app_hint() == "open Keepfold from the Start menu"
    assert doctor.check_ai_apps()[3] == ("Open Keepfold from the Start menu and tick an app "
                                         "under Connect AI apps.")
