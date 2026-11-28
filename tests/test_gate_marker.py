"""The `gate` marker, --gate-report and --gate-must (tests/conftest.py), exercised by pytester runs
that load only the two option hooks: the recorder is registered by `pytest_configure` itself."""

import json
from types import SimpleNamespace

import conftest
import pytest

pytest_plugins = ["pytester"]

SUITE = """
import pytest

@pytest.mark.gate("G1")
def test_pass(): pass

@pytest.mark.gate("G1")
@pytest.mark.gate("G7")
def test_pass_two_gates(): pass

@pytest.mark.gate("G2")
def test_skip(): pytest.skip("no browser")

@pytest.mark.gate("G3")
def test_fail(): assert 0

def test_unmarked(): pass
"""


@pytest.fixture
def inner(pytester):
    pytester.makepyfile(test_inner=SUITE)
    plugin = SimpleNamespace(pytest_addoption=conftest.pytest_addoption,
                             pytest_configure=conftest.pytest_configure)

    def run(*args):
        return pytester.inline_run("-p", "no:cacheprovider", *args, plugins=[plugin]).ret
    return run


def test_without_the_options_nothing_is_written_and_the_run_is_unchanged(inner, pytester):
    assert inner() == pytest.ExitCode.TESTS_FAILED  # test_fail, as in any plain run
    assert not list(pytester.path.glob("*.json"))
    assert inner("-k", "not test_fail") == pytest.ExitCode.OK  # the skip alone is no failure


def test_report_lists_every_gate_marked_test(inner, pytester):
    out = pytester.path / "gates.json"
    inner("--gate-report", str(out))
    got = json.loads(out.read_text(encoding="utf-8"))
    assert {g: (v["passed"], v["failed"], v["skipped"]) for g, v in got.items()} == {
        "G1": (2, 0, 0), "G2": (0, 0, 1), "G3": (0, 1, 0), "G7": (1, 0, 0)}
    assert got["G2"]["tests"] == {"test_inner.py::test_skip": "skipped"}


def test_a_must_gate_with_no_test_fails_the_run(inner):
    ok = ("-k", "test_pass_two or test_unmarked")
    assert inner(*ok) == pytest.ExitCode.OK
    assert inner(*ok, "--gate-must", "G9") == pytest.ExitCode.TESTS_FAILED  # no such gate
    assert inner(*ok, "--gate-must", "G2") == pytest.ExitCode.TESTS_FAILED  # deselected: zero


def test_a_must_gate_fails_on_a_skip_or_a_failure(inner):
    no_fail = ("-k", "not test_fail")
    assert inner(*no_fail, "--gate-must", "G2") == pytest.ExitCode.TESTS_FAILED  # a skip
    assert inner("--gate-must", "G3") == pytest.ExitCode.TESTS_FAILED  # a failure


def test_a_must_gate_that_passes_leaves_the_exit_alone_and_others_may_skip(inner):
    assert inner("-k", "not test_fail", "--gate-must", "G1,G7") == pytest.ExitCode.OK
