"""2.0 d, D2 L5: the wider tier 0 of fact_harvest. Every statement of a clause, style orders
into me/preferences ## Communication, moves and switches, and decision and project phrases
handed back as proposals (never saved). No model runs or loads."""

import json
from pathlib import Path

import pytest
from conftest import events
from test_safety_net import NOW, chat

from keepfold import claims
from keepfold import fact_harvest as fh
from keepfold.events import EventLog
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")
SENTENCES = json.loads((Path(__file__).parent / "fixtures" / "loop" / "tier0.json")
                       .read_text("utf-8"))["sentences"]


def facts(*turns, proposals=None):
    return [(page, fact) for page, fact, _ in fh.tier0(list(turns), proposals)]


def test_tier0_gives_a_line_for_at_least_55_percent_of_the_d2_sentences():
    # D2 measured 9 of these 25 (36%); proposals do not count here, only saved lines
    lines = sum(bool(fh.tier0([s])) for s in SENTENCES)
    assert lines / len(SENTENCES) >= 0.55, lines


@pytest.mark.parametrize("turn,want", [
    ("I live in Kochi and I'm a final-year CS student at CUSAT.",
     [("profile", "Lives in Kochi"), ("profile", "Is a final-year CS student at CUSAT")]),
    ("I eat eggs but I don't eat meat.",
     [("preferences", "Eats eggs"), ("preferences", "Doesn't eat meat")]),
    ("Keep answers short, no emojis please.",
     [("preferences", "Keep answers short"), ("preferences", "No emojis")]),
    ("From now on, always write Python examples with type hints.",
     [("preferences", "Always write Python examples with type hints")]),
    ("Please don't use emojis.", [("preferences", "Don't use emojis")]),
    ("I moved to Bangalore last month for my internship.", [("profile", "Moved to Bangalore")]),
    ("I've switched to C++ for DSA now, Java was too verbose.",
     [("preferences", "Switched to C++ for DSA")]),
    ("I switched from Java to Kotlin for Android work.",
     [("preferences", "Switched from Java to Kotlin for Android work")]),
    ("I usually study late at night.", [("profile", "Studies late at night")]),
    ("I hate long answers.", [("preferences", "Hates long answers")]),
    ("I'm 21 years old.", [("profile", "Is 21 years old")]),
    ("I have a dog named Bruno.", [("profile", "Has a dog named Bruno")]),
    ("My exam is on 12 December.", [("goals", "Exam is on 12 December")]),
])
def test_tier0_reads_the_wider_templates(turn, want):
    assert facts(turn) == want


@pytest.mark.parametrize("turn", [
    # R7: someone else, hypotheticals, one-off requests
    "My friend Ravi lives in Pune and works at Infosys.", "Jonas thinks I'm a perfectionist.",
    "My colleague switched to Rust from Go.", "Ravi moved to Berlin last year for his job.",
    "My sister keeps answers short, she never uses emojis.",
    "If we ever moved to Spark, I'd learn Scala.", "Suppose I moved to Berlin. Keep answers short.",
    "Imagine I were a doctor. I work at a hospital and I'm a surgeon.",
    "Make my portfolio intro shorter.", "Keep this answer short.", "Keep it under 200 words.",
    "Don't use emojis in this email.", "Never mind, keep going.",
    "No explanations needed, just fix it.", "Always cite the source for the second answer.",
    "Can you keep answers short?", "Keep answers short today.",
    # no place, no named pet, decisions and projects (proposals only)
    "I've moved to the new office, it's nicer.", "I have a cat allergy, so no cat videos.",
    "I've decided to go with PostgreSQL instead of MongoDB.",
    "I'm building a Flutter app called MessMate.",
])
def test_tier0_wide_finds_nothing(turn):
    assert facts(turn) == []


def test_decisions_and_projects_come_back_as_proposals_only():
    got: list = []
    turns = ["I've decided to go with PostgreSQL instead of MongoDB, because I need joins.",
             "I'm building a Flutter app called MessMate.", "We're going with Firebase for auth.",
             "My colleague switched to Rust from Go.", "Should we switch to Rust?",
             "I moved to Pune last month."]  # a line, so no proposal
    assert facts(*turns, proposals=got) == [("profile", "Moved to Pune")]
    assert got == [
        ("decision", "decided", "I've decided to go with PostgreSQL instead of MongoDB"),
        ("project", "building", "I'm building a Flutter app called MessMate."),
        ("decision", "going with", "We're going with Firebase for auth.")]
    capped: list = []
    fh.tier0(turns * 2 + ["Let's use Tailwind for the frontend."], capped)
    assert len(capped) == fh.PER_CHAT


def test_a_style_line_is_checked_like_any_other():
    assert facts("Never use emojis, sk-" + "a" * 24) == [("preferences", "Never use emojis")]
    assert facts("Always use emojis sk-" + "a" * 24) == []  # a secret in the line
    assert fh._style("Always write examples with " + "x" * fh.LINE_CHARS) is None


def test_a_pass_saves_style_lines_under_communication_and_never_a_proposal(root):
    chat(root, "c1", "Keep answers short, no emojis please.",
         "I've decided to go with PostgreSQL instead of MongoDB.", "I hate long answers.")
    pages = {p for d in ("decisions", "projects") for p in (root / d).glob("*.md")}
    out = fh.run(Vault(root), None, now=NOW)
    assert out["tier0"] == 3 and out["saved"] == 3, out
    v = Vault(root)
    rows = {r["text"]: r for r in claims.tiers(v, EventLog(v).iter_events())
            if r["page"] == "preferences" and r["status"] == "live"}
    assert rows["Keep answers short"]["section"] == "Communication"
    assert rows["No emojis"]["section"] == "Communication"
    assert rows["Hates long answers"]["section"] == "Personal"
    assert rows["Keep answers short"]["tier"] == "agent"  # a guess, served (ask)
    assert {p for d in ("decisions", "projects") for p in (root / d).glob("*.md")} == pages
    assert not any("PostgreSQL" in json.dumps(e) for e in events(root) if e["kind"] == "change")
