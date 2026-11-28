"""Gate G17, the settings file: config.yaml.bak is the last copy that parsed, a version above 1 is
refused, the click-only restore, doctor's unknown-key WARN, and no broken line's text anywhere."""

import json
import logging

import pytest
from test_app import app, call, fakes, vault  # noqa: F401  (fixtures: a served app, nothing real)

from keepfold import doctor, taint
from keepfold.vault import VaultError

pytestmark = pytest.mark.usefixtures("home")
SECRET = "hunter2-sk-live-0123"
BROKEN = f"vault: V\nbrowse:\n\tdeny: {SECRET}\n"  # a tab: line 3


def cfg(home):
    (home / ".keepfold").mkdir(exist_ok=True)
    return home / ".keepfold" / "config.yaml"


@pytest.mark.gate("G17")
def test_bak_is_last_good(home):
    p = cfg(home)
    bak = p.with_name("config.yaml.bak")
    taint.write_config({"vault": "A"})
    assert not bak.exists()  # nothing was there to keep
    first = p.read_text(encoding="utf-8")
    taint.write_config({"exclude": ["x"]})
    assert bak.read_text(encoding="utf-8") == first
    second = p.read_text(encoding="utf-8")
    taint.write_config({"exclude": ["x"]})  # unchanged: not rewritten, .bak stays
    assert bak.read_text(encoding="utf-8") == first
    taint.write_config({"vault": "B"})
    assert bak.read_text(encoding="utf-8") == second
    p.write_text(BROKEN, encoding="utf-8")  # a hand edit breaks it
    with pytest.raises(VaultError):
        taint.write_config({"vault": "C"})
    assert p.read_text(encoding="utf-8") == BROKEN
    assert bak.read_text(encoding="utf-8") == second  # the broken text never became the .bak
    assert taint.restorable()


@pytest.mark.gate("G17")
def test_version_above_1_is_refused(home):
    p = cfg(home)
    taint.write_config({"vault": "A", "version": 1})
    before = p.read_text(encoding="utf-8")
    with pytest.raises(VaultError, match="newer"):
        taint.write_config({"version": 2})
    newer = before.replace("version: 1", "version: 3")  # a newer Keepfold wrote it
    p.write_text(newer, encoding="utf-8")
    with pytest.raises(VaultError, match="version 3"):
        taint.write_config({"vault": "B"})
    assert p.read_text(encoding="utf-8") == newer
    assert not p.with_name("config.yaml.bak").exists()


@pytest.mark.gate("G17")
def test_restore_only_when_broken(app, home):  # noqa: F811
    p = cfg(home)
    bak = p.with_name("config.yaml.bak")
    restore = "/api/config/restore"
    assert call(app, "GET", restore)[0] == 405
    assert call(app, "POST", restore, {}, token=False)[0] == 401
    assert call(app, "POST", restore, {}, origin="http://evil.example")[0] == 403
    taint.write_config({"vault": "A"})
    taint.write_config({"vault": "B"})  # a readable config and a .bak
    assert call(app, "POST", restore, {})[0] == 400
    assert "A" not in p.read_text(encoding="utf-8")  # the readable file is untouched
    p.write_text(BROKEN, encoding="utf-8")
    row = call(app, "GET", "/api/status")[1]["rows"][0]
    assert row["id"] == "config" and row["fix"] == {
        "post": restore, "label": "Restore the last good copy"}
    assert p.read_text(encoding="utf-8") == BROKEN  # nothing restores itself
    code, js, raw, _ = call(app, "POST", restore, {})
    assert code == 200 and js["ok"] and SECRET.encode() not in raw
    assert taint.config() == {"vault": "A"} and taint.config_error() is None
    assert bak.read_text(encoding="utf-8") == p.read_text(encoding="utf-8")
    # no .bak: no button, and a refusal
    p.write_text(BROKEN, encoding="utf-8")
    bak.unlink()
    assert "fix" not in call(app, "GET", "/api/status")[1]["rows"][0]
    assert call(app, "POST", restore, {})[0] == 400
    # a .bak that does not parse is no last good copy
    bak.write_text(BROKEN, encoding="utf-8")
    assert "fix" not in call(app, "GET", "/api/status")[1]["rows"][0]
    code, js, raw, _ = call(app, "POST", restore, {})
    assert code == 400 and SECRET.encode() not in raw and p.read_text(encoding="utf-8") == BROKEN


def test_unknown_key_warns(home):
    p = cfg(home)
    p.write_text("vault: V\nexclude: [x]\nlocalllm: {model: m}\nversion: 1\n", encoding="utf-8")
    assert doctor.check_exclude()[0] == "PASS"
    assert {"vault", "exclude", "app", "browse", "git", "localllm", "chat", "cloudllm",
            "version"} <= taint._known_keys()  # read from the code
    p.write_text("vault: V\nexlcude: [x]\n", encoding="utf-8")
    state, label, value, fix = doctor.check_exclude()
    assert (state, label) == ("WARN", "Excluded folders") and "exlcude" in value
    assert "spelling" in fix
    p.write_text("exclude: [x]\n", encoding="utf-8")
    assert taint.unknown_keys() == []


@pytest.mark.gate("G17")
def test_broken_config_with_a_secret_never_logged(app, home, caplog, capsys):  # noqa: F811
    p = cfg(home)
    p.write_text(BROKEN, encoding="utf-8")
    seen = []
    with caplog.at_level(logging.DEBUG):
        for f in (lambda: taint.write_config({"vault": "X"}), taint.restore_config):
            with pytest.raises(VaultError) as e:
                f()
            seen.append(str(e.value))
        seen += [str(taint.config_error()), json.dumps(doctor.check_exclude()),
                 str(taint.excluded(home)), json.dumps(call(app, "GET", "/api/status")[1]),
                 call(app, "POST", "/api/config/restore", {})[2].decode()]
        assert taint.unknown_keys() == []
    out = capsys.readouterr()
    assert "line 3" in seen[0] and "ScannerError" in seen[0]  # the place, never the text
    assert SECRET not in "\n".join(seen) + caplog.text + out.out + out.err
