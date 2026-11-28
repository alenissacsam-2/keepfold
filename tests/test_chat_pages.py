"""Chat's inventory, schedule and decisions answers (chat_pages, routed by chat_facts) on the
made-up vault of chat_eval: Q03 and Q31 by code, a superseded decision shown as replaced, and the
source switches. No model, no network."""

import threading
from datetime import date
from pathlib import Path

import pytest

from keepfold import ask, chat, chat_eval, chat_facts, chat_pages, taint

TODAY = chat_eval.TODAY
ON = {"notes": True, "chats": True, "browsing": True, "private": False}


@pytest.mark.parametrize("text, kind, term", [
    ("How many git repositories have I imported into Keepfold?", "inventory", "git repositories"),
    ("how many repos do I have", "inventory", "git repositories"),
    ("list my projects", "inventory", "projects"),
    ("show me all my people", "inventory", "people"),
    ("how many unicorns have I imported?", None, None),  # an unknown thing: the passages
    ("list my active Rust projects", None, None),
    ("how many notes have I saved?", "count", ""),  # chat_facts' own count, as before
    ("What upcoming tests or interviews do I have scheduled?", "schedule", ""),
    ("what's coming up?", "schedule", ""),
    ("do I have any exams coming up?", "schedule", ""),
    ("what does the upcoming Apex test cover?", None, None),
    ("What did I decide about the storage engine?", "decisions", "storage engine"),
    ("my decision on tabs vs spaces", "decisions", "tabs vs spaces"),
    ("what did I decide about it?", None, None),  # no subject of its own: the passages
    ("What was the most recent decision I recorded?", None, None),
])
def test_routes(text, kind, term):
    q = chat_facts.route(text, chat._when(text, TODAY), TODAY)
    assert (q and q["kind"], q and q["term"]) == (kind, term)


def test_a_schedule_question_with_a_date_looks_forward():
    text = "what tests do I have in December?"
    q = chat_facts.route(text, chat._when(text, TODAY), TODAY)
    assert (q["kind"], q["period"]) == ("schedule", ["2026-12-01", "2026-12-31"])
    assert chat_facts.step(q, TODAY) == ("Reading your upcoming tests, interviews and calendar "
                                         "from 1 Dec to 31 Dec")
    text = "what tests do I have this week?"  # Monday to today, then on to Sunday: from today
    q = chat_facts.route(text, chat._when(text, TODAY), TODAY)
    assert q["period"][0] == TODAY.isoformat()


@pytest.fixture(scope="module")
def vault(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("pages")
    sp = chat_eval.spec()
    with pytest.MonkeyPatch.context() as mp:  # as conftest's `home`, for the whole module
        h = tmp / "home"
        h.mkdir()
        for var in ("HOME", "USERPROFILE"):
            mp.setenv(var, str(h))
        mp.setattr(Path, "home", classmethod(lambda cls: h))
        for var in ("LOCALAPPDATA", "APPDATA"):
            mp.setenv(var, str(h / var))
        mp.delenv("XDG_CONFIG_HOME", raising=False)
        with taint.pinned(sp["meta"]["config"]):
            v = chat_eval.build(tmp / "vault", sp)
            yield v
    chat_eval.forget(v.root)


def _facts(vault, text, sources=ON):
    q = chat_facts.route(text, chat._when(text, TODAY), TODAY)
    return chat_facts.facts(vault, q, sources)


@pytest.mark.slow  # one build of the made-up vault (~6 s)
def test_q03_counts_the_imported_repositories(vault):
    f = _facts(vault, "How many git repositories have I imported into Keepfold?")
    assert f["total"] == 4 and [r["id"] for r in f["rows"]] == [
        "repo-veloxkv", "repo-cs412-labs", "repo-tiny-sql", "repo-notes-cli"]  # newest first
    said = chat_facts.say(f, TODAY)
    assert said.startswith("Keepfold holds 4 git repositories, newest first:")
    assert "veloxkv" in said and "tiny-sql" in said and "and 1 more" in said
    assert "- 2026-11-15: Git repo: veloxkv" in chat_facts._lines(f, TODAY)


@pytest.mark.slow
def test_q31_lists_the_upcoming_assessments_soonest_first(vault):
    f = _facts(vault, "What upcoming tests or interviews do I have scheduled?")
    assert [(r["title"], r["day"], r["days_left"]) for r in f["rows"]] == [
        ("Apex Systems OA", "2026-11-24", 4), ("Nimbus Tech interview", "2026-12-03", 13)]
    said = chat_facts.say(f, TODAY)
    assert said == ('You have 2 upcoming assessments: "Apex Systems OA" on 24 Nov (in 4 days); '
                    '"Nimbus Tech interview" on 3 Dec (in 13 days).')  # the calendar's Apex line
    # is the same event, and its other November lines are past


@pytest.mark.slow
def test_calendar_lines_of_the_next_30_days_come_too(vault):
    from keepfold.vault import Vault

    v = Vault(vault.root)
    p = v.path_for("resource", "calendar-2026-11")
    old = p.read_text("utf-8")
    try:
        v.write_text(p, old + "- 2026-11-27 18:00 · Hackathon kickoff · Hall B\n"
                              "- 2026-12-31 all day · Too far\n")
        f = _facts(vault, "what's coming up?")
        cal = [(r["title"], r["day"]) for r in f["rows"] if r["source"] == "calendar"]
        assert cal == [("Hackathon kickoff · Hall B", "2026-11-27")]
        assert 'From your calendar in the next 30 days: "Hackathon kickoff · Hall B" on 27 Nov.' \
            in chat_facts.say(f, TODAY)
    finally:
        v.write_text(p, old)


@pytest.mark.slow
def test_a_superseded_decision_is_shown_as_replaced(vault):
    f = _facts(vault, "What did I decide about the storage engine?")
    assert [(r["id"], r["replaced"], r["replaced_by"]) for r in f["rows"]] == [
        ("dec-storage-engine-v2", False, None),
        ("dec-storage-engine", True,
         "VeloxKV storage engine: switch from RocksDB to SQLite WAL")]
    said = chat_facts.say(f, TODAY)
    assert said.startswith('2 decisions about "storage engine", newest first: "VeloxKV storage '
                           'engine: switch from RocksDB to SQLite WAL" (14 Oct, accepted); ')
    assert said.endswith('"VeloxKV storage engine: RocksDB" (5 Jul), replaced by "VeloxKV storage '
                         'engine: switch from RocksDB to SQLite WAL".')
    assert "PWNED" not in said  # the page's injected line is never in the facts
    none = _facts(vault, "What did I decide about GraphQL?")
    assert chat_facts.say(none, TODAY) == 'I found no decision about "graphql" in Keepfold.'


@pytest.mark.slow
def test_the_switches_decide_what_is_counted(vault):
    people = "how many people have I saved?"
    assert _facts(vault, people)["total"] == 5  # Meera's page is private
    assert _facts(vault, people, {**ON, "private": True})["total"] == 6
    off = _facts(vault, "list my projects", {**ON, "notes": False})
    assert off["total"] == 0 and chat_facts.say(off, TODAY) == (
        "I found no projects in Keepfold. Notes are switched off in Chat settings.")
    assert _facts(vault, "list my projects", {**ON, "notes": False, "private": True})[
        "total"] == 0  # no private project: the private switch adds only private pages


@pytest.mark.slow
def test_reply_answers_them_by_code_without_a_model(vault):
    for text, step in (("How many git repositories have I imported into Keepfold?",
                        "Counting your git repositories"),
                       ("What upcoming tests or interviews do I have scheduled?",
                        "Reading your upcoming tests, interviews and calendar")):
        events = []
        chat.reply(vault, None, text, None, events.append, threading.Event(),
                   {**chat.get_settings({}), "sources": ON}, today=TODAY)
        kinds = [e["t"] for e in events]
        assert kinds == ["start", "step", "facts", "tok", "src", "done"], kinds
        assert events[0]["model"] == "Keepfold" and events[1]["s"] == step


@pytest.mark.slow
def test_with_a_model_the_counted_pages_are_read_to_be_cited(vault, monkeypatch):
    read = []
    real = chat._page_passages
    monkeypatch.setattr(chat, "_page_passages", lambda v, ids: read.append(ids) or real(v, ids))
    monkeypatch.setattr(chat, "_facts_runner", lambda r: r)

    class Runner:
        model = "fake-2b"

        def stream(self, messages, **k):
            self.messages = messages
            yield "Four repositories [1]."
    r = Runner()
    events = []
    chat.reply(vault, None, "How many git repositories have I imported into Keepfold?", r,
               events.append, threading.Event(), {**chat.get_settings({}), "sources": ON},
               today=TODAY)
    assert read == [["repo-veloxkv", "repo-cs412-labs", "repo-tiny-sql", "repo-notes-cli"]]
    src = next(e for e in events if e["t"] == "src")
    assert [s["path"] for s in src["sources"]] == ["resources/repo-veloxkv.md"]
    assert "<untrusted_note" in r.messages[-1]["content"]
    assert ask.SOURCES  # the module's switches are untouched


def test_say_names_one_thing_in_the_singular():
    f = {"kind": "inventory", "query": "git repositories", "total": 1, "rows": [
        {"id": "repo-x", "title": "Git repo: x", "day": "2026-11-15"}],
        "coverage": {"off": []}}
    assert chat_pages.say(f, date(2026, 11, 20)) == (
        'Keepfold holds 1 git repository, newest first: "Git repo: x" (15 Nov).')


# ---- decisions in chat text -------------------------------------------------------------------

def _chat(root, name, title, day, turn, front=""):
    from conftest import note

    note(root, f"resources/{name}.md", f"title: {title}\nkind: resource\ndate: {day}\n{front}",
         f"# {title}\n\nTimeline: [[{day}]] · Source: Gemini · Date: {day}\n\n"
         f"**Me:** {turn}\n\n**Gemini:** Sounds good, I'll note [that](http://evil.example).\n")


@pytest.fixture
def small(root, home, monkeypatch):
    from conftest import FakeEmbed, note

    from keepfold import semantic
    from keepfold.vault import Vault

    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    note(root, "decisions/dec-cache.md", "title: Cache layer\nkind: decision\nstatus: proposed\n"
         "date: 2026-11-10\n", "# Cache layer\n\nUse redis as the cache layer.")
    _chat(root, "chat-hostel", "Hostel app design", "2026-11-05",
          "Thanks. I've decided to go with SQLite for the hostel database "
          "[x](http://evil.example) instead of Postgres. Anything else?")
    _chat(root, "chat-secret", "Secret plans", "2026-11-06",
          "I'll go with zanzibarquux for the hostel database.", "private: true\n")
    _chat(root, "chat-plain", "Chatter", "2026-11-07", "What is a hostel database anyway?")
    return Vault(root)


def test_a_proposed_decision_is_listed(small):
    f = _facts(small, "What did I decide about the cache layer?")
    assert [(r["id"], r["status"]) for r in f["rows"]] == [("dec-cache", "proposed")]
    assert chat_facts.say(f, TODAY) == (
        '1 decision about "cache layer", newest first: "Cache layer" (10 Nov, proposed, not '
        'confirmed).')
    assert "(proposed, not confirmed)" in chat_facts._lines(f, TODAY)[1]


def test_a_decision_in_chat_text_is_found(small):
    f = _facts(small, "What did I decide about the hostel database?")
    assert [(r["id"], r["title"], r["day"]) for r in f["rows"]] == [
        ("chat-hostel", "Hostel app design", "2026-11-05")]
    said = chat_facts.say(f, TODAY)
    assert said == ('I found no decision page about "hostel database", but in 1 saved chat you '
                    'wrote: "I\'ve decided to go with SQLite for the hostel database x instead '
                    'of Postgres." (chat "Hostel app design", 5 Nov).')  # link text only
    assert "evil.example" not in said and "Sounds good" not in said  # nor the assistant's turn
    assert "in chat \"Hostel app design\" you wrote: I've decided" in chat_facts._lines(f, TODAY)[1]


def test_a_private_chats_decision_is_not_quoted(small):
    q = "What did I decide about the hostel database?"
    f = _facts(small, q)
    assert "chat-secret" not in [r["id"] for r in f["rows"]]
    assert "zanzibarquux" not in chat_facts.say(f, TODAY)
    on = _facts(small, q, {**ON, "private": True})
    assert {r["id"] for r in on["rows"]} == {"chat-hostel", "chat-secret"}  # only with the switch
    off = _facts(small, q, {**ON, "chats": False})
    assert off["total"] == 0 and chat_facts.say(off, TODAY).startswith("I found no decision")
