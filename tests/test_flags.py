"""SETUP (Wave B): the config switches' defaults and fail-closed reads, doctor's unknown-key scan
knowing their keys, and `source: "night"` never taken from a tool payload (I2)."""

import pytest
from conftest import events, run

from keepfold import doctor, flags, taint

DEFAULTS = {flags.night: False, flags.warm_hook: False, flags.prime_rank: "words",
            flags.mcp_tools: "all"}


def cfg(home, text):
    (home / ".keepfold").mkdir(exist_ok=True)
    (home / ".keepfold" / "config.yaml").write_text(text, encoding="utf-8")


def test_defaults(home):
    for fn, want in DEFAULTS.items():
        assert fn() == want, fn.__name__


def test_set_values_are_read(home):
    cfg(home, "app: {night: true, warm_hook: true}\nprime: {rank: other}\nmcp: {tools: few}\n")
    assert (flags.night(), flags.warm_hook(), flags.prime_rank(), flags.mcp_tools()) == (
        True, True, "other", "few")


@pytest.mark.parametrize("text", ["app: {night: yes please, warm_hook: 1}\nprime: 3\nmcp: [x]\n",
                                  "app: [night]\nprime: {rank: 5}\nmcp: {tools: ''}\n"])
def test_odd_values_give_the_defaults(home, text):
    cfg(home, text)
    for fn, want in DEFAULTS.items():
        assert fn() == want, fn.__name__


@pytest.mark.gate("G17")
def test_a_broken_config_fails_closed(home):
    cfg(home, "app:\n\tnight: true\n")  # a tab: unreadable
    assert taint.config_error() is not None
    for fn, want in DEFAULTS.items():
        assert fn() == want, fn.__name__


def test_doctor_does_not_warn_on_the_new_keys(home):
    cfg(home, "exclude: [x]\napp: {night: true, warm_hook: true}\nprime: {rank: words}\n"
              "mcp: {tools: all}\n")
    assert taint.unknown_keys() == []
    assert doctor.check_exclude()[0] == "PASS"


def test_a_payload_source_night_gets_the_servers_source(root):
    async def body(call):
        await call("keepfold_start", {"model": "test-model"})
        await call("keepfold_log", {"kind": "self_report", "payload": {
            "concept": "arrays-basics", "verdict": "correct", "source": "night"}})
        await call("keepfold_end", {})

    run(root, body)
    assert "night" not in [e["source"] for e in events(root)]
