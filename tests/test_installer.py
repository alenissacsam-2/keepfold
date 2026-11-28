"""installer/setup_app.py (KeepfoldSetup.exe) logic, with uv, the registry and shortcuts mocked,
and the `keepfold-app` launcher."""

import importlib.util
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

if sys.platform != "win32":  # installer/app_exe.py needs Windows' ctypes (HRESULT) to import
    pytest.skip("KeepfoldSetup.exe (installer/) is Windows only", allow_module_level=True)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "installer"))  # setup_app imports app_exe from beside it
_spec = importlib.util.spec_from_file_location("setup_app", ROOT / "installer" / "setup_app.py")
setup_app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup_app)
app_exe = setup_app.app_exe
REAL_MAKE_SHORTCUTS = setup_app.make_shortcuts
REAL_REGISTER = setup_app.register
REAL_RUN = setup_app.run

BIN = r"C:\Users\x\AppData\Local\Programs\Keepfold\bin"


@pytest.fixture(autouse=True)
def _no_real_system(monkeypatch):
    """Nothing here may touch this computer's registry, PATH, shortcuts, scheduled tasks or
    processes; a test that needs one of them fakes it (the `calls` fixture records them)."""
    def refuse(*a, **k):
        pytest.fail("a test touched the real system")
    for name in ("set_user_path", "register", "unregister", "make_shortcuts", "drop_login",
                 "terminate", "remove_later", "start_app", "run_get", "run_set", "run_delete",
                 "old_task", "drop_task", "_capture"):
        monkeypatch.setattr(setup_app, name, refuse)
    # never the real Apps & Features entries (this PC may hold a real Cortex install)
    monkeypatch.setattr(setup_app, "_location", lambda key: None)
    monkeypatch.setattr(setup_app, "test_copy", lambda: True)  # FX5: a test is a test copy


def test_merge_path_appends_once():
    assert setup_app.merge_path("", BIN) == BIN
    assert setup_app.merge_path(r"C:\a;C:\b", BIN) == rf"C:\a;C:\b;{BIN}"
    assert setup_app.merge_path(r"C:\a;", BIN) == rf"C:\a;{BIN}"
    for present in (BIN, BIN.upper() + "\\", f'"{BIN}"'):
        current = rf"%USERPROFILE%\bin;{present}"
        assert setup_app.merge_path(current, BIN) == current


def test_drop_path_keeps_the_rest():
    assert setup_app.drop_path(rf"C:\a;{BIN}\;%X%\y;{BIN.lower()}", BIN) == r"C:\a;%X%\y"
    assert setup_app.drop_path(BIN, BIN) == ""


def test_uv_env_keeps_everything_in_the_folder(tmp_path):
    env = setup_app.uv_env(tmp_path, {"PATH": "p", "UV_CACHE_DIR": "elsewhere"})
    assert env["PATH"] == "p"
    for var, sub in (("UV_TOOL_DIR", "tools"), ("UV_TOOL_BIN_DIR", "bin"),
                     ("UV_PYTHON_INSTALL_DIR", "python"), ("UV_CACHE_DIR", "cache")):
        assert env[var] == str(tmp_path / sub)
    assert env["UV_PYTHON_PREFERENCE"] == "only-managed"


def test_uv_env_fills_the_proxy_from_windows_settings_but_never_overrides(tmp_path,
                                                                          monkeypatch):
    """uv reads only HTTP(S)_PROXY; a school or work proxy set in Windows settings is copied
    there (getproxies reads the registry), and a proxy already in the environment wins."""
    system = {"http": "http://proxy.school:8080", "https": "http://proxy.school:8080"}
    monkeypatch.setattr(setup_app.urllib.request, "getproxies", lambda: system)
    env = setup_app.uv_env(tmp_path, {"PATH": "p"})
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == "http://proxy.school:8080"
    for mine in ({"https_proxy": "http://mine:1"}, {"HTTP_PROXY": "http://mine:1"}):
        env = setup_app.uv_env(tmp_path, dict(mine))
        assert {k: v for k, v in env.items() if k.upper().endswith("_PROXY")} == mine
    monkeypatch.setattr(setup_app.urllib.request, "getproxies", lambda: {})
    assert not any(k.endswith("_PROXY") for k in setup_app.uv_env(tmp_path, {"PATH": "p"}))


def test_a_failed_uv_run_names_its_error_before_the_internet_advice(tmp_path, payload, calls,
                                                                     monkeypatch):
    """The last `error:` line uv printed leads, so a local problem (a path, a full disk) does
    not read as a network one; the advice still follows."""
    child = ("print('Resolved 3 packages'); print('error: File not found: `C:\\\\John`'); "
             "raise SystemExit(2)")
    monkeypatch.setattr(setup_app, "run", lambda args, env, say, cwd=None: REAL_RUN(
        [sys.executable, "-c", child], env, say, cwd))
    d = tmp_path / "Keepfold"
    with pytest.raises(setup_app.SetupError) as e:
        setup_app.install(d, setup_app.parse_args(["--dir", str(d)]), lambda s: None,
                          src=payload)
    msg = str(e.value)
    assert msg.startswith("error: File not found: `C:\\John` ")
    assert "stopped with exit code 2." in msg and "internet connection" in msg
    with pytest.raises(setup_app.SetupError, match=r"^python\.exe stopped with exit code 3"):
        REAL_RUN([sys.executable, "-c", "raise SystemExit(3)"], {**os.environ}, lambda s: None)


def test_build_bundles_only_an_x64_uv(tmp_path):
    spec = importlib.util.spec_from_file_location("build", ROOT / "installer" / "build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)

    def pe(machine: bytes) -> Path:
        b = bytearray(512)
        b[:2] = b"MZ"
        b[0x3C:0x40] = (0x80).to_bytes(4, "little")
        b[0x80:0x86] = b"PE\0\0" + machine
        f = tmp_path / f"uv-{machine.hex()}.exe"
        f.write_bytes(bytes(b))
        return f
    assert build.is_x64(pe(b"\x64\x86"))
    assert not build.is_x64(pe(b"\x64\xaa"))  # ARM64 (0xAA64)
    assert not build.is_x64(pe(b"\x4c\x01"))  # x86 (0x014C)
    (tmp_path / "junk.exe").write_bytes(b"not a program")
    assert not build.is_x64(tmp_path / "junk.exe")


def test_parse_args(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    a = setup_app.parse_args([])
    assert a.dir == (tmp_path / "Programs" / "Keepfold").resolve()
    assert not (a.quiet or a.uninstall or a.no_path or a.no_shortcuts or a.no_registry)
    a = setup_app.parse_args(["--quiet", "--uninstall", "--dir", str(tmp_path / "c"),
                              "--no-path", "--no-shortcuts", "--no-registry"])
    assert a.quiet and a.uninstall and a.no_path and a.no_shortcuts and a.no_registry
    assert a.dir == (tmp_path / "c").resolve()


def test_uninstall_values_point_at_the_copied_setup(tmp_path):
    v = setup_app.uninstall_values(tmp_path, "0.1.0")
    assert v["DisplayVersion"] == "0.1.0" and v["InstallLocation"] == str(tmp_path)
    exe = tmp_path / "KeepfoldSetup.exe"
    assert v["UninstallString"] == f'"{exe}" --uninstall --dir "{tmp_path}"'
    assert v["DisplayIcon"] == str(tmp_path / "tools" / "keepfold" / "app" / "Keepfold.exe")
    assert setup_app.version_of(Path("keepfold-1.2.3-py3-none-any.whl")) == "1.2.3"


@pytest.fixture
def payload(tmp_path):
    p = tmp_path / "payload"
    p.mkdir()
    (p / "uv.exe").write_bytes(b"uv")
    (p / "keepfold-0.2.0-py3-none-any.whl").write_bytes(b"whl")
    return p


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(setup_app, "run",
                        lambda args, env, say, cwd=None: seen.append(("run", args, env, cwd)))
    monkeypatch.setattr(setup_app, "get_user_path", lambda: r"C:\a")
    monkeypatch.setattr(setup_app, "set_user_path", lambda v: seen.append(("path", v)))
    monkeypatch.setattr(setup_app, "make_shortcuts",
                        lambda d, say, existing_only=False: seen.append(("lnk", existing_only)))
    monkeypatch.setattr(app_exe, "make", lambda d, version, say: seen.append(("exe", version)))
    monkeypatch.setattr(setup_app, "register", lambda d, v: seen.append(("reg", v)))
    monkeypatch.setattr(setup_app, "unregister", lambda: seen.append(("unreg",)))
    monkeypatch.setattr(setup_app, "drop_login", lambda: seen.append(("login",)))
    monkeypatch.setattr(setup_app, "shortcut_paths", lambda: [])
    monkeypatch.setattr(setup_app, "in_use", lambda d, procs=None: [])
    return seen


def test_install_runs_uv_inside_the_folder(tmp_path, payload, calls):
    d = tmp_path / "Keepfold"
    (d / "cache").mkdir(parents=True)
    (d / setup_app.MARKER).write_text("x")  # an earlier install: an upgrade
    (d / "keepfold-0.1.0-py3-none-any.whl").write_bytes(b"old")
    opts = setup_app.parse_args(["--dir", str(d)])
    setup_app.install(d, opts, lambda s: None, src=payload)
    (_, args, env, cwd), *rest = calls
    assert [str(a) for a in args] == [str(d / "uv.exe"), "tool", "install", "--force",
                                      "--reinstall-package", "keepfold", "--python",
                                      "cpython-3.14-windows-x86_64-none",
                                      str(d / "keepfold-0.2.0-py3-none-any.whl")]
    assert cwd == d
    assert env["UV_TOOL_BIN_DIR"] == str(d / "bin")
    assert rest == [("exe", "0.2.0"), ("path", rf"C:\a;{d / 'bin'}"), ("lnk", False),
                    ("reg", "0.2.0")]
    assert sorted(p.name for p in d.iterdir()) == [
        "keepfold-0.2.0-py3-none-any.whl", "keepfold-install.txt", "uv.exe"]


def test_install_flags_skip_path_shortcuts_registry(tmp_path, payload, calls):
    d = tmp_path / "Keepfold"
    opts = setup_app.parse_args(["--dir", str(d), "--no-path", "--no-shortcuts",
                                 "--no-registry"])
    setup_app.install(d, opts, lambda s: None, src=payload)
    assert [c[0] for c in calls] == ["run", "exe"]


def test_install_refuses_a_folder_with_other_files(tmp_path, payload, calls):
    d = tmp_path / "Documents"
    d.mkdir()
    (d / "thesis.docx").write_bytes(b"mine")
    with pytest.raises(setup_app.SetupError, match="other files"):
        setup_app.install(d, setup_app.parse_args(["--dir", str(d)]), lambda s: None, payload)
    assert [p.name for p in d.iterdir()] == ["thesis.docx"] and calls == []


def test_uninstall_keeps_user_data(tmp_path, calls):
    home = Path.home()  # conftest's temp home
    (home / ".keepfold").mkdir()
    (home / ".keepfold" / "config.yaml").write_text("vault: v\n")
    vault = home / "LearnerVault"
    (vault / "notes").mkdir(parents=True)
    (vault / "notes" / "a.md").write_text("mine")
    d = tmp_path / "Keepfold"
    (d / "tools" / "keepfold" / "Lib").mkdir(parents=True)
    (d / "bin").mkdir()
    (d / "bin" / "keepfold.exe").write_bytes(b"x")
    (d / setup_app.MARKER).write_text("x")
    said = []
    later = setup_app.uninstall(d, setup_app.parse_args(["--uninstall", "--dir", str(d)]),
                                said.append)
    assert not d.exists() and later is False
    assert (home / ".keepfold" / "config.yaml").read_text() == "vault: v\n"
    assert (vault / "notes" / "a.md").read_text() == "mine"
    # the AI apps first (the installed keepfold.exe), then files, then the entries pointing at them
    assert [c[0] for c in calls] == ["run", "path", "login", "unreg"] and "kept" in said[-1]
    assert [str(a) for a in calls[0][1]] == [str(d / "bin" / "keepfold.exe"), "disconnect"]


def _install_tree(d: Path) -> None:
    for sub in ("bin", "tools/keepfold/Lib", "python/cpython-3.14", "cache"):
        (d / sub).mkdir(parents=True)
    for name in ("uv.exe", "constraints.txt", "KeepfoldSetup.exe",
                 "keepfold-0.2.0-py3-none-any.whl", setup_app.MARKER):
        (d / name).write_bytes(b"x")


def test_uninstall_deletes_only_what_setup_made(tmp_path, calls):
    """A3: a vault (or anything else) the student put in the install folder survives."""
    d = tmp_path / "Keepfold"
    _install_tree(d)
    (d / "notes").mkdir()
    (d / "notes" / "a.md").write_text("mine")
    (d / ".keepfold").mkdir()
    (d / "thesis.docx").write_bytes(b"mine")
    said = []
    opts = setup_app.parse_args(["--uninstall", "--keep-ai-apps", "--dir", str(d)])
    assert setup_app.uninstall(d, opts, said.append) is False
    assert sorted(p.name for p in d.iterdir()) == [".keepfold", "notes", "thesis.docx"]
    assert (d / "notes" / "a.md").read_text() == "mine"
    assert any("Keepfold did not make" in s for s in said)
    assert [c[0] for c in calls] == ["path", "login", "unreg"]  # --keep-ai-apps: no disconnect


def test_uninstall_deletes_files_first_and_says_how_to_retry(tmp_path, calls, monkeypatch):
    """A7: a locked file stops it before the shortcuts, PATH and Apps & Features entry go."""
    d = tmp_path / "Keepfold"
    _install_tree(d)
    real = setup_app.shutil.rmtree

    def locked(p, **k):
        if Path(p).name == "tools":
            raise PermissionError(13, "in use", str(p / "x.dll"))
        real(p, **k)
    monkeypatch.setattr(setup_app.shutil, "rmtree", locked)
    opts = setup_app.parse_args(["--uninstall", "--keep-ai-apps", "--dir", str(d)])
    with pytest.raises(setup_app.SetupError, match=r"x\.dll.*uninstall again from Installed apps"):
        setup_app.uninstall(d, opts, lambda s: None)
    assert calls == [] and (d / setup_app.MARKER).is_file()


def test_uninstall_refuses_while_keepfold_runs(tmp_path, calls, monkeypatch):
    d = tmp_path / "Keepfold"
    _install_tree(d)
    monkeypatch.setattr(setup_app, "in_use", lambda d, procs=None: [(7, str(d / "bin" / "x"))])
    opts = setup_app.parse_args(["--uninstall", "--keep-ai-apps", "--dir", str(d)])
    with pytest.raises(setup_app.SetupError, match="by 1 program"):
        setup_app.uninstall(d, opts, lambda s: None)
    assert calls == [] and (d / "bin").is_dir()


def test_a_failed_disconnect_is_only_a_warning(tmp_path, calls, monkeypatch):
    d = tmp_path / "Keepfold"
    _install_tree(d)
    (d / "bin" / "keepfold.exe").write_bytes(b"x")

    def fail(args, env, say, cwd=None):
        raise setup_app.SetupError("keepfold.exe stopped with exit code 1.")
    monkeypatch.setattr(setup_app, "run", fail)
    said = []
    setup_app.uninstall(d, setup_app.parse_args(["--uninstall", "--dir", str(d)]), said.append)
    assert not d.exists() and any(s.startswith("warning: some AI apps") for s in said)


def test_uninstall_running_from_the_folder_leaves_the_exe_to_cmd(tmp_path, calls, monkeypatch):
    """A8: the running KeepfoldSetup.exe stays for remove_later, which deletes only it."""
    d = tmp_path / "Keepfold"
    _install_tree(d)
    monkeypatch.setattr(setup_app.sys, "executable", str(d / "KeepfoldSetup.exe"))
    opts = setup_app.parse_args(["--uninstall", "--keep-ai-apps", "--dir", str(d)])
    assert setup_app.uninstall(d, opts, lambda s: None) is True
    assert [p.name for p in d.iterdir()] == ["KeepfoldSetup.exe"]
    seen = []
    monkeypatch.setattr(setup_app.subprocess, "Popen", lambda cmd, **k: seen.append(cmd))
    monkeypatch.setattr(setup_app, "remove_later", _real_remove_later)
    setup_app.remove_later(d)
    assert f'del /f /q "{d / "KeepfoldSetup.exe"}"' in seen[0] and f'rmdir "{d}"' in seen[0]
    assert "/s" not in seen[0].split("rmdir", 1)[1]  # never the folder's other files


_real_remove_later = setup_app.remove_later


def test_check_dir(tmp_path):
    d = tmp_path / "c"
    setup_app.check_dir(d)  # missing: fine
    d.mkdir()
    (d / "KeepfoldSetup.exe").write_bytes(b"x")  # what an uninstall left behind: fine
    setup_app.check_dir(d)
    (d / ".keepfold").mkdir()  # a vault: never, marker or not
    (d / setup_app.MARKER).write_text("x")
    with pytest.raises(setup_app.SetupError, match="is a Keepfold vault"):
        setup_app.check_dir(d)


def test_install_pins_dependencies_and_survives_a_shortcut_failure(tmp_path, payload, calls,
                                                                   monkeypatch):
    """A9: constraints.txt from uv.lock goes to `uv tool install -c`; A18: no shortcut is a
    warning, and Apps & Features still gets its entry."""
    (payload / "constraints.txt").write_text("numpy==2.0.0\n")

    def blocked(d, say, existing_only=False):
        raise OSError("a managed PC blocks the shortcut object")
    monkeypatch.setattr(setup_app, "make_shortcuts", blocked)
    d = tmp_path / "Keepfold"
    said = []
    setup_app.install(d, setup_app.parse_args(["--dir", str(d)]), said.append, src=payload)
    args = [str(a) for a in calls[0][1]]
    # relative, run in d: uv 0.9.15 splits a -c path that has a space (uv#22290)
    assert args[args.index("-c") + 1] == "constraints.txt" and calls[0][3] == d
    assert args[args.index("--python") + 1] == "cpython-3.14-windows-x86_64-none"
    assert (d / "constraints.txt").read_text() == "numpy==2.0.0\n"
    assert any(s.startswith("warning: no Start menu shortcut") for s in said)
    assert [c[0] for c in calls] == ["run", "exe", "path", "reg"]
    assert said[-1] == "Keepfold is installed."
    env = calls[0][2]
    assert env["UV_NATIVE_TLS"] == "1" and env["UV_COMPILE_BYTECODE"] == "1"


def test_uninstall_refuses_a_folder_without_the_marker(tmp_path, calls):
    d = tmp_path / "NotKeepfold"
    d.mkdir()
    (d / "keep.txt").write_text("x")
    with pytest.raises(setup_app.SetupError, match="nothing was removed"):
        setup_app.uninstall(d, setup_app.parse_args(["--dir", str(d)]), lambda s: None)
    assert (d / "keep.txt").exists() and calls == []


def test_quiet_main_reports_failure_by_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_app, "LOG", tmp_path / "setup.log")
    assert setup_app.main(["--quiet", "--uninstall", "--dir", str(tmp_path / "none")]) == 1
    assert "nothing was removed" in (tmp_path / "setup.log").read_text()


def test_parse_args_update_and_wait_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_app, "installed_dir", lambda: tmp_path / "reg")
    a = setup_app.parse_args(["--update", "--wait-pid", "4242"])
    assert a.update and a.wait_pid == 4242 and a.dir == (tmp_path / "reg").resolve()
    # the version in Apps & Features; the shortcuts still there are rewritten (make_shortcuts)
    assert a.no_path and not a.no_shortcuts and not a.no_registry
    a = setup_app.parse_args(["--update", "--dir", str(tmp_path / "x")])
    assert a.dir == (tmp_path / "x").resolve() and a.wait_pid is None
    assert setup_app.parse_args([]).wait_pid is None
    with pytest.raises(SystemExit):
        setup_app.parse_args(["--wait-pid", "app"])


def test_in_use_lists_only_programs_inside_the_folder(tmp_path):
    d = tmp_path / "Keepfold"
    procs = [(101, str(d / "bin" / "keepfold.exe")),
             (102, str(d / "TOOLS" / "keepfold" / "Scripts" / "python.exe").upper()),
             (103, str(tmp_path / "Keepfold2" / "bin" / "keepfold.exe")),  # a sibling, not inside
             (104, str(tmp_path / "Keepfold.exe")),
             (105, r"C:\Windows\explorer.exe"),
             (os.getpid(), str(d / "KeepfoldSetup.exe")),  # this setup and its one-file parent
             (os.getppid(), str(d / "KeepfoldSetup.exe"))]
    got = setup_app.in_use(d, procs)
    assert [pid for pid, _ in got] == ([101, 102] if os.name == "nt" else [101])
    assert setup_app.in_use(d, []) == []
    assert setup_app.in_use_text(got[:1]).startswith("Keepfold is in use by 1 program (")


def test_install_refuses_while_keepfold_runs_and_kills_nothing(tmp_path, payload, calls,
                                                             monkeypatch):
    d = tmp_path / "Keepfold"
    d.mkdir()
    (d / setup_app.MARKER).write_text("x")
    busy = [(7, str(d / "bin" / "keepfold.exe"))]
    monkeypatch.setattr(setup_app, "in_use", lambda d, procs=None: busy)
    monkeypatch.setattr(setup_app, "terminate", lambda pids: pytest.fail("killed without asking"))
    opts = setup_app.parse_args(["--quiet", "--update", "--dir", str(d)])
    with pytest.raises(setup_app.SetupError, match=r"by 1 program .*keepfold\.exe \(process 7\)"):
        setup_app.install(d, opts, lambda s: None, src=payload)
    assert calls == []


def test_update_needs_an_install_and_keeps_shortcuts_and_path(tmp_path, payload, calls):
    d = tmp_path / "Keepfold"
    opts = setup_app.parse_args(["--update", "--dir", str(d)])
    with pytest.raises(setup_app.SetupError, match="not installed"):
        setup_app.install(d, opts, lambda s: None, src=payload)
    d.mkdir()
    (d / setup_app.MARKER).write_text("x")
    said = []
    setup_app.install(d, opts, said.append, src=payload)
    # the shortcuts still there are rewritten for Keepfold.exe (older ones ran keepfold-app.exe)
    assert [c[0] for c in calls] == ["run", "exe", "lnk", "reg"] and calls[2] == ("lnk", True)
    assert said[-1] == ("Keepfold is updated. AI apps connected to Keepfold reconnect when you "
                        "restart them.")


def test_quiet_update_waits_for_the_app_then_reopens_it(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(setup_app, "LOG", tmp_path / "setup.log")
    monkeypatch.setattr(setup_app, "wait_pid", lambda pid: seen.append(("wait", pid)))
    monkeypatch.setattr(setup_app, "in_use", lambda d, procs=None: [])
    monkeypatch.setattr(setup_app, "install", lambda d, o, say: seen.append(("install", o.update)))
    monkeypatch.setattr(setup_app, "start_app", lambda d: seen.append(("open", d)))
    d = tmp_path / "Keepfold"
    assert setup_app.main(["--quiet", "--update", "--wait-pid", "9", "--dir", str(d)]) == 0
    assert seen == [("wait", 9), ("install", True), ("open", d.resolve())]


@pytest.mark.slow
def test_the_wheel_ships_the_receiver_and_no_extension(tmp_path):
    """A10: an installer install (the wheel) has the no-console receiver entry point; the
    capture extension is gone (0.3.2), so the wheel holds none of it."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    subprocess.run([uv, "build", "--wheel", "-o", str(tmp_path)], cwd=ROOT, check=True,
                   capture_output=True)
    z = zipfile.ZipFile(next(tmp_path.glob("keepfold-*.whl")))
    names = set(z.namelist())
    assert not [n for n in names if "/extension/" in n]
    ep = next(n for n in names if n.endswith(".dist-info/entry_points.txt"))
    assert "keepfold-receiver = keepfold.app.launch:receiver" in z.read(ep).decode()


def test_launcher_logs_without_a_console(monkeypatch):
    from keepfold.app import desktop, launch, server

    seen = {}
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "argv", [str(Path("C:/inst/bin/keepfold-app.exe")), "--hidden"])
    monkeypatch.setenv("PATH", "orig")
    monkeypatch.setenv("KEEPFOLD_VAULT", "V")
    monkeypatch.setattr(server, "running_url", lambda: None)

    def fake_run(root, hidden, echo, smoke):
        seen.update(root=root, hidden=hidden)
        echo("Keepfold app: http://127.0.0.1:1/")

    monkeypatch.setattr(desktop, "run", fake_run)
    launch.main()
    log = sys.stdout
    log.close()
    assert seen == {"root": Path("V"), "hidden": True}
    assert (Path.home() / ".keepfold" / "logs" / "app.log").read_text().startswith("Keepfold app:")
    assert os.environ["PATH"].startswith(str(Path("C:/inst/bin").resolve()) + os.pathsep)


def test_launcher_puts_the_install_bin_on_path(tmp_path, monkeypatch):
    """Keepfold.exe runs from the tool venv's folder: PATH gets the install's bin
    (`keepfold.exe`)."""
    from keepfold import update
    from keepfold.app import desktop, launch, server

    d = tmp_path / "Keepfold"
    exe = app_exe.path(d)
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    (d / update.MARKER).write_text("x")
    monkeypatch.setattr(sys, "prefix", str(app_exe.venv(d)))
    monkeypatch.setattr(sys, "executable", str(exe).upper())  # Windows names: any case
    monkeypatch.setattr(sys, "argv", [str(ROOT / "src" / "keepfold" / "app" / "launch.py")])
    monkeypatch.setenv("PATH", "orig")
    monkeypatch.setattr(server, "running_url", lambda: None)
    monkeypatch.setattr(desktop, "run", lambda root, hidden, echo, smoke: None)
    launch.main()
    assert os.environ["PATH"] == str((d / "bin").resolve()) + os.pathsep + "orig"


def test_launcher_hands_over_to_keepfold_exe(tmp_path, monkeypatch):
    """An older shortcut or Start with Windows value runs bin\\keepfold-app.exe: it starts the
    venv's Keepfold.exe with the same arguments, so the window belongs to Keepfold.exe."""
    from keepfold.app import desktop, launch

    venv = tmp_path / "keepfold"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "app").mkdir()
    (venv / "app" / "Keepfold.exe").write_bytes(b"MZ")
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "executable", str(venv / "Scripts" / "pythonw.exe"))
    monkeypatch.setattr(sys, "argv", ["keepfold-app.exe", "--hidden"])
    started = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda args, **kw: started.append(args))
    monkeypatch.setattr(desktop, "run", lambda *a, **k: pytest.fail("ran in the wrong process"))
    launch.main()
    assert started == [[str(venv / "app" / "Keepfold.exe"), "-m", "keepfold.app.launch",
                        "--hidden"]]


# ---- Keepfold.exe (installer/app_exe.py) -------------------------------------------------------

ICO = ROOT / "src" / "keepfold" / "assets" / "keepfold.ico"
windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows executables")


def test_icon_resources_split_the_ico():
    images, group = app_exe.icon_resources(ICO.read_bytes())
    assert len(images) == 7 and all(img.startswith(b"\x89PNG") for img in images)
    assert group[:6] == b"\0\0\1\0\7\0" and len(group) == 6 + 14 * 7
    w, h, _, _, planes, bits, size, rid = app_exe.struct.unpack_from("<BBBBHHIH", group, 6)
    assert (w, h, planes, bits, size, rid) == (16, 16, 1, 32, len(images[0]), 1)
    with pytest.raises(OSError):
        app_exe.icon_resources(b"MZ not an icon")


def test_version_resource_names_keepfold():
    v = app_exe.version_resource("0.2.13")
    assert app_exe.struct.unpack_from("<HHH", v) == (len(v), 52, 0)  # VS_FIXEDFILEINFO
    assert app_exe.struct.unpack_from("<4I", v, 40) == (0xFEEF04BD, 0x00010000, 2, 13 << 16)
    text = v.decode("utf-16-le", errors="replace")
    for key, value in (("FileDescription", "Keepfold"), ("ProductName", "Keepfold"),
                       ("FileVersion", "0.2.13"), ("OriginalFilename", "Keepfold.exe")):
        assert f"{key}\0" in text and f"{value}\0" in text


def _fake_tool(d: Path) -> Path:
    """The uv tool venv of an install, made from this test's own base Python."""
    v = app_exe.venv(d)
    (v / "Scripts").mkdir(parents=True)
    (v / "Scripts" / "keepfold.exe").write_bytes(b"uv's console script")  # never overwritten
    (v / "pyvenv.cfg").write_text(f"home = {Path(sys.base_prefix)}\nversion_info = 3.14\n")
    assets = app_exe.ico(d).parent
    assets.mkdir(parents=True)
    shutil.copy2(ICO, assets / "keepfold.ico")
    return v


@windows
def test_make_copies_and_stamps_a_keepfold_exe_that_runs_in_the_venv(tmp_path):
    d = tmp_path / "Keepfold"
    v = _fake_tool(d)
    said = []
    exe = app_exe.make(d, "0.2.13", said.append)
    assert exe == v / "app" / "Keepfold.exe" and not any("warning" in s for s in said)
    assert (v / "Scripts" / "keepfold.exe").read_bytes() == b"uv's console script"
    assert (exe.parent / f"python{sys.version_info[0]}{sys.version_info[1]}.dll").is_file()
    assert app_exe.read_resource(exe, app_exe.RT_VERSION, 1) == app_exe.version_resource("0.2.13")
    assert app_exe.read_resource(exe, app_exe.RT_GROUP_ICON, 1) == \
        app_exe.icon_resources(ICO.read_bytes())[1]
    assert b"longPathAware" in app_exe.read_resource(exe, app_exe.RT_MANIFEST, 1)
    assert app_exe.read_resource(exe, app_exe.RT_ICON, 8) is None  # pythonw's other icons gone
    out = subprocess.run([str(exe), "-c", "import sys; print(sys.prefix)"], capture_output=True,
                         text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert Path(out.stdout.strip()) == v  # the venv, from ..\pyvenv.cfg


@windows
def test_a_failed_stamp_keeps_a_working_exe(tmp_path):
    d = tmp_path / "Keepfold"
    _fake_tool(d)
    app_exe.ico(d).write_bytes(b"broken")
    said = []
    exe = app_exe.make(d, "0.2.13", said.append)
    assert exe.is_file() and any("keeps Python's icon" in s for s in said)


@windows
def test_shortcut_carries_the_app_id_and_icon(tmp_path):
    lnk, target = tmp_path / "Keepfold.lnk", tmp_path / "Keepfold.exe"
    target.write_bytes(b"MZ")
    app_exe.shortcut(lnk, target, "-m keepfold.app.launch", target)
    ps = ("$l = (New-Object -ComObject WScript.Shell).CreateShortcut($env:L); "
          "$l.TargetPath; $l.Arguments; $l.IconLocation; "
          "(New-Object -ComObject Shell.Application).NameSpace($env:D).ParseName('Keepfold.lnk')"
          ".ExtendedProperty('System.AppUserModel.ID')")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                         capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                         env={**os.environ, "L": str(lnk), "D": str(tmp_path)})
    assert out.stdout.splitlines() == [str(target), "-m keepfold.app.launch", f"{target},0",
                                       app_exe.APP_ID]
    assert app_exe.shortcut_target(lnk) == str(target)  # how an update finds its Keepfold.lnk
    with pytest.raises(OSError):
        app_exe.shortcut_target(tmp_path / "missing.lnk")


def _fake_links(monkeypatch, tmp_path):
    """Start menu and Desktop Keepfold.lnk paths; a fake .lnk holds its target as text."""
    menu, desk = tmp_path / "Programs" / "Keepfold.lnk", tmp_path / "Desktop" / "Keepfold.lnk"
    menu.parent.mkdir()
    desk.parent.mkdir()
    monkeypatch.setattr(setup_app, "shortcut_paths", lambda: [menu, desk])
    monkeypatch.setattr(app_exe, "shortcut",
                        lambda lnk, exe, args, icon: lnk.write_text(str(exe)))
    monkeypatch.setattr(app_exe, "shortcut_target", lambda lnk: lnk.read_text())
    return menu, desk


def test_an_update_renames_this_installs_keepfold_shortcuts(tmp_path, monkeypatch):
    menu, desk = _fake_links(monkeypatch, tmp_path)
    d = tmp_path / "Keepfold"
    exe = app_exe.path(d)
    for lnk in (menu, desk):  # 0.6.1's shortcuts, one to the older launcher
        lnk.with_name(setup_app.OLD_LNK).write_text(str(d / "bin" / "keepfold-app.exe"))
    said = []
    REAL_MAKE_SHORTCUTS(d, said.append, existing_only=True)
    for lnk in (menu, desk):
        assert sorted(p.name for p in lnk.parent.iterdir()) == ["Keepfold.lnk"]
        assert lnk.read_text() == str(exe)
    assert said == [f"shortcut: {menu}", f"shortcut: {desk}"]


def test_an_update_adds_no_shortcut_the_user_deleted(tmp_path, monkeypatch):
    menu, desk = _fake_links(monkeypatch, tmp_path)
    d = tmp_path / "Keepfold"
    menu.with_name(setup_app.OLD_LNK).write_text(str(app_exe.path(d)))
    other = desk.with_name(setup_app.OLD_LNK)  # another copy's (a sibling folder): not this one's
    other.write_text(str(tmp_path / "Keepfold2" / "bin" / "keepfold-app.exe"))
    REAL_MAKE_SHORTCUTS(d, lambda s: None, existing_only=True)
    assert sorted(p.name for p in menu.parent.iterdir()) == ["Keepfold.lnk"]
    assert sorted(p.name for p in desk.parent.iterdir()) == [setup_app.OLD_LNK]  # no Desktop one
    # a fresh install makes both, and still leaves another copy's old shortcut alone
    REAL_MAKE_SHORTCUTS(d, lambda s: None)
    assert sorted(p.name for p in desk.parent.iterdir()) == [setup_app.OLD_LNK, "Keepfold.lnk"]


def test_uninstall_removes_both_shortcut_names(tmp_path, calls, monkeypatch):
    menu, desk = _fake_links(monkeypatch, tmp_path)
    for lnk in (menu, desk, menu.with_name(setup_app.OLD_LNK),
                desk.with_name(setup_app.OLD_LNK)):
        lnk.write_text("x")
    d = tmp_path / "Keepfold"
    _install_tree(d)
    opts = setup_app.parse_args(["--uninstall", "--keep-ai-apps", "--dir", str(d)])
    setup_app.uninstall(d, opts, lambda s: None)
    assert list(menu.parent.iterdir()) == [] and list(desk.parent.iterdir()) == []


def test_apps_and_features_shows_keepfold_under_the_same_key(tmp_path, monkeypatch):
    seen = {}

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def create(root, path):
        seen["key"] = (root, path)
        return Key()
    fake = SimpleNamespace(HKEY_CURRENT_USER="HKCU", REG_SZ=1, REG_DWORD=4, CreateKey=create,
                           SetValueEx=lambda k, name, _, kind, value: seen.update({name: value}))
    monkeypatch.setitem(sys.modules, "winreg", fake)  # never the real registry
    REAL_REGISTER(tmp_path, "0.6.2")
    assert seen["key"] == ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Keepfold")
    assert seen["DisplayName"] == "Keepfold" and seen["Publisher"] == "Keepfold"
    assert seen["DisplayVersion"] == "0.6.2"


def test_make_shortcuts_on_update_rewrites_only_those_still_there(tmp_path, monkeypatch):
    menu, desk = tmp_path / "Programs" / "Keepfold.lnk", tmp_path / "Desktop" / "Keepfold.lnk"
    menu.parent.mkdir()
    menu.write_bytes(b"old")
    monkeypatch.setattr(setup_app, "shortcut_paths", lambda: [menu, desk])
    made = []
    monkeypatch.setattr(app_exe, "shortcut", lambda lnk, exe, args, icon: made.append(
        (lnk, exe, args, icon)))
    d = tmp_path / "Keepfold"
    REAL_MAKE_SHORTCUTS(d, lambda s: None, existing_only=True)
    exe = app_exe.path(d)
    assert made == [(menu, exe, "-m keepfold.app.launch", exe)]
    REAL_MAKE_SHORTCUTS(d, lambda s: None)
    assert [m[0] for m in made] == [menu, menu, desk]


# ---- an upgrade over Cortex (0.6.1 and older) -------------------------------------------------

OLD_KEY = setup_app.OLD_UNINSTALL_KEY


@pytest.fixture
def upgrade(calls, monkeypatch):
    """A Cortex install's Windows, faked: Apps & Features entries (`keys`), the HKCU Run key
    (`run`), the old Task Scheduler entry's arguments (`task`) and `keepfold migrate --json`'s
    (exit code, stdout) (`answer`); `calls` records register, unregister, migrate and task."""
    w = SimpleNamespace(keys={}, run={}, task=None, answer=(0, '{"message": "Moved."}\n'))
    monkeypatch.setattr(setup_app, "_location", lambda key: w.keys.get(key))
    monkeypatch.setattr(setup_app, "register", lambda d, v: (
        calls.append(("reg", v)), w.keys.__setitem__(setup_app.UNINSTALL_KEY, d)))
    monkeypatch.setattr(setup_app, "unregister", lambda key=setup_app.UNINSTALL_KEY: (
        calls.append(("unreg", key)), w.keys.pop(key, None)))
    monkeypatch.setattr(setup_app, "test_copy", lambda: False)
    monkeypatch.setattr(setup_app, "run_get", lambda name: w.run.get(name))
    monkeypatch.setattr(setup_app, "run_set", lambda name, value: w.run.__setitem__(name, value))
    monkeypatch.setattr(setup_app, "run_delete", lambda name: w.run.pop(name, None))
    monkeypatch.setattr(setup_app, "old_task", lambda: w.task)
    monkeypatch.setattr(setup_app, "drop_task", lambda name: (
        calls.append(("task", name)), setattr(w, "task", None)))

    def capture(args, timeout):
        calls.append(("migrate", [str(a) for a in args]))
        return w.answer
    monkeypatch.setattr(setup_app, "_capture", capture)
    return w


def _cortex_install(d: Path) -> Path:
    """What CortexSetup.exe 0.6.1 left in `d`."""
    for sub in ("bin", "tools/cortex-learner/app", "python/cpython-3.12", "cache"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    for name in ("uv.exe", "constraints.txt", "CortexSetup.exe", setup_app.OLD_MARKER,
                 "cortex_learner-0.6.1-py3-none-any.whl", "bin/cortex.exe", "bin/cortex-app.exe",
                 "bin/cortex-receiver.exe", "tools/cortex-learner/app/Cortex.exe"):
        (d / name).write_bytes(b"x")
    return d


def _old_default(tmp_path, monkeypatch) -> Path:
    """%LOCALAPPDATA% of a user whose name has a space and an accent; returns Programs\\Cortex
    there (not made)."""
    local = tmp_path / "Users" / "José Smith" / "AppData" / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    return local / "Programs" / "Cortex"


def test_upgrade_from_the_default_folder(tmp_path, payload, calls, upgrade, monkeypatch):
    """C:\\Users\\José Smith\\AppData\\Local\\Programs\\Cortex: Keepfold goes to
    Programs\\Keepfold, the data moves, then the old folder, shortcut, PATH entry and uninstall
    entry go and Start with Windows / Start at login start Keepfold instead."""
    old = _cortex_install(_old_default(tmp_path, monkeypatch))
    upgrade.keys[OLD_KEY] = old
    vault = r"C:\Users\José Smith\Cortex"
    old_app = old / "tools" / "cortex-learner" / "app" / "Cortex.exe"
    upgrade.run.update({"Cortex": f'"{old_app}" -m cortex.app.launch --hidden',
                        "Cortex browse receiver": f'"{old / "bin" / "cortex-receiver.exe"}" '
                                                  f'--vault "{vault}"', "OneDrive": "o"})
    monkeypatch.setattr(setup_app, "get_user_path", lambda: f"C:\\a;{old / 'bin'}")
    menu, desk = _fake_links(monkeypatch, tmp_path)
    menu.with_name(setup_app.OLD_LNK).write_text(str(old_app))
    desk.with_name(setup_app.OLD_LNK).write_text(r"C:\Elsewhere\Cortex.exe")  # not this one's
    opts = setup_app.parse_args([])
    d = opts.dir
    assert d == (old.parent / "Keepfold").resolve()
    said = []
    assert setup_app.install(d, opts, said.append, src=payload) == (0, "Moved.")
    assert not old.exists() and (d / setup_app.MARKER).is_file()
    assert upgrade.run == {
        "OneDrive": "o", "Keepfold": f'"{app_exe.path(d)}" -m keepfold.app.launch --hidden',
        "Keepfold browse receiver": f'"{d / "bin" / "keepfold-receiver.exe"}" --vault "{vault}"'}
    assert upgrade.keys == {setup_app.UNINSTALL_KEY: d}  # the old entry is gone
    assert not menu.with_name(setup_app.OLD_LNK).exists()
    assert desk.with_name(setup_app.OLD_LNK).exists()
    # migrate only after the new install, and the old install goes only after it answered
    assert [c[0] for c in calls] == ["run", "exe", "path", "lnk", "reg", "migrate", "path",
                                     "unreg"]
    assert calls[5][1] == [str(d / "bin" / "keepfold.exe"), "migrate", "--json"]
    assert calls[6] == ("path", "C:\\a") and calls[7] == ("unreg", OLD_KEY)
    assert said[-1] == "Keepfold is installed."


def test_upgrade_in_a_custom_folder_keeps_the_folder(tmp_path, payload, calls, upgrade):
    """C:\\Cortex, chosen by the user: Keepfold goes into the same folder, which is never
    renamed; only the files with the old names go."""
    old = _cortex_install(tmp_path / "My Cortex")
    (old / "notes.txt").write_text("mine")
    upgrade.keys[OLD_KEY] = old
    opts = setup_app.parse_args([])
    d = opts.dir
    assert d == old.resolve()
    assert setup_app.install(d, opts, lambda s: None, src=payload) == (0, "Moved.")
    assert sorted(p.name for p in d.iterdir()) == [
        "bin", "constraints.txt", "keepfold-0.2.0-py3-none-any.whl", "keepfold-install.txt",
        "notes.txt", "python", "tools", "uv.exe"]
    assert list((d / "bin").iterdir()) == list((d / "tools").iterdir()) == []
    assert (d / "notes.txt").read_text() == "mine"
    assert upgrade.keys == {setup_app.UNINSTALL_KEY: d}
    # one PATH entry, the same folder's bin: nothing to drop
    assert [c[0] for c in calls] == ["run", "exe", "path", "lnk", "reg", "migrate", "unreg"]


def test_upgrade_asks_before_closing_a_running_cortex(tmp_path, payload, calls, upgrade,
                                                       monkeypatch):
    old = _cortex_install(_old_default(tmp_path, monkeypatch)).resolve()
    upgrade.keys[OLD_KEY] = old
    busy = [(7, str(old / "tools" / "cortex-learner" / "app" / "Cortex.exe"))]
    monkeypatch.setattr(setup_app, "in_use", lambda d, procs=None: busy if d == old else [])
    d = setup_app.parse_args([]).dir
    assert setup_app.in_use_all(d) == busy and setup_app.folders(d) == [d, old]
    with pytest.raises(setup_app.SetupError, match=r"by 1 program .*Cortex\.exe \(process 7\)"):
        setup_app.install(d, setup_app.parse_args([]), lambda s: None, src=payload)
    assert calls == [] and (old / setup_app.OLD_MARKER).is_file() and not d.exists()
    # the window names the old programs as it names Keepfold's
    progs = [(1, str(old / "bin" / "cortex.exe")), *busy, (3, str(old / "bin" / "cortex-app.exe")),
             (4, str(old / "bin" / "cortex-receiver.exe"))]
    assert sp.group_in_use(progs, {}) == ["Claude's Keepfold connection (1)",
                                          "The Keepfold window (2)", "The browsing receiver (1)"]


def test_upgrade_without_start_with_windows_creates_none(tmp_path, payload, calls, upgrade):
    old = _cortex_install(tmp_path / "Cortex")
    upgrade.keys[OLD_KEY] = old
    upgrade.run["OneDrive"] = "o"
    opts = setup_app.parse_args([])
    setup_app.install(opts.dir, opts, lambda s: None, src=payload)
    assert upgrade.run == {"OneDrive": "o"} and ("task", setup_app.OLD_LOGIN) not in calls


def test_upgrade_carries_the_old_scheduled_task_over(tmp_path, payload, calls, upgrade):
    """Versions before the Run value started the receiver from Task Scheduler."""
    old = _cortex_install(tmp_path / "Cortex")
    upgrade.keys[OLD_KEY] = old
    upgrade.task = 'browse serve --vault "D:\\Notes Vault"'
    opts = setup_app.parse_args([])
    setup_app.install(opts.dir, opts, lambda s: None, src=payload)
    recv = opts.dir / "bin" / "keepfold-receiver.exe"
    assert upgrade.run == {"Keepfold browse receiver": f'"{recv}" --vault "D:\\Notes Vault"'}
    assert ("task", "Cortex browse receiver") in calls and upgrade.task is None


def test_receiver_command_keeps_the_vault():
    d = Path(r"C:\K")
    recv = r'"C:\K\bin\keepfold-receiver.exe"'
    assert setup_app.receiver_command(d, r'"C:\C\bin\cortex.exe" browse serve --vault "V"') == \
        recv + ' --vault "V"'
    assert setup_app.receiver_command(d, "") == recv


def test_a_crash_midway_then_a_rerun_finishes(tmp_path, payload, calls, upgrade, monkeypatch):
    """Setup dies after the new install, before the data moved: nothing old is gone yet. A
    locked old file later keeps the old marker and entry. Each rerun picks up where it was."""
    old = _cortex_install(_old_default(tmp_path, monkeypatch))
    upgrade.keys[OLD_KEY] = old
    upgrade.run["Cortex"] = "old"
    real_drop = setup_app.drop_old_files

    def power_cut(*a, **k):
        raise RuntimeError("power cut")
    monkeypatch.setattr(setup_app, "drop_old_files", power_cut)
    d = setup_app.parse_args([]).dir
    with pytest.raises(RuntimeError):
        setup_app.install(d, setup_app.parse_args([]), lambda s: None, src=payload)
    assert (old / setup_app.OLD_MARKER).is_file() and OLD_KEY in upgrade.keys
    assert upgrade.run == {"Cortex": "old"} and ("migrate" not in [c[0] for c in calls])
    monkeypatch.setattr(setup_app, "drop_old_files", real_drop)

    real_rmtree = setup_app.shutil.rmtree

    def locked(p, **k):
        if Path(p) == old.resolve() / "tools":
            raise PermissionError(13, "in use", str(Path(p) / "x.dll"))
        real_rmtree(p, **k)
    monkeypatch.setattr(setup_app.shutil, "rmtree", locked)
    opts = setup_app.parse_args([])
    assert opts.dir == d  # the rerun finds Keepfold where the first run put it
    said = []
    assert setup_app.install(d, opts, said.append, src=payload) == (0, "Moved.")
    assert any("x.dll is in use, so the old program stays" in s for s in said)
    assert (old / setup_app.OLD_MARKER).is_file() and OLD_KEY in upgrade.keys
    assert "Cortex" not in upgrade.run and "Keepfold" in upgrade.run

    monkeypatch.setattr(setup_app.shutil, "rmtree", real_rmtree)
    setup_app.install(d, setup_app.parse_args([]), lambda s: None, src=payload)
    assert not old.exists() and upgrade.keys == {setup_app.UNINSTALL_KEY: d}


@pytest.mark.parametrize("code, out, moved", [
    (0, 'copying\n{"ok": true, "message": "Moved your data."}\n', (0, "Moved your data.")),
    (2, '{"message": "Close Cortex first."}', (2, "Close Cortex first.")),
    (1, '{"message": "The vault is read-only."}', (1, "The vault is read-only.")),
    (1, "Traceback (most recent call last):\nImportError: no yaml\n", None)])
def test_migrate_exit_codes(tmp_path, payload, calls, upgrade, code, out, moved):
    """0 moved, 2 blocked by an old Cortex (Retry), 1 failed (keepfold doctor): the install is
    done either way. No answer at all: the new install may not run, so the old one stays."""
    old = _cortex_install(tmp_path / "Cortex")
    upgrade.keys[OLD_KEY] = old
    upgrade.answer = (code, out)
    opts = setup_app.parse_args([])
    said = []
    got = setup_app.install(opts.dir, opts, said.append, src=payload)
    assert said[-1] == "Keepfold is installed."
    if moved:
        assert got == moved
        assert not (old / setup_app.OLD_MARKER).exists() and OLD_KEY not in upgrade.keys
    else:
        assert got == (1, "keepfold.exe stopped without an answer (ImportError: no yaml).")
        assert (old / setup_app.OLD_MARKER).is_file() and OLD_KEY in upgrade.keys
        assert any(s.startswith("warning: the old Cortex program") for s in said)


def test_moved_text_and_the_quiet_setup_say_what_to_do(tmp_path, monkeypatch):
    assert setup_app.moved_text(2, "Close Cortex first.") == "Close Cortex first."
    text = setup_app.moved_text(1, "The vault is read-only.")
    assert text.startswith("Keepfold is installed, but") and "keepfold doctor" in text
    monkeypatch.setattr(setup_app, "LOG", tmp_path / "setup.log")
    for moved, words in (((2, "Close Cortex first."), "Close Cortex first."),
                         ((1, "x"), "keepfold doctor"), (None, "")):
        monkeypatch.setattr(setup_app, "install", lambda d, o, say, m=moved: m)
        assert setup_app.main(["--quiet", "--dir", str(tmp_path / "K")]) == 0  # installed
        assert words in (tmp_path / "setup.log").read_text(encoding="utf-8")
    assert setup_app.LOG.name == "setup.log"  # the patched one; the real one:
    assert "cortex-setup.log" not in (ROOT / "installer" / "setup_app.py").read_text("utf-8")


def test_an_old_folder_is_removed_only_with_its_marker(tmp_path, payload, calls, upgrade,
                                                         monkeypatch):
    old = _old_default(tmp_path, monkeypatch)
    (old / "bin").mkdir(parents=True)
    (old / "bin" / "cortex.exe").write_bytes(b"x")
    (old / "thesis.docx").write_bytes(b"mine")
    opts = setup_app.parse_args([])
    assert setup_app.install(opts.dir, opts, lambda s: None, src=payload) is None
    assert sorted(p.name for p in old.iterdir()) == ["bin", "thesis.docx"]  # not an install
    assert "migrate" not in [c[0] for c in calls]
    # with the marker: what Cortex made goes, the student's own file and the folder stay
    _cortex_install(old)
    said = []
    setup_app.install(opts.dir, setup_app.parse_args([]), said.append, src=payload)
    assert sorted(p.name for p in old.iterdir()) == ["thesis.docx"]
    assert any("still holds files Cortex did not make" in s for s in said)


def test_a_test_copy_leaves_run_values_and_tasks_alone(tmp_path, payload, calls, upgrade,
                                                       monkeypatch):
    """FX5: with the home folder moved to a temp folder, the Run values and Task Scheduler
    are never changed, and the real user's Cortex install (its Apps & Features entry) is
    neither closed nor removed: a test copy does not see it."""
    monkeypatch.setattr(setup_app, "test_copy", lambda: True)
    old = _cortex_install(tmp_path / "Cortex")
    files = sorted(p.name for p in old.iterdir())
    upgrade.keys[OLD_KEY] = old
    upgrade.run.update({"Cortex": "a", "Cortex browse receiver": 'r --vault "V"'})
    upgrade.task = '--vault "V"'
    assert setup_app.old_install() is None and setup_app.folders(old) == [old]
    said = []
    opts = setup_app.parse_args([])
    assert setup_app.install(opts.dir, opts, said.append, src=payload) is None  # no migrate
    assert sorted(p.name for p in old.iterdir()) == files and OLD_KEY in upgrade.keys
    assert upgrade.run == {"Cortex": "a", "Cortex browse receiver": 'r --vault "V"'}
    assert upgrade.task == '--vault "V"' and "task" not in [c[0] for c in calls]


def test_a_fresh_install_with_no_cortex_is_unchanged(tmp_path, payload, calls, upgrade):
    opts = setup_app.parse_args([])
    assert opts.dir == setup_app.default_dir().resolve()
    said = []
    assert setup_app.install(opts.dir, opts, said.append, src=payload) is None
    assert [c[0] for c in calls] == ["run", "exe", "path", "lnk", "reg"]
    assert upgrade.run == {} and said[-1] == "Keepfold is installed."


# ---- the setup window's pure parts (installer/setup_progress.py) ------------------------------

sp = setup_app.sp
# install()'s own lines around a first install's uv output, as the log has them
FIRST_INSTALL = r"""Installing Keepfold 0.6.0 into C:\X\Keepfold (downloads Python and packages)...
running: C:\X\Keepfold\uv.exe tool install --force --reinstall-package keepfold
Downloading cpython-3.14.0-windows-x86_64-none (download) (20.4MiB)
 Downloaded cpython-3.14.0-windows-x86_64-none (download)
Resolved 86 packages in 2.31s
Downloading numpy (12.3MiB)
Downloading onnxruntime (11.9MiB)
 Downloaded numpy
Downloading pillow (6.8MiB)
 Downloaded pillow
 Downloaded onnxruntime
Prepared 86 packages in 41.20s
Installed 86 packages in 5.37s
Bytecode compiled 9233 files in 14.02s
 + numpy==2.2.6
 + keepfold==0.6.0
Installed 3 executables: keepfold, keepfold-app, keepfold-receiver
app: C:\X\Keepfold\tools\keepfold\app\Keepfold.exe
added to your PATH: C:\X\Keepfold\bin (new terminals know keepfold)
shortcut: C:\X\Programs\Keepfold.lnk
added to Apps & Features (uninstall from there)
Keepfold is installed.""".splitlines()


def _run(state, lines):
    seen = [state]
    for line in lines:
        seen.append(sp.progress(seen[-1], line))
    return seen


def test_progress_follows_a_first_install_to_the_end():
    seen = _run(sp.start("install", "0.6.0"), FIRST_INSTALL)
    fractions = [s.fraction for s in seen]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0
    order = [k for k, _ in sp.steps("install")]
    assert [order.index(s.step) for s in seen] == sorted(order.index(s.step) for s in seen)
    assert seen[-1].step == "done" and seen[-1].status == "Keepfold is installed."
    statuses = [s.status for s in seen]
    assert "Downloading Python 3.14 and packages (first install only)…" in statuses
    assert "Installing 86 packages…" in statuses and "Making shortcuts…" in statuses
    # package downloads move the bar inside the install step
    assert seen[8].fraction > seen[5].fraction
    assert sp.progress(seen[5], "Some new uv line") == seen[5]  # an unknown line: no move


def test_progress_of_an_update_and_a_late_line():
    s = sp.start("update", "0.6.0")
    assert (s.step, s.fraction, s.status) == ("wait", 0.0, "Waiting for Keepfold to close…")
    lines = ["waiting for process 9 (the Keepfold app) to exit",
             "closing 2 programs that use Keepfold: x (process 1); y (process 2)",
             FIRST_INSTALL[0], "Resolved 80 packages in 3.57s",
             "Prepared 1 package in 324ms", "Uninstalled 1 package in 118ms",
             "Installed 1 package in 487ms", "Bytecode compiled 3490 files in 1.90s",
             "Installed 3 executables: keepfold, keepfold-app, keepfold-receiver",
             r"app: C:\X\Keepfold.exe", "Keepfold is updated. AI apps connected to Keepfold "
             "reconnect when you restart them.", "opened the Keepfold app"]
    seen = _run(s, lines)
    assert [x.fraction for x in seen] == sorted(x.fraction for x in seen)
    assert [x.step for x in seen[1:4]] == ["wait", "close", "install"]
    assert seen[-2].status == "Keepfold is updated to 0.6.0. Opening it…"
    assert (seen[-1].step, seen[-1].fraction) == ("open", 1.0)
    assert seen[-1].status == ("Keepfold is updated to 0.6.0. Restart Claude and your other AI "
                               "apps so they reconnect.")
    # a step never goes back: a late "waiting" line during the install changes nothing
    assert sp.progress(seen[5], "waiting for process 9 (the Keepfold app) to exit") == seen[5]
    # an install has no "close" row: closing programs there keeps its step, and says so
    s = sp.progress(sp.start("install"), "closing 1 program that use Keepfold: x")
    assert (s.step, s.fraction, s.status) == ("folder", 0, "Closing programs that use Keepfold…")


def test_step_lists_and_elapsed():
    assert sp.steps("update", "0.6.0") == [
        ("wait", "Waiting for Keepfold to close"), ("close", "Closing programs that use Keepfold"),
        ("install", "Installing Keepfold 0.6.0"), ("finish", "Finishing up"),
        ("open", "Opening Keepfold")]
    assert sp.steps("install", "0.6.0") == [
        ("folder", "Choose a folder"),
        ("install", "Installing Keepfold 0.6.0 (downloads Python and packages the first time)"),
        ("finish", "Finishing up"), ("done", "Done")]
    assert sp.steps("install")[1][1].startswith("Installing Keepfold (downloads")  # no wheel
    for mode in ("install", "update"):  # every row has its share of the bar, in order
        assert [k for k, _ in sp.steps(mode)] == list(sp.SPANS[mode])
    assert [sp.elapsed(t) for t in (0, 8.9, 60, 80)] == ["0 s", "8 s", "1 min 0 s",
                                                          "1 min 20 s"]


def test_in_use_is_grouped_in_plain_words():
    d = r"C:\Users\x\AppData\Local\Programs\Keepfold"
    busy = [(11, d + r"\bin\keepfold.exe"),
            (12, d + r"\tools\keepfold\Scripts\python.exe"),  # keepfold.exe's child
            (13, d + r"\python\cpython-3.14\python.exe"),  # and its grandchild
            (21, d + r"\tools\keepfold\app\Keepfold.exe"),
            (31, d + r"\bin\keepfold-receiver.exe"),
            (41, d + r"\uv.exe")]
    parents = {11: 5, 12: 11, 13: 12, 21: 6, 31: 7, 41: 8}
    assert sp.group_in_use(busy, parents) == [
        "Claude's Keepfold connection (1)", "The Keepfold window (1)", "The browsing receiver (1)",
        "Other Keepfold programs (1)"]
    two = [(11, d + r"\bin\keepfold.exe"), (12, d + r"\BIN\KEEPFOLD.EXE"),
           (13, d + r"\bin\keepfold-app.exe")]
    assert sp.group_in_use(two, {}) == ["Claude's Keepfold connection (2)",
                                        "The Keepfold window (1)"]
    assert sp.group_in_use([], {}) == []
    loop = [(1, d + r"\bin\keepfold.exe"), (2, d + r"\tools\python.exe")]
    assert sp.group_in_use(loop, {1: 2, 2: 1}) == ["Other Keepfold programs (2)"]  # pid reuse


def test_the_window_icon_is_bundled_and_found(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("build", ROOT / "installer" / "build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    args = build.data(tmp_path / "payload")
    pairs = [args[i + 1] for i, a in enumerate(args) if a == "--add-data"]
    assert f"{ICO}{os.pathsep}." in pairs and build.ICON == ICO and ICO.is_file()
    assert args[args.index("--icon") + 1] == str(ICO)  # KeepfoldSetup.exe's own icon too
    assert "*data(payload)" in (ROOT / "installer" / "build.py").read_text()  # in the command
    assert setup_app.icon_path() == ICO  # from source
    (tmp_path / "keepfold.ico").write_bytes(ICO.read_bytes())
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert setup_app.icon_path() == tmp_path / "keepfold.ico"  # the one-file exe's copy


def test_the_install_window_asks_and_fails_inside_itself():
    """No system message box in the install and update window: the in-use question and
    errors are panels in it (uninstall keeps Windows' own questions)."""
    import inspect
    src = inspect.getsource(setup_app.window)
    assert "messagebox" not in src and "ask_close" not in src
    assert "group_in_use" in src and "Copy log" in src
    # an old Cortex install's programs are asked about and closed too; a blocked move: Retry
    assert "in_use_all(opts.dir)" in src and "*folders(opts.dir)" in src
    assert '("Retry", retry, True)' in src


def test_the_update_window_waits_for_the_app_itself(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(setup_app, "LOG", tmp_path / "setup.log")
    monkeypatch.setattr(setup_app, "wait_pid", lambda pid: seen.append(("wait", pid)))
    monkeypatch.setattr(setup_app, "window", lambda o, say: seen.append(("window", o.update)) or 0)
    d = str(tmp_path / "Keepfold")
    assert setup_app.main(["--update", "--wait-pid", "9", "--dir", d]) == 0
    assert seen == [("window", True)]  # its first step, so the window is never blank
    monkeypatch.setattr(setup_app, "in_use", lambda d, procs=None: [])
    seen.clear()
    assert setup_app.main(["--wait-pid", "9", "--dir", d]) == 0
    assert seen == [("wait", 9), ("window", False)]


class _Widget:
    def __init__(self, cls):
        self.cls, self.ran = cls, 0

    def winfo_class(self):
        return self.cls

    def invoke(self):
        self.ran += 1


def test_enter_runs_the_focused_button_else_the_lit_one():
    lit = []
    back = _Widget("TButton")
    setup_app.press_enter(back, lambda: lit.append(1))
    assert back.ran == 1 and lit == []  # Tab moved focus to Back: Enter means Back
    for focused in (_Widget("TEntry"), None):
        setup_app.press_enter(focused, lambda: lit.append(1))
    assert lit == [1, 1]
    setup_app.press_enter(None, None)  # no lit button: nothing happens
