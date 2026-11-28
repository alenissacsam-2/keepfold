"""D20 C1-C3 (ask.check_cites): a model's [n] checked in code against the passages it read,
on R9's recorded answers (tests/fixtures/chat_replay, made-up vault) and small cases; and
Chat running it (the corrected words sent as a text event; M3: only [n] is asked again)."""

import json
from pathlib import Path

import pytest
import test_chat
from test_chat import Silent, Stream, _turn

from keepfold import ask, chat, chat_eval

pytestmark = pytest.mark.usefixtures("home")
vault = test_chat.vault  # its fixture
REPLAY = Path(__file__).parent / "fixtures" / "chat_replay"


def _row(report: str, qid: str) -> tuple[str, list[dict], str, bool]:
    """(answer, the passages read, the question, a page answer) of R9's `qid` in `report`."""
    table = json.loads((REPLAY / "passages.json").read_text(encoding="utf-8"))
    row = next(q for q in json.loads((REPLAY / f"{report}.json").read_text(encoding="utf-8"))[
        "questions"] if q["id"] == qid)
    found = []
    for p in (table[k] for k in row["served"]):
        if p["path"] not in {f["path"] for f in found}:
            found.append(p)
    q = next(q for q in chat_eval.spec()["questions"] if q["id"] == qid)
    return row["answer"], found, q["question"], (row.get("facts") or {}).get("kind") in (
        "inventory", "schedule", "decisions")


@pytest.mark.parametrize("report,qid,want", [
    ("gpu1", "Q03", ["repo-veloxkv", "repo-cs412-labs", "repo-tiny-sql", "repo-notes-cli"]),  # C2
    ("gpu1", "Q31", ["apex-oa", "nimbus-interview"]),  # C2: a schedule cites the pages it read
    ("gpu-norerank", "Q06", []),  # C1: a facts answer that read no passage cites none
    ("gpu1", "Q07", []),
    ("gpu2", "Q15", ["2026-11-15", "res-hl-ddia-ch7"]),  # C3: no [n]; its words are in [1], [2]
    ("gpu-norerank", "Q42", ["oa-prep-plan"]),  # C3: a quote of [4] cited as [1]
    ("gpu2", "Q43", ["res-pdf-vr-revisited"]),  # a quote of [3] cited as [2]
    ("gpu1", "Q44", ["preferences"]),  # the Pipenv line of [1] cited as [3]
    ("gpu1", "Q45", ["preferences"]),  # cited [1] and [4]; its words are in [3]
    ("gpu1", "Q57", ["wal-checkpoints", "rollup-2026-w45"]),  # a right quote with no [n]
    ("gpu2", "Q37", ["private-project-kestrel", "rollup-2026-w38-private"]),  # right: kept
    ("gpu1", "Q59", ["repo-veloxkv"]),  # a computed "28 more" is not flagged or moved
])
def test_check_cites_on_r9_answers(report, qid, want):
    answer, found, question, pages = _row(report, qid)
    fixed, cited = ask.check_cites(answer, found, question, pages=pages)
    assert [chat_eval._id(found[n - 1]["path"]) for n in sorted(cited)] == want
    assert ask.cites(fixed) <= cited  # the words carry no number outside the sources
    assert ask._MARK.sub("", fixed) == ask._MARK.sub("", answer)  # only the numbers move


def test_r9_rewrites_move_the_numbers_in_the_words():
    answer, found, question, _ = _row("gpu1", "Q45")
    fixed, _ = ask.check_cites(answer, found, question)
    assert fixed == ("In June 2026, you wanted code explained with a **minimal working diff "
                     "first**, followed by a **3-bullet explanation** [3]. You also preferred "
                     "**verbose line-by-line walkthroughs** before seeing the code [3].")
    answer, found, question, _ = _row("gpu-norerank", "Q06")
    assert ask.check_cites(answer, found, question)[0] == (
        "You last reviewed B-Trees on 2026-11-12 at 20:30. This occurred during a study event "
        "titled \"B-Trees\".")
    answer, found, question, _ = _row("gpu2", "Q37")  # "4.1" is not a sentence end
    assert ask.check_cites(answer, found, question)[0] == answer


def _p(title, text):
    return {"path": f"notes/{title.lower()}.md", "title": title, "text": text}


FOUND = [_p("Pistons", "Pistons move inside the cylinder bore of the engine block."),
         _p("Gearbox", "The gearbox changes torque ratios through meshed helical gears."),
         _p("Brakes", "Disc brakes clamp the rotor with hydraulic calipers.")]


@pytest.mark.parametrize("answer,want,cited", [
    ("It moved [9].", "It moved.", set()),  # C1: no passage 9
    ("It moved [0].", "It moved.", set()),
    ("Pistons move in the bore [9].", "Pistons move in the bore [1].", {1}),  # and C3
    ("Pistons move inside the cylinder bore [1].", "Pistons move inside the cylinder bore [1].",
     {1}),  # right: untouched
    ("Helical gears change torque ratios [3].", "Helical gears change torque ratios [2].", {2}),
    ("Hydraulic calipers clamp the rotor.", "Hydraulic calipers clamp the rotor.", {3}),  # no mark
    ("It works well [2].", "It works well [2].", {2}),  # no telling words: the model's stands
    ("[1] Pistons. [2] Gears.", "[1] Pistons. [2] Gears.", {1, 2}),
    ('As noted, "the gearbox changes torque ratios" [1].',
     'As noted, "the gearbox changes torque ratios" [2].', {2}),  # C3: the quote's passage
])
def test_check_cites_small_cases(answer, want, cited):
    assert ask.check_cites(answer, FOUND, "How does my car work?") == (want, cited)


def test_question_words_are_no_evidence_and_nothing_read_keeps_nothing():
    # the question's own words ("cylinder bore") do not move a cite to the passage that has them
    assert ask.check_cites("The cylinder bore [2].", FOUND, "what is the cylinder bore?") == (
        "The cylinder bore [2].", {2})
    assert ask.check_cites("No sources here.", [], "?") == ("No sources here.", set())
    assert ask.check_cites("Pages [1].", FOUND, "?", pages=True)[1] == {1}  # cited: kept
    assert ask.check_cites("Four pages.", FOUND, "?", pages=True) == ("Four pages.", {1, 2, 3})


# ---- in Chat ------------------------------------------------------------------------------------

def test_chat_sends_the_corrected_words_and_saves_them(vault):
    s = Stream("openai", ["Pistons drive the crankshaft [7]."])
    try:
        cid, ev = _turn(vault, None, "car motor", s.runner())
    finally:
        s.close()
    texts = [e["s"] for e in ev if e["t"] == "text"]
    assert texts[-1] == "Pistons drive the crankshaft."  # C1: no passage 7 was read
    assert ev[-2]["t"] == "src" and ev[-2]["sources"] == []
    assert chat.load(vault, cid)[-1]["text"] == "Pistons drive the crankshaft."


def test_an_answer_of_only_citations_is_asked_once_more(vault):  # D20 M3 (Q47's "[3]")
    s = Silent(["[1]"], ["Pistons [1]."])
    try:
        cid, ev = _turn(vault, None, "car motor", s.runner())
    finally:
        s.close()
    assert len(s.bodies) == 2
    assert "gemma3:2b said nothing; asking once more" in [e["s"] for e in ev if e["t"] == "step"]
    assert chat.load(vault, cid)[-1]["text"] == "Pistons [1]."
