import os
import shutil
import time
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest
from conftest import FakeEmbed, note

from keepfold import semantic, vecstore
from keepfold.catalog import Catalog
from keepfold.index import Index
from keepfold.mcp_server import Keepfold
from keepfold.vault import Vault, VaultError

pytestmark = pytest.mark.usefixtures("home")
REAL_LOAD = semantic.load_embedder  # before the autouse fixture wraps it

FIXTURE = Path(__file__).parent / "fixtures" / "vault_mini"


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "vault"
    shutil.copytree(FIXTURE, r)
    note(r, "notes/engines.md", "title: Engines\nsummary: how a motor works\n",
         "# Engines\n\nAutomobile car motor basics.")
    note(r, "notes/pasta.md", "title: Pasta\n",
         "# Pasta\n\nA recipe from the kitchen, cooking slowly. The vehicle was late.")
    note(r, "notes/gearbox.md", 'title: Gearbox\nrelated: ["[[engines]]"]\n',
         "# Gearbox\n\nGears and teeth.")
    note(r, "notes/hidden.md", "title: Hidden\nprivate: true\nrelated: [\"[[engines]]\"]\n",
         "# Hidden\n\nautomobile vehicle car diary")
    return r


@pytest.fixture
def idx(root):
    v = Vault(root)
    return Index(v, Catalog.load(v))


Q = semantic.QUERY_PREFIX + "vehicle"


def ids(hits):
    return [h["id"] for h in hits]


def test_hybrid_puts_the_similar_page_first_and_expands_links(idx):
    hits = semantic.recall(idx, "vehicle", FakeEmbed(), limit=8, hops=1)
    # pasta's cosine (~0.2) is under MIN_COS, so it ranks on its keyword credit alone
    assert ids(hits)[:3] == ["engines", "gearbox", "pasta"]
    assert [h["match"] for h in hits[:3]] == ["similar", "linked", "keyword"]
    assert hits[0]["summary"] == "how a motor works" and hits[0]["kind"] == "note"
    assert hits[1]["via"] == "engines (related)" and hits[1]["score"] == pytest.approx(0.35)
    assert hits[2]["score"] == pytest.approx(semantic.W_KW)
    assert hits[0]["score"] > hits[1]["score"] > hits[2]["score"]
    assert "hidden" not in ids(hits)
    assert ids(semantic.recall(idx, "vehicle", FakeEmbed(), hops=0)) == ["engines", "pasta"]


def test_private_pages_are_never_embedded(idx):
    fake = FakeEmbed()
    semantic.recall(idx, "vehicle", fake)
    assert not any("diary" in t for t in fake.texts)
    assert not any("Private concept" in t for t in fake.texts)


def test_embed_all_sorts_by_length_but_returns_input_order():
    texts = ["car motor engine vehicle", "pasta", "", "kitchen cooking recipe pasta", "car"]
    assert np.array_equal(semantic.embed_all(FakeEmbed(), texts, batch=2),
                          semantic._embed(FakeEmbed(), texts))
    assert semantic.embed_all(FakeEmbed(), []).shape[0] == 0


def test_only_embeds_the_named_pages_heads(idx):
    fake = FakeEmbed()
    ids_, _, _, _ = semantic.vectors(idx, fake, 4, budget=0.0, only={"engines", "pasta"})
    assert sorted(ids_) == ["engines", "pasta"] and len(fake.texts) == 2


def test_cache_reembeds_only_changed_pages_and_drops_deleted(root, idx):
    fake = FakeEmbed()
    semantic.recall(idx, "vehicle", fake)
    n_pages = len(fake.texts) - 1  # minus the query
    assert n_pages == len(idx._notes) and (root / semantic.CACHE).is_file()

    fake.texts.clear()
    semantic.recall(idx, "vehicle", fake)
    assert fake.texts == [Q]  # nothing changed: only the (prefixed) query

    semantic._MEM.clear()  # a new process: the cache comes back from disk
    semantic.recall(idx, "vehicle", fake)
    assert fake.texts == [Q] * 2

    p = note(root, "notes/pasta.md", "title: Pasta\n", "# Pasta\n\nNow about a motor car.")
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    (root / "notes/gearbox.md").unlink()
    idx.refresh()
    fake.texts.clear()
    hits = semantic.recall(idx, "vehicle", fake)
    assert len(fake.texts) == 2 and "Now about a motor car." in fake.texts[1]
    assert "gearbox" not in ids(hits)
    semantic._MEM.clear()
    stored = semantic._read(root)
    assert "gearbox" not in stored and "hidden" not in stored
    assert len(stored) == n_pages - 1


def test_an_idle_server_drops_its_model_and_answers_the_same_after(root, monkeypatch):
    """M3: once the idle thread drops the last model, an idle `keepfold mcp` empties its working
    set (`_trim`); its next recall loads the model again and answers as before (fake model)."""
    loads, trims, real_trim = [], [], semantic._trim

    class Model:
        def __init__(self):
            loads.append(1)

        def embed(self, texts):
            return list(FakeEmbed()(list(texts)))

    monkeypatch.setattr(semantic, "_text_embedding", lambda download=False, threads=None: Model())
    monkeypatch.setattr(semantic, "load_embedder", REAL_LOAD)
    monkeypatch.setattr(semantic, "_trim", lambda: trims.append(1))
    semantic.models_dir().mkdir(parents=True)
    c = Keepfold(Vault(root))
    first = c.keepfold_recall("vehicle")["hits"]
    assert loads == [1] and ids(first)[0] == "engines"
    assert semantic.idle_pass() == [] and trims == []  # just used: kept
    assert semantic.idle_pass(time.monotonic() + semantic.IDLE_UNLOAD + 1) == ["embedder"]
    assert trims == [1] and semantic._main.model is None
    assert c.keepfold_recall("vehicle")["hits"] == first and len(loads) == 2
    real_trim()  # the real call (Windows: EmptyWorkingSet) runs without error


def test_without_the_model_it_falls_back_to_keyword_and_says_so(idx, tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, "_embedder", None)
    monkeypatch.setattr(semantic, "models_dir", lambda: tmp_path / "no-models")
    hits, notice = semantic.search(idx, "vehicle")
    assert notice == semantic.NOT_SET_UP and "keepfold model download" in notice
    assert ids(hits) == ["pasta"] and hits[0]["match"] == "keyword"
    assert not (tmp_path / "no-models").exists()  # never downloads implicitly
    assert semantic.search(idx, "")[0] == []


def test_context_related_uses_recall_when_the_model_is_there(root, monkeypatch):
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    ctx = Keepfold(Vault(root)).keepfold_context(topic="automobile")["context"]
    related = ctx.split("## Related\n", 1)[1]
    assert related.index("[[engines]]") < related.index("[[gearbox]]")
    assert "hidden" not in related


def _bump(p):
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_private_domain_hides_its_concepts_and_raw_over_mcp(root, monkeypatch):
    """SEC-2: privacy inherits from the domain over MCP too, as it does in the views."""
    with open(root / "catalog.yaml", "a", encoding="utf-8") as f:
        f.write('  - id: "hobby"\n    title: "Hobby"\n    aliases: []\n    areas:\n'
                '      - id: "hobby.cars"\n        title: "Cars"\n        concepts:\n'
                '          - id: "gear-ratios"\n            title: "Gear ratios"\n'
                '            aliases: []\n            requires: []\n')
    dom = note(root, "domains/hobby.md", 'id: "hobby"\ntitle: "Hobby"\nkind: "domain"\n',
               "# Hobby")
    note(root, "areas/hobby.cars.md", 'id: "hobby.cars"\ntitle: "Cars"\ndomain: "hobby"\n'
         'parent: "[[hobby]]"\n', "# Cars")
    note(root, "concepts/gear-ratios.md", 'id: "gear-ratios"\ntitle: "Gear ratios"\n'
         'domain: "hobby"\nparent: "[[hobby.cars]]"\n', "# Gear ratios\n\nautomobile car")
    note(root, "resources/diary-src.md", 'title: "Diary"\nprivate: true\n'
         'source: "raw/diary.md"\n', "# Diary")
    (root / "raw").mkdir(exist_ok=True)
    (root / "raw/diary.md").write_text("my diary", encoding="utf-8")
    (root / "raw/open.md").write_text("open text", encoding="utf-8")
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    c = Keepfold(Vault(root))
    assert "gear-ratios" in ids(c.keepfold_recall("vehicle")["hits"])  # domain still public

    dom.write_text(dom.read_text("utf-8").replace("kind:", "private: true\nkind:"), "utf-8")
    _bump(dom)
    assert "gear-ratios" not in ids(c.keepfold_find("gear ratios")["hits"])
    with pytest.raises(VaultError) as e:
        c.keepfold_read("gear-ratios")
    assert e.value.code == "unknown_id"
    assert "gear-ratios" not in ids(c.keepfold_recall("vehicle")["hits"])
    assert "gear-ratios" not in c.keepfold_context(topic="gear ratios")["context"]
    semantic._MEM.clear()
    stored = semantic._read(root)
    assert "gear-ratios" not in stored and "engines" in stored
    with pytest.raises(VaultError) as e:
        c.keepfold_read("raw/diary.md")
    assert e.value.code == "unknown_id"
    assert "open text" in c.keepfold_read("raw/open.md")["text"]

    dom.write_text(dom.read_text("utf-8").replace("private: true\n", ""), "utf-8")
    _bump(dom)
    assert "gear-ratios" in ids(c.keepfold_find("gear ratios")["hits"])  # privacy lifted


def test_context_topic_finds_a_concept_named_by_one_word(root, monkeypatch):
    """RETR-2: a phrase where one word names a concept still lists it, with or without the
    model (the fallback retries the full-text match on any non-stopword word)."""
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    topic = Keepfold(Vault(root)).keepfold_context(topic="how about pointers")["context"]
    assert "(two-pointers)" in topic.split("## Related", 1)[0]
    monkeypatch.setattr(semantic, "_embedder", None)  # home has no models: no embedder
    c_ctx = Keepfold(Vault(root)).keepfold_context(topic="how about pointers")["context"]
    assert "(two-pointers)" in c_ctx and "No matching concepts." not in c_ctx


def test_cosine_floor_and_stopwords_make_nothing_an_answer(root, idx, monkeypatch):
    """RETR-3: under MIN_COS and with no non-stopword keyword match, a page is no hit."""
    assert "pasta" in ids(semantic.recall(idx, "vehicle", FakeEmbed()))  # keyword hit
    assert "pasta" not in ids(semantic.recall(idx, "motor", FakeEmbed()))  # cos ~0.2 only
    assert semantic._keyword(idx, "what is the") == {}  # stopwords alone match nothing

    class Far(FakeEmbed):  # the query sits ~70 degrees from every page
        def __call__(self, texts):
            out = super().__call__(texts)
            for k, t in enumerate(texts):
                if t.startswith(semantic.QUERY_PREFIX):
                    out[k] = [0.4, 0.4, 1.0, 0.0]
            return out

    monkeypatch.setattr(semantic, "_embedder", Far())
    ctx = Keepfold(Vault(root)).keepfold_context(topic="what is quantum")["context"]
    assert "## Related" not in ctx and "No matching concepts." in ctx
    monkeypatch.setattr(semantic, "MIN_COS", 0.3)
    ctx = Keepfold(Vault(root)).keepfold_context(topic="what is quantum")["context"]
    assert "[[pasta]]" in ctx.split("## Related", 1)[1]


def test_under_the_floor_a_keyword_hit_needs_half_the_query_terms(root):
    note(root, "notes/tips.md", "title: Tips\n", "# Tips\n\nThe best way to study.")
    note(root, "problems/lc-1293.md", 'id: "lc-1293"\ntitle: "LC 1293"\n', "# LC 1293\n")
    v = Vault(root)
    idx = Index(v, Catalog.load(v))
    query = "best pizza in naples"  # "best" is 1 of 3 terms: no hit with the model
    assert "tips" not in ids(semantic.recall(idx, query, FakeEmbed(), hops=0))
    assert "tips" in ids(semantic.recall(idx, query, None, hops=0))  # keyword-only as before
    assert ids(semantic.recall(idx, "LC 1293", FakeEmbed(), hops=0))[0] == "lc-1293"


def test_question_words_do_not_hold_the_cover(root):
    note(root, "notes/dough.md", "title: Dough\n", "# Dough\n\nNeapolitan pizza dough, rested.")
    v = Vault(root)
    idx = Index(v, Catalog.load(v))
    # 2 content words of 6 non-stopwords held the cover at 0.33 < MIN_KW_COVER; now 2 of 2
    query = "what did I say about pizza dough in my vault"
    assert semantic._terms(query) == ['"pizza"*', '"dough"*']
    assert semantic._cover(idx, query)["dough"] == 1.0
    assert "dough" in ids(semantic.recall(idx, query, FakeEmbed(), hops=0))  # cos 0: keyword
    assert semantic._terms("did I remember") == ['"did"', '"remember"*']  # all frame: kept
    assert semantic._keyword(idx, "what is the") == {}  # stopwords still match nothing


def test_reranker_skipped_when_the_top_is_clear(root, idx):
    calls = []

    def spy(query, texts):
        calls.append(query)
        return [0.0] * len(texts)

    plain = semantic.recall(idx, "vehicle", FakeEmbed(), hops=0)
    assert [h["score"] for h in plain] == pytest.approx([semantic.W_COS, semantic.W_KW])
    assert semantic.recall(idx, "vehicle", FakeEmbed(), hops=0, rerank_with=spy) == plain
    assert calls == []  # 0.3 is far under 0.7: the cross-encoder never ran
    for name in ("truck", "bus", "van"):  # three pages at cosine 1 and a keyword: a close top
        note(root, f"notes/{name}.md", f"title: {name}\n", f"# {name}\n\nA vehicle.")
    v = Vault(root)
    idx = Index(v, Catalog.load(v))
    semantic.recall(idx, "vehicle", FakeEmbed(), hops=0, rerank_with=spy)
    assert calls == ["vehicle"]


DEEP = "The vehicle broke down, so the car motor got fixed."


def chat_body(n=40, deep_at=30):
    """A chat page: a kitchen head, short timestamped turns, one vehicle turn deep down."""
    turns = [f"### 2026-05-05 00:{k:02d}\n**Me:** turn {k}\n\n**Claude:** "
             + (DEEP if k == deep_at else "filler words. " * 8) for k in range(n)]
    return "# Trip\n\nA kitchen recipe for pasta.\n\n" + "\n\n".join(turns)


@pytest.fixture
def chat(root):
    note(root, "resources/trip.md", "title: Trip\n", chat_body())
    v = Vault(root)
    return Index(v, Catalog.load(v))


def test_chunks_are_headed_overlapping_windows_cut_at_turns():
    n = {"title": "Trip", "kind": "resource", "fields": {}, "body": chat_body()}
    body = semantic._filled(n["body"].strip())
    ch = semantic._chunks(n)
    assert ch[0] == (0, semantic._text(n)) and len(ch) > 3
    assert ch[1][0] == semantic.BODY_CHARS - semantic.CHUNK_OVERLAP
    for (s, t), (s2, _) in zip(ch[1:], ch[2:], strict=False):
        head, piece = t.split("\n", 1)
        assert head.startswith("Trip › ### ") and body[s:s + len(piece)] == piece
        assert len(piece) <= semantic.CHUNK_CHARS
        assert body[s + len(piece):].startswith("\n### ")  # cut before a whole turn
        assert s2 == s + len(piece) - semantic.CHUNK_OVERLAP  # overlap
    s, t = ch[-1]
    assert body[s:] == t.split("\n", 1)[1]  # the last one runs to the end
    short = {"title": "S", "kind": "note", "fields": {}, "body": "x" * semantic.BODY_CHARS}
    assert semantic._chunks(short) == [(0, semantic._text(short))]
    flat = {"title": "F", "kind": "note", "fields": {}, "body": "y" * 5000}  # a hard cut
    tail = semantic._chunks(flat)[1:]
    assert [s for s, _ in tail] == [1300, 2100, 2900, 3700, 4500]  # stride 800
    assert [t.split("\n", 1) for _, t in tail][0][0] == "F · note"  # no heading, no day
    assert [len(t.split("\n", 1)[1]) for _, t in tail] == [1000] * 4 + [500]


def test_a_chunk_header_skips_comment_lines_in_code_blocks():
    """Audit 21 Nov C13: `# comment` inside a fenced block is not a heading."""
    body = ("## Setup\n\nInstall it.\n\n```bash\n# install deps\npip-tool sync\n```\n\n"
            + "More words here. " * 200)
    n = {"title": "Guide", "kind": "note", "fields": {}, "body": body}
    head = semantic._chunks(n)[1][1].split("\n", 1)[0]
    assert head.startswith("Guide › ## Setup") and "install deps" not in head


def test_c13_pages_with_code_comment_lines_are_embedded_again_once(root):
    """C13's chunk headers changed only for pages with a "# line" in a code block: the first
    load after it drops just their rows (a meta flag makes it once); a dry run changes nothing."""
    note(root, "notes/guide.md", "title: Guide\n",
         "# Guide\n\n```bash\n# install deps\nmotor tool\n```\n")
    v = Vault(root)
    idx = Index(v, Catalog.load(v))
    semantic.vectors(idx, FakeEmbed(), 4)
    db = root / semantic.CACHE

    def store():
        with closing(vecstore.connect(db, create=False)) as con:
            return (vecstore.get_meta(con, "c13"),
                    {i: (m, s.tobytes(), x.tobytes()) for i, m, _, s, x in vecstore.rows(con, 4)})

    flag, before = store()
    assert flag is True and "guide" in before and "engines" in before
    with closing(vecstore.connect(db)) as con, vecstore.tx(con):
        vecstore.set_meta(con, "c13", False)  # a store from before C13's change
    dry = FakeEmbed()
    semantic.vectors(idx, dry, 4, save=False)
    assert store() == (False, before) and dry.texts == []
    fake = FakeEmbed()
    semantic.vectors(idx, fake, 4)  # this process's memory still holds the old rows
    flag, after = store()
    assert flag is True and fake.texts and all(t.startswith("Guide") for t in fake.texts)
    assert after.keys() == before.keys()
    assert {i: e for i, e in after.items() if i != "guide"} == {
        i: e for i, e in before.items() if i != "guide"}  # the rest untouched
    semantic._MEM.clear()
    again = FakeEmbed()
    semantic.vectors(idx, again, 4)
    assert again.texts == [] and store() == (True, after)


def test_a_window_without_turns_or_paragraphs_cuts_at_a_line_sentence_or_space():
    """RR-32: the cut ladder never splits a word when the window has any whitespace."""
    assert semantic._cut("a\n\nb\nc. d e") == 1  # paragraph first
    assert semantic._cut("ab\ncd. ef gh") == 2  # then a line end
    assert semantic._cut("one two. three four") == 8  # then after a sentence's end
    assert semantic._cut("one two three") == 7  # then the last space
    assert semantic._cut("x" * 50) == -1  # no whitespace: a hard cut
    words = " ".join(f"w{k:04d}" for k in range(1500))  # one long line of 6-char words
    n = {"title": "W", "kind": "note", "fields": {}, "body": words}
    body = semantic._filled(words)
    for s, t in semantic._chunks(n)[1:]:
        piece = t.split("\n", 1)[1]
        assert body[s:s + len(piece)] == piece
        assert s + len(piece) == len(body) or body[s + len(piece)] == " "  # ends on a space


def test_a_page_scores_by_its_best_chunk_and_reranks_on_it(chat, monkeypatch):
    monkeypatch.setattr(semantic, "RERANK_MARGIN", 1.0)  # rerank even a clear top
    hits = semantic.recall(chat, "vehicle", FakeEmbed(), hops=0)
    trip = next(h for h in hits if h["id"] == "trip")
    assert trip["match"] == "similar" and trip["score"] > semantic.W_COS  # cos 1 from deep down
    start, text = next(c for c in semantic._chunks(chat._notes["trip"]) if DEEP in c[1])
    assert trip["at"] == start
    assert "at" not in next(h for h in hits if h["id"] == "engines")  # its head won
    seen = []

    def spy(query, texts):
        seen.extend(texts)
        return [0.0] * len(texts)

    semantic.recall(chat, "vehicle", FakeEmbed(), hops=0, rerank_with=spy)
    assert text in seen and semantic._text(chat._notes["trip"]) not in seen


def test_partial_build_scores_by_chunk_0_and_a_build_completes_it(root, chat, monkeypatch):
    monkeypatch.setattr(semantic, "RECALL_BUDGET", 0)
    hits = semantic.recall(chat, "vehicle", FakeEmbed(), hops=0)
    assert next(h for h in hits if h["id"] == "trip")["match"] == "keyword"  # head only
    ids, page, start, _ = semantic.vectors(chat, FakeEmbed(), 4, 0)
    assert len(start) == len(ids)  # one row per page: nothing past the heads yet
    semantic.vectors(chat, FakeEmbed(), 4)  # no budget: `keepfold model build`
    semantic._MEM.clear()  # a new process
    fake = FakeEmbed()
    hits = semantic.recall(chat, "vehicle", fake, hops=0)
    assert fake.texts == [Q]  # all built and saved: nothing re-embedded
    assert next(h for h in hits if h["id"] == "trip")["match"] == "similar"


def test_an_interrupted_build_resumes_without_redoing_saved_chunks(root, chat, monkeypatch):
    monkeypatch.setattr(semantic, "BATCH", 2)
    first, heads, tail_calls = FakeEmbed(), {semantic._text(n) for n in chat._notes.values()}, []

    class Dies(FakeEmbed):  # killed on the second batch past the heads
        def __call__(self, texts):
            if texts[0] not in heads:
                tail_calls.append(1)
                if len(tail_calls) == 2:
                    raise KeyboardInterrupt
            return first(texts)

    with pytest.raises(KeyboardInterrupt):
        semantic.vectors(chat, Dies(), 4, save_every=0)
    semantic._MEM.clear()
    second = FakeEmbed()
    semantic.vectors(chat, second, 4)
    all_texts = [t for i in chat._notes for _, t in semantic._chunks(chat._notes[i])]
    assert not set(first.texts) & set(second.texts)
    assert sorted(first.texts + second.texts) == sorted(all_texts)


def test_cache_versioning_rebuilds_on_new_chunking(root, chat, monkeypatch):
    heads = {semantic._text(chat._notes[i]) for i in semantic._pages(chat)}
    semantic.vectors(chat, FakeEmbed(), 4)
    with closing(vecstore.connect(root / semantic.CACHE, create=False)) as con:
        meta = vecstore.get_meta(con, "settings")
        assert meta["text"] == semantic.TEXT_VERSION and meta["chunk"] == semantic.CHUNKING
        rows = list(vecstore.rows(con, meta["dim"]))
        assert rows and all(full and v.dtype == np.float16 for _, _, full, _, v in rows)
    semantic._MEM.clear()
    monkeypatch.setattr(semantic, "CHUNKING", [1500, 800, 100])  # other settings: rebuild
    fake = FakeEmbed()
    semantic.vectors(chat, fake, 4)
    assert heads <= set(fake.texts)


def test_a_lost_cache_rebuilds_heads_under_a_budget(root, chat, monkeypatch):
    full = semantic.vectors(chat, FakeEmbed(), 4)
    semantic._MEM.clear()
    (root / semantic.CACHE).unlink()  # PRIVACY.md: .keepfold/ may be deleted at any time
    monkeypatch.setattr(semantic, "HEAD_BUDGET", 0)
    fake = FakeEmbed()
    hits = semantic.recall(chat, "vehicle", fake, hops=0)
    assert fake.texts == [Q]  # no page embedded inside the call
    assert hits and {h["match"] for h in hits} == {"keyword"}
    assert semantic.vectors(chat, FakeEmbed(), 4, 0, heads=0)[0] == []
    ids, page, start, vecs = semantic.vectors(chat, FakeEmbed(), 4)  # a later build fills it
    assert ids == full[0] and (start == full[2]).all() and np.allclose(vecs, full[3])


def test_a_server_saving_during_a_build_keeps_the_builds_chunks(root, chat, monkeypatch):
    monkeypatch.setattr(semantic, "RECALL_BUDGET", 0)
    semantic.recall(chat, "vehicle", FakeEmbed())  # a server: heads only

    class Racing(FakeEmbed):  # while the server embeds, another process builds and saves
        def __call__(self, texts):
            if not self.texts:
                server = dict(semantic._MEM)
                semantic._MEM.clear()
                semantic.vectors(chat, FakeEmbed(), 4)  # `keepfold model build`
                semantic._MEM.clear()
                semantic._MEM.update(server)
            return super().__call__(texts)

    _bump(note(root, "notes/pasta.md", "title: Pasta\n", "# Pasta\n\nNow about a motor car."))
    chat.refresh()
    semantic.recall(chat, "vehicle", Racing())  # embeds pasta's head, then saves
    full, n_rows = semantic._read(chat.vault.root)["trip"][1:3]
    assert full and len(n_rows) > 1  # the build's chunks survived the server's save


def test_text_drops_empty_template_headings():
    body = "# Asha\n\n## Who they are to me\n- Lab partner\n\n## How I write to them\n"
    text = semantic._text({"title": "Asha", "fields": {}, "body": body})
    assert "Lab partner" in text and "Who they are to me" in text
    assert "How I write to them" not in text
    nested = "## Plan\n### Step\n- go\n## Empty\n"
    assert semantic._filled(nested) == "## Plan\n### Step\n- go"


CARS = "# {t}\n\n{tl}Automobile car motor basics{z}.\n"
TIMELINE = "Timeline: [[2026-05-01]] · Source: Gemini · Date: 2026-05-05\n\n"


@pytest.fixture
def chats(root):
    note(root, "notes/cars.md", "title: Cars\n", CARS.format(t="Cars", tl="", z=""))
    note(root, "resources/gem-cars.md", "title: Gem cars\n",
         CARS.format(t="Gem cars", tl=TIMELINE, z=" blimp"))
    note(root, "resources/book.md", "title: Book\n", CARS.format(t="Book", tl="", z=""))
    v = Vault(root)
    return Index(v, Catalog.load(v))


def test_imported_chat_pages_rank_below_the_users_own_pages(chats):
    n = chats._notes
    assert semantic.is_chat(n["gem-cars"])
    assert not semantic.is_chat(n["book"]) and not semantic.is_chat(n["cars"])
    sc = {h["id"]: h["score"] for h in semantic.recall(chats, "vehicle", FakeEmbed(), hops=0)}
    full = {h["id"]: h["score"]
            for h in semantic.recall(chats, "vehicle", FakeEmbed(), hops=0, chats=True)}
    assert sc["cars"] > sc["gem-cars"]
    assert sc["gem-cars"] == pytest.approx(full["gem-cars"] * semantic.CHAT_WEIGHT, abs=1e-3)
    assert full["cars"] == sc["cars"]
    assert sc["book"] == full["book"]  # a resource without a Timeline Source is not a chat
    # the only match is still returned
    assert ids(semantic.recall(chats, "blimp", FakeEmbed(), hops=0)) == ["gem-cars"]


def test_rerank_weights_chat_pages_unless_asked(chats):
    def ce(query, texts):  # the cross-encoder slightly prefers the chat page
        return [1.0 if t.startswith("Gem cars") else 0.5 for t in texts]

    def top(**kw):
        got = ids(semantic.recall(chats, "vehicle", FakeEmbed(), hops=0, rerank_with=ce, **kw))
        return [i for i in got if i in ("cars", "gem-cars")]

    assert top() == ["cars", "gem-cars"]
    assert top(chats=True) == ["gem-cars", "cars"]


def test_explain_shows_score_parts_and_the_misses(idx, monkeypatch):
    monkeypatch.setattr(semantic, "RERANK_MARGIN", 1.0)  # rerank even a clear top
    ex = {}
    hits = semantic.recall(idx, "vehicle", FakeEmbed(), limit=1, hops=0, explain=ex)
    parts, misses = semantic.explained(idx, ex, hits, 5)
    assert ids(hits) == ["engines"] and parts[0]["cos"] == pytest.approx(1.0)
    assert parts[0]["kw"] == 0.0 and parts[0]["weight"] == 1.0
    assert "Automobile" in parts[0]["chunk"]
    why = {m["id"]: m["reason"] for m in misses}
    assert why["pasta"] == "below limit"  # a keyword hit that the limit cut
    assert next(m for m in misses if m["id"] == "pasta")["kw"] == pytest.approx(1.0)
    assert len(misses) == 5 and "hidden" not in why
    assert all(r.startswith("cosine ") and "< MIN_COS" in r for i, r in why.items() if i != "pasta")
    ex = {}
    hits = semantic.recall(idx, "vehicle", FakeEmbed(), limit=1, hops=0, explain=ex,
                           rerank_with=lambda q, texts: [0.0] * len(texts))
    parts, misses = semantic.explained(idx, ex, hits, 5)
    assert parts[0]["rerank"] == pytest.approx(0.5)
    assert {m["id"]: m["reason"] for m in misses}["pasta"] == "below limit after rerank"
    ex = {}  # keyword only: no cosine, no chunk
    parts, _ = semantic.explained(idx, ex, semantic.recall(idx, "vehicle", explain=ex), 0)
    assert parts[0]["cos"] is None and parts[0]["chunk"] == ""


def test_recall_cli_explain_writes_no_recall_log(root, monkeypatch):
    from typer.testing import CliRunner

    from keepfold import recall_log
    from keepfold.cli import app

    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    out = CliRunner().invoke(app, ["recall", "vehicle", "--limit", "1", "--hops", "0",
                                   "--explain", "--show-misses", "--vault", str(root)])
    assert out.exit_code == 0, out.output
    assert "cos 1.000  kw 0.000" in out.output and "chunk@0" in out.output
    assert "missed:" in out.output and "pasta: below limit" in out.output
    assert recall_log.rows(Vault(root)) == []


def test_a_page_whose_status_no_longer_holds_ranks_below_the_current_one(root):
    note(root, "decisions/a-old.md", "title: Car plan\nstatus: superseded\n", CARS.format(
        t="Car plan", tl="", z=""))
    note(root, "decisions/b-new.md", "title: Car plan\nstatus: accepted\n", CARS.format(
        t="Car plan", tl="", z=""))
    note(root, "projects/c-done.md", "title: Car plan\nstatus: shipped\n", CARS.format(
        t="Car plan", tl="", z=""))
    v = Vault(root)
    idx = Index(v, Catalog.load(v))
    sc = {h["id"]: h["score"] for h in semantic.recall(idx, "vehicle", FakeEmbed(), hops=0)}
    # equal text: by id the old one would sort first; a shipped project is still true history
    assert sc["b-new"] == sc["c-done"] == pytest.approx(sc["a-old"] / semantic.STALE_WEIGHT)
    assert semantic._factors(idx, "a-old", False, None) == {"status": semantic.STALE_WEIGHT}


def test_a_recent_journal_or_chat_page_edges_out_an_old_one(root, monkeypatch):
    monkeypatch.setattr(semantic, "_today", lambda: "2026-11-20")
    for day in ("2025-01-10", "2026-11-10"):
        note(root, f"journal/{day}.md", f"title: Journal {day}\n", CARS.format(
            t=f"Journal {day}", tl="", z=""))
    for name, day in (("gem-a", "2025-01-10"), ("gem-b", "2026-11-10")):
        tl = TIMELINE.replace("2026-05-05", day)
        note(root, f"resources/{name}.md", "title: Gem cars\n", CARS.format(
            t="Gem cars", tl=tl, z=""))
    v = Vault(root)
    idx = Index(v, Catalog.load(v))
    got = ids(semantic.recall(idx, "vehicle", FakeEmbed(), hops=0, limit=20))
    assert got.index("2026-11-10") < got.index("2025-01-10")
    assert got.index("gem-b") < got.index("gem-a")
    f = semantic._factors(idx, "gem-b", False, None)
    assert f["chat"] == semantic.CHAT_WEIGHT
    assert f["recency"] == pytest.approx(1 + 0.12 * np.exp(-10 / 180))
    assert semantic._factors(idx, "2025-01-10", False, None)["recency"] < 1.02
    # an undated page, and a dated page of another kind, are unchanged
    assert semantic._weight(idx, "engines", False, None) == 1.0
    note(root, "notes/dated.md", "title: Dated\ndate: 2026-11-19\n", "# Dated\n\ncar")
    idx = Index(v, Catalog.load(v))
    assert semantic._factors(idx, "dated", False, None) == {}


def test_a_current_query_ranks_the_newer_of_two_like_pages_first(root, monkeypatch):
    monkeypatch.setattr(semantic, "_today", lambda: "2026-11-20")
    for name, day in (("a-old", "2025-01-10"), ("b-new", "2026-11-10")):
        note(root, f"decisions/{name}.md", f"title: Car plan\nstatus: accepted\ndate: {day}\n",
             CARS.format(t="Car plan", tl="", z=""))
    v = Vault(root)
    idx = Index(v, Catalog.load(v))

    def order(q):
        got = ids(semantic.recall(idx, q, FakeEmbed(), hops=0, limit=20))
        return got.index("a-old") < got.index("b-new")

    assert order("vehicle")  # neutral: equal scores, by id
    assert not order("which vehicle do I use these days")
    assert semantic.is_current("Am I STILL on it?") and not semantic.is_current("knowledge")


def _long_page():
    filler = "Some words about the plan and the week. " * 30
    body = (f"# Week\n\n{filler}\n\n## 10 Nov\n\n### Notes\n\n{filler}\n\n"
            f"### Decisions\n\n{filler}\n\n{filler}\n\n## 11 Nov\n\n{filler}\n")
    return {"title": "Week", "kind": "journal", "fields": {"date": "2026-11-10"}, "body": body}


def test_a_deep_chunk_is_headed_by_title_heading_path_day_and_kind():
    n = _long_page()
    body = semantic._filled(n["body"].strip())
    ch = semantic._chunks(n)
    assert len(ch) > 2 and ch[0] == (0, semantic._text(n))
    for s, t in ch[1:]:
        head, piece = t.split("\n", 1)
        assert body[s:s + len(piece)] == piece  # the offset still points into the real body
        above = [ln for ln in body[:s].splitlines() if ln.startswith("#")]
        assert head.endswith(" · 2026-11-10 · journal") and above[-1] in head
    assert ch[1][1].split("\n", 1)[0] == "Week › ## 10 Nov › ### Notes · 2026-11-10 · journal"
    assert ch[-1][1].startswith("Week › ## 11 Nov · ")  # a new ## closes ### Decisions
    long = semantic._header("T" * 300, ["## x"], "2026-11-10", "note")
    assert len(long) == semantic.HEADER_CHARS and long.endswith(" · 2026-11-10 · note")


def test_a_deep_passage_cites_its_span_in_the_body_not_the_header():
    from keepfold import ask

    n = _long_page()
    s, t = semantic._chunks(n)[2]
    at, size = ask._span(n["body"], t, n["title"])
    assert n["body"][at:].startswith(t.split("\n", 1)[1].strip().splitlines()[0])
    assert size <= len(t)


def test_explain_shows_the_weight_factors(root, monkeypatch):
    monkeypatch.setattr(semantic, "_today", lambda: "2026-05-05")
    note(root, "resources/gem-cars.md", "title: Gem cars\n",
         CARS.format(t="Gem cars", tl=TIMELINE, z=""))
    note(root, "decisions/old-cars.md", "title: Old cars\nstatus: retired\n",
         CARS.format(t="Old cars", tl="", z=""))
    v = Vault(root)
    idx = Index(v, Catalog.load(v))
    ex = {}
    hits = semantic.recall(idx, "vehicle", FakeEmbed(), hops=0, explain=ex)
    parts = {p["id"]: p for p in semantic.explained(idx, ex, hits)[0]}
    assert parts["gem-cars"]["factors"] == {"chat": 0.6, "recency": 1.12}
    assert parts["gem-cars"]["weight"] == pytest.approx(0.6 * 1.12)
    assert parts["old-cars"]["factors"] == {"status": 0.5}
    assert parts["engines"]["factors"] == {} and parts["engines"]["weight"] == 1.0


def test_models_load_from_keepfold_models_never_fastembeds_temp_cache(home, monkeypatch):
    """R29a: fastembed's default cache is under %TEMP%, which Windows cleans; both models are
    read from (and downloaded into) ~/.keepfold/models."""
    import fastembed
    from fastembed.rerank import cross_encoder

    seen = []

    class Model:
        def __init__(self, name, cache_dir=None, **kw):
            seen.append(cache_dir)

    monkeypatch.setattr(fastembed, "TextEmbedding", Model)
    monkeypatch.setattr(cross_encoder, "TextCrossEncoder", Model)
    semantic._text_embedding()
    semantic._cross_encoder()
    want = str(home / ".keepfold" / "models")
    assert semantic.models_dir() == home / ".keepfold" / "models" and seen == [want, want]
