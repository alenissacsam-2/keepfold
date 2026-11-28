"""`keepfold eval loop` (A20): D2's core-loop study as a command. The made-up corpus is
tests/fixtures/loop/; the run happens in a temp home and a temp vault."""

import os
from pathlib import Path

import pytest
from conftest import FIXTURES
from typer.testing import CliRunner

from keepfold import loop_eval
from keepfold.cli import app


@pytest.mark.slow
def test_wave_a_bars():
    """The whole run (about 40 s). Today's numbers (D2 §2, raised by A21) as floors: A22 raises
    them (Related 7 of 9 in the hook), a later change may not lower them."""
    n = loop_eval.run()
    out = loop_eval.summary(n)
    for want in ("Items lost of 26", "Decisions and projects proposed, of 6",
                 "Guesses served when they fit", "Related pages, of 9",
                 "Silent retirements", "must-serve recall"):
        assert want in out
    assert (n["items"], n["decisions_projects"], n["related_expected"], n["must_total"]) == (
        26, 6, 9, 37)
    # me_line 6 -> 5 with A11 (D-15): a //tag in an imported chat only proposes (Review), so it
    # is no saved line about you; it is not lost either
    assert n["lost"] <= 4 and n["me_line"] >= 5 and n["text_only"] >= 16
    assert n["decisions_projects_proposed"] >= 0  # nothing yet: a later fix raises it
    # A21's slot matches by prompt words; the corpus's fits share none (they need meaning)
    assert n["guess_served"] >= 0
    assert n["related_hook"] >= 0 and n["related_mcp"] >= 7  # A22: hook 7 of 9
    assert n["silent_retirements"] == 0  # A21: a quoted guess asks Still true? instead
    assert n["must_hook"] >= 26 and n["must_mcp"] >= 27 and n["must_hook_grown"] >= 19  # A21
    assert n["tier0_hits"] >= 9 and n["found_5"] >= 12
    assert n["negatives_saved"] == 0  # the roleplay and the "second version" reaction stay out


def test_the_fixtures_hold_the_study():
    corpus = loop_eval.load("corpus.json")
    assert len(corpus["gold"]) == 26 and len(corpus["chats"]) == 20 and len(corpus["export"]) == 1
    spec = loop_eval.load("prime.json")
    assert len(spec["cases"]) == 20 and sum(len(c["related"]) for c in spec["cases"]) == 9
    assert loop_eval.FIXTURES == FIXTURES / "loop"


def test_missing_fixtures_are_a_clean_refusal(tmp_path):
    with pytest.raises(loop_eval.LoopRefused, match="repo checkout"):
        loop_eval.load("corpus.json", tmp_path)


def test_refuses_the_real_home(tmp_path, monkeypatch):
    real = Path.home()
    with pytest.raises(loop_eval.LoopRefused, match="real home"), loop_eval.sandbox(real):
        pytest.fail("ran in the real home")
    other = tmp_path / "profile"
    other.mkdir()
    monkeypatch.setenv("USERPROFILE", str(other))  # the env's own home counts as real too
    with pytest.raises(loop_eval.LoopRefused), loop_eval.sandbox(other):
        pytest.fail("ran in the home the environment names")
    assert not any(other.iterdir())


def test_sandbox_moves_home_and_puts_everything_back(monkeypatch):
    before = (Path.home(), os.environ.get("USERPROFILE"), os.environ.get("KEEPFOLD_CHATS_DIR"))
    monkeypatch.setenv("KEEPFOLD_VAULT", "keep-me")
    with loop_eval.sandbox() as tmp:
        assert Path.home().is_relative_to(tmp)
        assert "KEEPFOLD_VAULT" not in os.environ
        assert Path(os.environ["KEEPFOLD_CHATS_DIR"]).is_relative_to(tmp)
    assert not tmp.exists()
    assert (Path.home(), os.environ.get("USERPROFILE"),
            os.environ.get("KEEPFOLD_CHATS_DIR")) == before
    assert os.environ["KEEPFOLD_VAULT"] == "keep-me"


def test_fake_embedder_is_deterministic_and_loads_nothing():
    e = loop_eval.FakeEmbed()
    a, b = e(["I live in Kochi", "I live in Kochi"])
    assert (a == b).all() and abs(float((a * a).sum()) - 1) < 1e-5
    assert float(e(["I live in Kochi"])[0] @ e(["Kochi is home"])[0]) > 0
    assert not e([""]).any()


def test_command_prints_the_numbers(monkeypatch):
    got = {k: 1 for k in ("items", "lost", "me_line", "text_only", "decisions_projects",
                          "decisions_projects_proposed", "guess_fit", "guess_served",
                          "related_expected", "related_hook", "related_mcp",
                          "silent_retirements", "must_total", "must_hook", "must_mcp",
                          "must_hook_grown", "tier0_hits", "tier0_of", "found_5", "found_asked")}
    monkeypatch.setattr(loop_eval, "run", lambda: got)
    r = CliRunner().invoke(app, ["eval", "loop"])
    assert r.exit_code == 0 and "Items lost of 1: 1" in r.output
    r = CliRunner().invoke(app, ["eval", "loop", "--json"])
    assert '"lost": 1' in r.output
    monkeypatch.setattr(loop_eval, "run", lambda: (_ for _ in ()).throw(
        loop_eval.LoopRefused("your real home")))
    r = CliRunner().invoke(app, ["eval", "loop"])
    assert r.exit_code == 1 and "your real home" in r.output
