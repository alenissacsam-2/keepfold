"""A2: `App`'s route methods live in mixins under `keepfold.app.routes`; `server.py` keeps the
constructor with its get/post tables, the jobs, the handler and the methods that read names tests
patch on the server module. Nothing may get lost or shadowed in the split."""

import ast
import inspect
from pathlib import Path

import pytest

from keepfold.app import routes, server

ROUTES = Path(routes.__file__).parent
MIXINS = ("home", "review", "xray", "chat", "setup", "imports", "system")
# `dir(App)` before the split (non-dunder names): every one must still be there
OLD_NAMES = {
    "CARDS", "COLD_ANSWERS", "DATE_KINDS", "RELATED", "_check", "_click", "_clicks",
    "_close_dry", "_decision", "_drop", "_dry_index", "_home", "_home_prefs", "_import",
    "_line_group", "_live", "_model_disk", "_model_runner", "_notify_update", "_outside",
    "_paused_rows", "_private", "_producer", "_promoted", "_related", "_rule", "_section",
    "_skip_profiles", "_swap", "_today", "_vault", "_waiting", "_waiting_group", "_want",
    "_watched", "_with_core", "_xray_client", "_xray_set", "app_move", "app_uninstall",
    "auto_import", "auto_settings", "autostart", "autostart_set", "backup", "brain_picture",
    "browse", "browse_settings", "cancel", "capture", "capture_hide", "chat_attach",
    "chat_begin", "chat_delete", "chat_eval", "chat_file_remove", "chat_file_save", "chat_get",
    "chat_import_auto", "chat_import_set", "chat_list", "chat_pages", "chat_passage",
    "chat_settings", "chat_settings_get", "chat_state", "cloud", "cold_answer", "cold_check",
    "connect", "connect_apply", "connect_path", "connect_plan", "doctor", "drop_dry", "entry",
    "export", "forget_due", "git_settings", "history_import", "home", "home_date", "home_delta",
    "home_fact", "home_layout", "home_site", "home_version", "hotkey", "hotkey_set",
    "import_preview", "import_run", "llm", "llm_load", "llm_pull", "llm_settings", "llm_size",
    "llm_test", "lock", "login", "need_root", "news_seen", "notify", "notify_set", "open",
    "pack_copied", "pickup", "pickup_add", "pickup_dismiss", "pickup_later", "piggyback",
    "private_chats", "private_unlocked", "process", "profiles_set", "quit", "review",
    "review_approve", "review_conflict", "review_crowded_keep", "review_decision_approve",
    "review_decision_dismiss", "review_dismiss", "review_forget_dismiss",
    "review_import_dismiss", "review_outside_keep", "review_outside_remove",
    "review_playbook_keep", "review_promote_later", "review_promote_pin", "review_resume",
    "review_retire", "review_rule_approve", "review_rule_approve_all", "review_rule_dismiss",
    "review_still", "review_unsafe_keep", "safety_net_auto", "scan", "selftest", "setup",
    "setup_embed", "setup_git", "setup_index", "setup_install", "setup_model", "setup_pull",
    "setup_skip", "setup_start", "setup_test", "setup_vault", "setup_voice", "snapshot",
    "status", "status_report", "tidy_about_me", "tonight", "tonight_undo", "touch", "unlock",
    "update_apply", "update_check", "update_check_set", "update_quietly", "update_state",
    "vault", "warm_home", "warm_status", "watch_add", "watch_auto", "watch_remove", "welcome",
    "welcome_set", "window_capture", "window_show", "xray", "xray_ack", "xray_compare",
    "xray_dryrun", "xray_mute", "xray_pin", "xray_trace",
}
# names tests patch on `keepfold.app.server`: read from there at call time, so the code that
# reads them stays in server.py (a mixin module must not read them as globals)
PATCHED = {"_open", "_obsidian", "running_url", "start_url", "show_window", "show_capture", "PORT",
           "WATCH_S", "IDLE_S", "PIGGYBACK", "PRIVATE_S", "WARM_STATUS", "MAX_CONNECTIONS",
           "KEEPALIVE_S", "start", "finish"}
# entries the HTTP handler serves itself, per table (get, post)
STREAMED = ({"/api/vault/snapshot"}, {"/api/chat", server.UPLOAD})


def _classes():
    import importlib
    out = {}
    for m in MIXINS:
        mod = importlib.import_module(f"keepfold.app.routes.{m}")
        cls = next(c for n, c in vars(mod).items()
                   if inspect.isclass(c) and c.__module__ == mod.__name__)
        out[m] = cls
    return out


def test_the_old_app_surface_is_still_there():
    assert OLD_NAMES <= set(dir(server.App)), sorted(OLD_NAMES - set(dir(server.App)))


def test_every_route_table_entry_is_a_bound_method():
    app = server.App(None)
    for table, streamed in zip((app.get, app.post), STREAMED, strict=True):
        for path, fn in table.items():
            if path in streamed:
                assert fn is None, path
                continue
            assert inspect.ismethod(fn) and fn.__self__ in (app, app.jobs), path


def test_each_method_is_defined_once():
    seen = {}
    for m, cls in _classes().items():
        for name in vars(cls):
            if name.startswith("__"):
                continue
            assert name not in seen, f"{name} is in both {seen[name]} and {m}"
            assert name not in vars(server.App), f"{name} is in {m} and shadowed by server.App"
            seen[name] = m
    assert set(server.App.__bases__) == set(_classes().values())


def test_patched_names_stay_on_the_server_module_and_mixins_never_read_them():
    for n in PATCHED:
        assert hasattr(server, n), n
    for m in MIXINS:
        tree = ast.parse((ROUTES / f"{m}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                assert node.id not in PATCHED, f"{m}.py reads {node.id}: keep it in server.py"


def test_mixins_do_not_import_the_server_module():
    for p in ROUTES.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "app.server" not in text and "import server" not in text, p.name


@pytest.mark.parametrize("name", ["Busy", "NotFound", "IMPORTS", "DRY_IDLE_S", "CLOUD_FIELDS"])
def test_shared_names_are_the_same_objects_on_the_server_module(name):
    from keepfold.app.routes import _common
    assert getattr(server, name) is getattr(_common, name)
