"""Chat's typed data answers (`chat_facts`): the routing rules, the facts of each kind on a
synthetic vault with dated notes, saved AI chats, journal lines, browsing visits and study
events, the private switch, the stream's event order through `chat.reply` with a fake loopback
runner, and the speed on a 5k-page vault. Made-up data only; no model."""

import json
import random
import threading
import time
from datetime import UTC, date, datetime

import pytest
from conftest import FakeEmbed, note
from test_chat import ALL_ON, Stream, _study_events

from keepfold import chat, chat_facts, semantic
from keepfold.browse import store
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")
TODAY = date(2026, 11, 18)  # a Wednesday
OFF = dict(ALL_ON, private=False)


def _route(q):
    return chat_facts.route(q, chat._when(q, TODAY), TODAY)


@pytest.mark.parametrize("q,kind,term", [
    ("How many times did I visit github?", "count", "github"),
    ("how often do I use git", "count", "git"),
    ("how many chats about transformers this year?", "count", "transformers"),
    ("How many chats about transformers did I have?", "count", "transformers"),
    ("how many times have I searched for machine learning in October", "count",
     "machine learning"),
    ("how many days did I visit youtube last month?", "count", "youtube"),
    ("how many chats did I have last week?", "count", ""),
    ("When did I first visit GitHub?", "first", "github"),
    ("when did I start learning rust?", "first", "rust"),
    ("when was the last time I studied heaps?", "last", "heaps"),
    ("when did I last talk about kubernetes", "last", "kubernetes"),
    ("timeline of October", "timeline", ""),
    ("show my timeline", "timeline", ""),
    ("what did I do each day last week?", "timeline", ""),
    ("what did I do in March 2024?", "timeline", ""),  # older than the digest's year
    ("tell me everything about rust", "about", "rust"),
    ("What do I know about machine learning?", "about", "machine learning"),
    ("everything I have on dynamic programming", "about", "dynamic programming"),
    ("rust vs go", "compare", "rust vs go"),
    ("What's the difference between heaps vs stacks?", "compare", "heaps vs stacks"),
    ("compare my notes on B-Trees with LSM trees", "compare", "b trees vs lsm trees"),
    ("Compare kubernetes and helm", "compare", "kubernetes vs helm"),
    ("what am I missing about databases?", "gaps", "databases"),
    ("What else am I missing on distributed systems", "gaps", "distributed systems"),
])
def test_typed_questions_are_routed(q, kind, term):
    got = _route(q)
    assert got is not None and (got["kind"], got["term"]) == (kind, term), got


@pytest.mark.parametrize("q", [
    "What is kubernetes?", "how many bits are in a byte?", "how many edges does a tree have?",
    "how many notes are in a C major chord?", "how often should I revise graphs?",
    "how many times should I practise?", "how often do I need to review heaps?",
    "when did the first world war start?", "when was the first computer built?",
    "tell me about rust", "what do you know about rust?", "how many times did I?",
    "when did I first", "summarize this article", "what happened on 11 September 2001?",
    "which sites did I visit yesterday?", "what did I study yesterday?",
    "how many times will I visit github tomorrow?",
    "should I use postgres vs mysql?", "compare it and that", "rust vs rust",
    "what am I missing?", "what is missing from this list?",
    "how many notes in 0000?", "timeline of December 9999",  # audit 21 Nov C11: no crash
])
def test_ordinary_questions_go_to_the_passages(q):
    assert _route(q) is None


def test_focus_and_period():
    q = _route("how many chats did I have last week?")
    assert (q["focus"], q["lo"], q["hi"]) == ("chats", "2026-11-09", "2026-11-15")
    assert _route("how many times did I visit github")["focus"] == "browsing"
    assert _route("how many times did I mention github")["focus"] is None
    assert _route("how many days did I visit youtube last month?")["unit"] == "days"
    assert _route("show my timeline")["lo"] == "2026-11-12"  # the last 7 days
    assert _route("timeline of October")["unit"] == "week"
    assert _route("what did I do each day in October")["unit"] == "day"
    assert _route("how often do I use git this month")["hi"] == "2026-11-18"


# ---- a vault with dated items across sources -------------------------------------------------

def _visit(con, day, url, title, host, site, category=None, n=1):
    for i in range(n):
        ts = datetime.fromisoformat(day).replace(hour=12, minute=i, tzinfo=UTC).timestamp()
        con.execute(
            f"INSERT INTO visits({', '.join(store._COLS)}) VALUES "
            f"({', '.join('?' * len(store._COLS))})",
            ("chrome/Default", f"{url}{day}{i}", ts, day, url, title, host, site, category, 0,
             None, None, None, "", "", 0, None))


@pytest.fixture
def vault(root, home, monkeypatch):
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    (home / ".keepfold").mkdir()
    (home / ".keepfold" / "config.yaml").write_text(
        "browse:\n  private_domains: [secret.example]\n", encoding="utf-8")
    note(root, "notes/kubernetes.md", "title: Kubernetes\ndate: 2026-09-01\n",
         "# Kubernetes\n\nContainers, pods and Kubernetes services. Kubernetes again.")
    note(root, "notes/helm.md", "title: Helm charts\ndate: 2026-11-12\n",
         "# Helm charts\n\nDeploying to kubernetes with helm.")
    note(root, "notes/diary.md", "title: Night diary\nprivate: true\ndate: 2026-11-14\n",
         "# Night diary\n\nKubernetes outage dream.")
    note(root, "resources/chat-kube.md", "title: Kubernetes chat\nkind: resource\n",
         "# Kubernetes chat\n\nTimeline: [[2026-11-03]] · Source: Gemini · Date: 2026-11-03\n\n"
         "We talked about kubernetes operators.")
    note(root, "journal/2026-11-16.md", "title: Journal 2026-11-16\nkind: journal\n"
         "date: 2026-11-16\n", "## Log\n- 09:30 Read about kubernetes operators\n- 12:00 Lunch\n")
    v = Vault(root)
    with store.connect(v) as con:
        _visit(con, "2026-11-10", "https://github.com/k8s", "kubernetes/kubernetes - GitHub",
               "github.com", "github.com", n=2)
        _visit(con, "2026-11-11", "https://github.com/x", "Other repo", "github.com", "github.com")
        _visit(con, "2026-11-17", "https://secret.example/k", "Kubernetes secret notes",
               "secret.example", "secret.example")
        _visit(con, "2026-11-13", "https://clinic.example/k", "Kubernetes clinic",
               "clinic.example", "clinic.example", category="health")  # hidden: private
    con.close()
    _study_events(v)
    return v


def test_count_across_sources_under_the_private_switch(vault):
    q = _route("how many times did I mention kubernetes?")
    on = chat_facts.facts(vault, q, ALL_ON)
    assert on["kind"] == "count" and on["period"] is None and on["unit"] == "times"
    assert on["sources"] == {"notes": 3, "chats": 1, "journal": 1, "browsing": 4}
    assert on["total"] == 9 == on["coverage"]["matched"]
    assert on["coverage"]["read"] >= on["total"] and on["coverage"]["off"] == []
    notes = next(r for r in on["rows"] if r["source"] == "notes")
    assert notes == {"source": "notes", "count": 3, "days": 3, "first": "2026-09-01",
                     "last": "2026-11-14"}
    off = chat_facts.facts(vault, q, OFF)
    assert off["sources"] == {"notes": 2, "chats": 1, "journal": 1, "browsing": 2}
    assert off["coverage"]["off"] == ["private"]
    dumped = json.dumps(off)
    assert "diary" not in dumped.lower() and "secret" not in dumped and "clinic" not in dumped
    said = chat_facts.say(off, TODAY)
    assert said == ('"kubernetes" shows up 6 times: 2 notes, 1 saved AI chat, 1 journal line, '
                    "2 visits.")


def test_count_with_a_focus_a_period_or_days(vault):
    f = chat_facts.facts(vault, _route("how many chats about kubernetes?"), ALL_ON)
    assert (f["total"], f["sources"], f["focus"]) == (1, {"chats": 1}, "chats")
    assert chat_facts.say(f, TODAY) == '1 saved AI chat mentions "kubernetes".'
    f = chat_facts.facts(vault, _route("how many times did I visit github last week?"), ALL_ON)
    assert (f["total"], f["sources"], f["period"]) == (3, {"browsing": 3},
                                                       ["2026-11-09", "2026-11-15"])
    f = chat_facts.facts(vault, _route("how many days did I visit github?"), ALL_ON)
    assert f["total"] == 2 and f["unit"] == "days"
    f = chat_facts.facts(vault, _route("how many chats did I have last week?"), ALL_ON)
    assert f["total"] == 0 and chat_facts.empty(f)
    assert chat_facts.say(f, TODAY) == ("I found no saved AI chats from 9 Nov to 15 Nov in "
                                        "your archive.")
    f = chat_facts.facts(vault, _route("how many times did I study two pointers?"), ALL_ON)
    assert f["sources"] == {"study": 2}


def test_first_and_last_order_by_the_items_own_date(vault):
    first = chat_facts.facts(vault, _route("when did I first read about kubernetes?"), ALL_ON)
    assert first["item"] == {"day": "2026-09-01", "source": "notes", "title": "Kubernetes"}
    assert [r["day"] for r in first["rows"]] == ["2026-09-01", "2026-11-03", "2026-11-10"]
    assert chat_facts.say(first, TODAY) == ('The first time "kubernetes" shows up is 1 Sep: '
                                            '"Kubernetes" (note).')
    last = chat_facts.facts(vault, _route("when did I last look at kubernetes?"), ALL_ON)
    assert last["item"] == {"day": "2026-11-17", "source": "browsing",
                            "title": "Kubernetes secret notes"}
    last = chat_facts.facts(vault, _route("when did I last look at kubernetes?"), OFF)
    assert last["item"] == {"day": "2026-11-16", "source": "journal",
                            "title": "09:30 Read about kubernetes operators"}
    none = chat_facts.facts(vault, _route("when did I first visit zebrafish.org?"), ALL_ON)
    assert none["item"] is None and chat_facts.say(none, TODAY) == (
        'I found nothing about "zebrafish org" in your archive.')


def test_timeline_groups_by_day_and_week(vault):
    f = chat_facts.facts(vault, _route("what did I do each day last week?"), ALL_ON)
    assert f["unit"] == "day" and f["period"] == ["2026-11-09", "2026-11-15"]
    days = [r["from"] for r in f["rows"]]
    assert days == sorted(days, reverse=True) and "2026-11-16" not in days
    d10 = next(r for r in f["rows"] if r["from"] == "2026-11-10")
    assert d10["sources"] == {"browsing": 2} and d10["top"] == {"browsing": ["github.com"]}
    assert any(r["from"] == "2026-11-14" and "notes" in r["sources"] for r in f["rows"])
    off = chat_facts.facts(vault, _route("what did I do each day last week?"), OFF)
    assert not any(r["from"] in ("2026-11-13", "2026-11-14") for r in off["rows"])
    weeks = chat_facts.facts(vault, _route("timeline of the past 3 months"), ALL_ON)
    assert weeks["unit"] == "week" and all(
        date.fromisoformat(r["from"]).weekday() == 0 for r in weeks["rows"])
    assert "busiest week" in chat_facts.say(weeks, TODAY)


def test_everything_about(vault):
    f = chat_facts.facts(vault, _route("tell me everything about kubernetes"), ALL_ON)
    assert f["page"]["title"] == "Kubernetes" and f["page"]["kind"] == "note"
    assert f["first"]["day"] == "2026-09-01" and f["last"]["day"] == "2026-11-17"
    assert f["related"][0] == "Kubernetes"  # the most mentions first
    assert [s["site"] for s in f["sites"]] == ["github.com", "clinic.example", "secret.example"]
    off = chat_facts.facts(vault, _route("tell me everything about kubernetes"), OFF)
    assert [s["site"] for s in off["sites"]] == ["github.com"]
    assert "Night diary" not in json.dumps(off) and off["last"]["day"] == "2026-11-16"
    subject = chat_facts.facts(vault, _route("what do I know about two pointers?"), ALL_ON)
    assert subject["page"] == {"id": "two-pointers", "title": "Two pointers",
                               "kind": "concept"}


@pytest.mark.parametrize("q", ["what did I decide about tabs vs spaces?",
                               "how does python vs java handle memory?", "does rust vs go",
                               "is react better than vue or vs code?"])
def test_a_question_with_vs_inside_is_no_compare(q):
    got = chat_facts.route(q, None, date(2026, 11, 21))  # the passages answer it, or (R4) a
    assert got is None or got["kind"] == "decisions"  # decision question reads decision pages


def test_compare_counts_each_side_with_its_top_page(vault):
    f = chat_facts.facts(vault, _route("kubernetes vs helm"), ALL_ON)
    assert f["kind"] == "compare" and [r["term"] for r in f["rows"]] == ["kubernetes", "helm"]
    kube, helm = f["rows"]
    assert kube["total"] == 9 and kube["page"] == {"id": "kubernetes", "title": "Kubernetes",
                                                   "day": "2026-09-01", "source": "notes"}
    assert helm["total"] == 1 and helm["page"]["title"] == "Helm charts"
    assert f["total"] == 10 and f["sources"]["notes"] == 4
    said = chat_facts.say(f, TODAY)
    assert said.startswith('"kubernetes" shows up 9 times (') and \
        'most in "Kubernetes" (written on 1 Sep)' in said and '"helm" shows up 1 time' in said
    off = chat_facts.facts(vault, _route("compare kubernetes and helm"), OFF)
    assert "Night diary" not in json.dumps(off)
    none = chat_facts.facts(vault, _route("zebra vs okapi"), ALL_ON)
    assert chat_facts.empty(none) and chat_facts.say(none, TODAY) == (
        'I found nothing about "zebra vs okapi" in your archive.')


def test_compare_top_page_keeps_its_own_id_when_titles_repeat(vault):
    """Audit 21 Nov C4: two pages titled "New chat": the top one's id, not the first's."""
    note(vault.root, "notes/chat-a.md", "title: New chat\ndate: 2026-11-01\n",
         "# New chat\n\nOne zebra.")
    note(vault.root, "notes/chat-b.md", "title: New chat\ndate: 2026-11-05\n",
         "# New chat\n\nZebra, zebra, zebra.")
    zebra, _ = chat_facts.facts(vault, _route("zebra vs helm"), ALL_ON)["rows"]
    assert zebra["page"]["id"] == "chat-b" and zebra["page"]["day"] == "2026-11-05"


def test_compare_reads_each_top_page_with_its_written_on_line(vault):
    s = Stream("ollama", ["Kubernetes has more [1]."])
    try:
        _, ev = _turn(vault, "compare kubernetes and helm", s.runner())
    finally:
        s.close()
    user = s.bodies[0]["messages"][1]["content"]
    assert "Kubernetes\nFrom: your note\nWritten on 2026-09-01\n" in user
    assert "Helm charts\nFrom: your note\nWritten on 2026-11-12\n" in user
    assert user.index('<untrusted_note id="facts">') < user.index('<untrusted_note id="1">')
    assert next(e for e in ev if e["t"] == "step")["s"] == 'Comparing "kubernetes vs helm"'
    src = next(e for e in ev if e["t"] == "src")["sources"]
    assert [(x["n"], x["path"]) for x in src] == [(1, "notes/kubernetes.md")]


def _hub(vault):
    note(vault.root, "notes/devops.md", "title: DevOps\ntags: [topic]\n", "# DevOps\n")
    note(vault.root, "notes/devops-ci.md", "title: CI pipelines\ntags: [topic]\n"
         "related: ['[[devops]]']\n", "# CI pipelines\n")
    for slug, title in (("docker", "Docker basics"), ("terraform", "Terraform state")):
        note(vault.root, f"notes/{slug}.md", f"title: {title}\nrelated: ['[[devops]]']\n",
             f"# {title}\n\nNotes.")
    note(vault.root, "notes/vault-secrets.md", "title: Vault secrets\nprivate: true\n"
         "related: ['[[devops]]']\n", "# Vault secrets\n\nPrivate.")
    old = datetime(2026, 10, 2, 12, tzinfo=UTC).timestamp()
    import os
    os.utime(vault.root / "notes/terraform.md", (old, old))


def test_gaps_lists_a_hubs_pages_oldest_first_and_unopened_recalls(vault):
    _hub(vault)
    f = chat_facts.facts(vault, _route("what am I missing about devops?"), ALL_ON)
    assert f["page"] == {"id": "devops", "title": "DevOps", "kind": "note"}
    assert [r["title"] for r in f["rows"]][:1] == ["Terraform state"]  # untouched longest
    assert {r["title"] for r in f["rows"]} == {"Terraform state", "Docker basics",
                                               "CI pipelines"}
    assert f["rows"][0]["day"] == "2026-10-02" and f["total"] == 3
    assert next(r for r in f["rows"] if r["title"] == "CI pipelines")["hub"] is True
    assert "Vault secrets" not in json.dumps(f)  # a private page is never listed
    assert f["unopened"] == []  # no recall log: that half is skipped
    log = vault.root / ".keepfold" / "recall-log.jsonl"
    log.parent.mkdir(exist_ok=True)
    log.write_text("\n".join(json.dumps(r) for r in [
        {"rid": "a", "query": "devops on call rota", "ids": []},
        {"rid": "b", "query": "devops docker layers", "ids": ["docker"]},
        {"opened": "b", "id": "docker"},
        {"rid": "c", "query": "pasta recipe", "ids": []}]) + "\n", encoding="utf-8")
    f = chat_facts.facts(vault, _route("what am I missing about devops?"), ALL_ON)
    assert f["unopened"] == ["devops on call rota"]
    said = chat_facts.say(f, TODAY)
    assert said.startswith('Your topic "DevOps" has 3 pages; the longest untouched') and \
        '"Terraform state" (2 Oct)' in said and '"devops on call rota"' in said
    assert chat_facts.facts(vault, _route("what am I missing about devops?"),
                            dict(ALL_ON, notes=False))["rows"] == []
    none = chat_facts.facts(vault, _route("what am I missing about knitting?"), ALL_ON)
    assert chat_facts.say(none, TODAY).startswith('No topic page matches "knitting"')


def test_a_private_subject_is_counted_only_under_the_private_switch(vault):
    q = _route("how many times did I study secret diary?")
    assert chat_facts.facts(vault, q, ALL_ON)["sources"] == {"study": 1}
    hidden = chat_facts.facts(vault, q, OFF)
    assert hidden["total"] == 0 and "Private" not in json.dumps(hidden)
    about = chat_facts.facts(vault, _route("what do I know about secret diary?"), OFF)
    assert about["page"] is None


@pytest.mark.parametrize("term, hay, hit", [
    ("java", "Java streams", True), ("java", "JavaScript closures", False),
    ("git", "git rebase", True), ("git", "GitHub actions", False),
    ("c++", "C++ templates", True), ("c++", "cooking curry", False),
    ("pointers", "two-pointers drill", True), ("pointer", "Pointers again", True),
    ("process", "child processes", True), ("class", "classes in Python", True),
    ("node.js", "Node.js streams", True), ("kubernetes", "kubernetes/kubernetes", True),
    ("python", "python3 venv", True), ("java", "java8 lambdas", True)])
def test_terms_match_whole_words_with_a_plural(term, hay, hit):
    """Audit 21 Nov C3: "java" is not JavaScript, "git" not GitHub, "c" not every c-word."""
    assert chat_facts._matcher(chat_facts.words(term))[0](hay) is hit


def test_study_counts_skip_voided_and_bookkeeping_events(vault):
    """Audit 21 Nov C2: a voided grade and a remap are not study."""
    from keepfold.events import EventLog

    log = EventLog(vault)
    noon = datetime(2026, 11, 18, 12).astimezone()
    g = log.append(session="cli", kind="grade", source="tutor", now=noon,
                   fields={"object": "concept:two-pointers", "verdict": "correct"})
    log.append(session="cli", kind="void", source="tutor", now=noon,
               fields={"ref": g["id"], "payload": {"reason": "x"}})
    log.append(session="cli", kind="remap", source="external", now=noon,
               fields={"object": "concept:two-pointers", "ref": "concept:old-pointers"})
    q = _route("how many times did I study two pointers?")
    assert chat_facts.facts(vault, q, ALL_ON)["sources"] == {"study": 2}


# ---- the stream ------------------------------------------------------------------------------

def _turn(vault, text, runner, settings=None):
    events = []
    cid = chat.reply(vault, None, text, runner, events.append, threading.Event(),
                     settings or dict(chat.get_settings(), sources=ALL_ON), today=TODAY)
    return cid, events


@pytest.fixture
def fake():
    s = Stream("ollama", ["You mentioned it ", "9 times."])
    yield s
    s.close()


def test_the_facts_event_comes_before_the_models_words(vault, fake):
    cid, ev = _turn(vault, "how many times did I mention kubernetes?", fake.runner())
    kinds = [e["t"] for e in ev]
    assert kinds[:3] == ["start", "step", "facts"] and kinds[-2:] == ["src", "done"]
    assert kinds.index("facts") < kinds.index("tok")
    assert ev[1]["s"] == 'Counting "kubernetes"'
    assert ev[2]["facts"]["total"] == 9
    steps = [e["s"] for e in ev if e["t"] == "step"]
    assert steps[1] == "Wording the facts with gemma3:2b"
    assert "".join(e["s"] for e in ev if e["t"] == "tok") == "You mentioned it 9 times."
    body = fake.bodies[0]
    system, user = body["messages"]
    assert system["content"].startswith("You put facts") and "2026-11-18" in system["content"]
    assert '<untrusted_note id="facts">' in user["content"]
    assert '"kubernetes" shows up 9 times' in user["content"]
    assert user["content"].rstrip().endswith("numbers and dates.)")
    saved = chat.load(vault, cid)[-1]
    assert saved["facts"]["total"] == 9 and saved["model"] == "gemma3:2b"


def test_the_facts_piece_carries_titles_without_links_or_markup(vault, fake):
    note(vault.root, "notes/evil.md", "title: '![i](http://evil.example/x.png) [Kubernetes]"
         "(http://evil.example) <script>x</script>'\ndate: 2026-11-15\n", "# Evil\n\nkubernetes")
    with store.connect(vault) as con:
        _visit(con, "2026-11-15", "https://evil.example/k", "[Kubernetes](javascript:x) <img>",
               "evil.example", "evil.example")
    con.close()
    for q in ("timeline of last week", "tell me everything about kubernetes",
              "when did I last look at kubernetes?"):
        _, ev = _turn(vault, q, fake.runner())
        facts = next(e for e in ev if e["t"] == "facts")["facts"]
        blob = json.dumps(facts)
        assert "Kubernetes" in blob or "kubernetes" in blob
        for bad in ("http://", "javascript:", "](", "<script", "<img", "!["):
            assert bad not in blob, (q, bad)
        assert facts["coverage"]["read"] >= facts["coverage"]["matched"] > 0


def test_empty_facts_answer_without_the_model(vault, fake):
    _, ev = _turn(vault, "how many times did I visit zebrafish.org?", fake.runner())
    assert [e["t"] for e in ev] == ["start", "step", "facts", "tok", "src", "done"]
    assert ev[3]["s"] == 'I found nothing about "zebrafish org" in your archive.'
    assert fake.bodies == [] and ev[-1]["model"] == "Keepfold"


def test_no_runner_or_search_model_words_the_facts_in_code(vault, monkeypatch):
    monkeypatch.setattr(semantic, "load_embedder", lambda *a, **k: None)
    _, ev = _turn(vault, "when did I first read about kubernetes?", None)
    assert not [e for e in ev if e["t"] == "err"]
    assert "".join(e["s"] for e in ev if e["t"] == "tok") == (
        'The first time "kubernetes" shows up is 1 Sep: "Kubernetes" (note).')

    def refuse():
        raise ValueError("a cloud model in the config")
    _, ev = _turn(vault, "timeline of last week", refuse)
    assert [e["t"] for e in ev][:3] == ["start", "step", "facts"]
    assert "".join(e["s"] for e in ev if e["t"] == "tok").startswith("7 items from 9 Nov")


def test_everything_about_adds_cited_passages(vault, fake):
    s = Stream("ollama", ["Kubernetes runs pods [1]."])
    try:
        _, ev = _turn(vault, "tell me everything about kubernetes", s.runner())
    finally:
        s.close()
    user = s.bodies[0]["messages"][1]["content"]
    assert user.index('<untrusted_note id="facts">') < user.index('<untrusted_note id="1">')
    src = next(e for e in ev if e["t"] == "src")["sources"]
    assert [x["n"] for x in src] == [1]


def test_everything_about_in_a_period_reads_passages_from_it(vault):
    """Audit 21 Nov C12: "about X last week" reads no page written outside that week."""
    s = Stream("ollama", ["Helm deploys it [1]."])
    try:
        _turn(vault, "tell me everything about kubernetes last week", s.runner())
    finally:
        s.close()
    user = s.bodies[0]["messages"][1]["content"]
    assert "Date: 2026-11-12" in user  # Helm charts, in the week
    assert "Date: 2026-09-01" not in user and "Date: 2026-11-03" not in user


def test_a_follow_up_and_the_older_answers_keep_their_routes(vault, fake):
    _, ev = _turn(vault, "which sites did I visit last week?", fake.runner())
    assert not [e for e in ev if e["t"] == "facts"]  # the site list, as before
    _, ev = _turn(vault, "what did I do yesterday?", fake.runner())
    assert not [e for e in ev if e["t"] == "facts"]  # the digest, as before
    assert fake.bodies == []


# ---- speed -----------------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.perf
def test_every_typed_answer_is_quick_on_5k_pages(tmp_path, home):
    from test_home import WORDS, _big

    root = tmp_path / "big"
    _big(root)
    v = Vault(root)
    rng = random.Random(0)
    with store.connect(v) as con:  # 50k visits over 90 days, a word in ~1 title of 5
        for i in range(50_000):
            day = date.fromordinal(TODAY.toordinal() - i % 90).isoformat()
            site = f"s{i % 500}.example"
            _visit(con, day, f"https://{site}/p{i}",
                   " ".join(rng.choice(WORDS) for _ in range(5)), site, site)
    con.close()
    for q in ("how many times did I mention heap?", "when did I first read about graph tree?",
              "when did I last visit s7.example?", "timeline of October",
              "what did I do each day last week?", "tell me everything about heap"):
        f = chat_facts.facts(v, _route(q), ALL_ON)  # the first fills the index cache
        t = time.perf_counter()
        f = chat_facts.facts(v, _route(q), ALL_ON)
        took = time.perf_counter() - t
        # ~0.3-0.6 s alone; every visit matching ("graph" in all 50k) took ~0.8 s
        assert f["total"] and took < 1.0, (q, took)


def test_a_page_is_found_by_its_alias(vault):
    """R13d-2: an `aliases:` name finds a note's page as its title does; a private note's
    alias only under the private switch."""
    note(vault.root, "notes/campus.md", "title: Campus life\ndate: 2026-11-10\n"
         "aliases: [NWTU]\n", "# Campus life\n\nHostel notes from NWTU.")
    note(vault.root, "notes/zorb.md", "title: Night list\ndate: 2026-11-10\nprivate: true\n"
         "aliases: [Zorblat]\n", "# Night list\n\nZorblat things.")
    f = chat_facts.facts(vault, _route("tell me everything about nwtu"), ALL_ON)
    assert f["page"]["id"] == "campus" and f["page"]["title"] == "Campus life"
    assert chat_facts.facts(vault, _route("tell me everything about zorblat"),
                            ALL_ON)["page"]["id"] == "zorb"
    off = chat_facts.facts(vault, _route("tell me everything about zorblat"), OFF)
    assert off["page"] is None and "Night list" not in json.dumps(off)


@pytest.mark.parametrize("q,term", [
    ("How many chats did I have about DSA?", "dsa"),
    ("how many chats did I have on python this year?", "python"),
    ("How many chats about transformers did I have?", "transformers"),  # as before
    ("How many notes did I write about rust?", "rust"),
    ("how many times did I have a chat about dsa?", "have a chat about dsa"),  # not this form
    ("How many chats did I have with Bob?", "have with bob"),
    ("how many chats have I had in my archive?", ""),
    ("how many visits did I make to github?", "make to github"),
])
def test_count_question_drops_have_about(q, term):
    got = _route(q)
    assert (got["kind"], got["term"]) == ("count", term), got
