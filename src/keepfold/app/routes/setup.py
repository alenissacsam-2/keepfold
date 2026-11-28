"""Setup: the wizard's steps, Connect, the Local model, and the browsing settings and jobs.

Pure moves out of `App` (server.py): the methods keep their bodies; `App` inherits this
mixin and its get/post tables still bind `self.<name>`."""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

from keepfold import update
from keepfold.app import actions, bundle, status, targets, wizard
from keepfold.app.routes._common import (
    CLOUD_FIELDS,
    _bool,
    _checkout,
    _connect_skipped,
    _disk,
    _domains,
    skill_url,
)


class SetupRoutes:
    def setup(self) -> dict:
        from keepfold import chats, cli, importers
        from keepfold.app import desktop
        if self.warming not in (None, threading.current_thread()):
            self.warming.join()  # the warm-up is asking the same probes: wait, not ask twice
        return {**wizard.state(self.root, self.scanned),
                "connect": [{k: t[k] for k in ("id", "label", "detected", "connected")}
                            for t in targets.describe(self.entry())],
                "first_prompts": list(cli.FIRST_PROMPTS), "connect_skipped": _connect_skipped(),
                "welcome_done": desktop._app_cfg().get("welcome_done") is True,
                "guides": {**chats.GUIDES, **importers.GUIDES},
                "imports": status.imports(self.root if self.entry() else None),
                "watched": self._watched()}

    def _want(self, body: dict) -> dict:
        want = body.get("want")
        if not isinstance(want, dict) or not all(
                isinstance(v, bool) or (k == "lmstudio" and v == "private")
                for k, v in want.items()):
            raise ValueError("want must map app ids to true or false")
        return want

    def connect(self) -> dict:
        """Connect: the AI apps, plus (with a vault) the context packs for web chats and the
        per-agent profiles (plan v9 item 8)."""
        from keepfold import access, packs, profiles
        from keepfold.events import EventLog
        extra = {}
        if self.entry() is not None:
            v = self._vault()
            events = list(EventLog(v).iter_events())  # read once for every pack
            last = packs.copied(events)

            def size(n, s, text):  # R32a: changed only when copied before and now different
                h = packs.digest(text)
                return {"chars": s, "text": text, "hash": h,
                        "changed": (n, s) in last and last[(n, s)] != h}
            extra = {"packs": [{"id": n, "sizes": [size(n, s, t) for s, t in
                                                   packs.sized(v, n, events=events).items()]}
                               for n in packs.PACKS],
                     "profiles": profiles.load(v), "scopes": list(access.SCOPES)}
        return {"targets": targets.describe(self.entry()), "vault": self.entry() is not None,
                "instructions": packs.USE_TEXT,  # Copy instructions on rows with a `paste`
                "skill_zip": str(_checkout() / "dist" / "keepfold-skill.zip"),
                "skill_zip_found": (_checkout() / "dist" / "keepfold-skill.zip").is_file(),
                "skill_url": skill_url(), **extra}

    def pack_copied(self, body: dict) -> dict:
        """R32a: the owner pressed Copy on a context pack; one log event, so Connect can say
        when the pack changed since. Only pack, chars and hash come from the app."""
        from keepfold import packs
        from keepfold.events import EventLog
        if not isinstance(body, dict) or set(body) != {"pack", "chars", "hash"}:
            raise ValueError("send pack, chars and hash only.")
        name, chars, h = body["pack"], body["chars"], body["hash"]
        if not isinstance(name, str) or name not in packs.PACKS:
            raise ValueError(f"pack must be one of {', '.join(packs.PACKS)}.")
        if type(chars) is not int or chars not in packs.SIZES:
            raise ValueError("chars must be 1500 or 5000.")
        if not isinstance(h, str) or not re.fullmatch(r"[0-9a-f]{16}", h):
            raise ValueError("hash must be 16 hex characters.")
        EventLog(self._vault()).append(session="cli", kind="log", source="self", fields={
            "payload": {"kind": "pack_copied", "pack": name, "chars": chars, "hash": h}})
        return {"ok": True}

    def profiles_set(self, body: dict) -> dict:
        """The owner's per-agent profiles, saved from the app only (never from MCP input)."""
        from keepfold import profiles
        return {"profiles": profiles.save(self._vault(), body)}  # body: {profiles, unknown}

    def connect_plan(self, body: dict) -> dict:
        if bundle.translocated():  # the configs would name a temporary path: Move first
            return {"move": True}
        return {"rows": targets.plan_all(self._want(body), self.entry())}

    def connect_apply(self, body: dict) -> dict:
        if bundle.translocated():
            return {"move": True}
        want, base = self._want(body), self.entry()

        def run(job):
            bundle.link_local_bin()  # the Mac app's ~/.local/bin/keepfold; elsewhere nothing
            return {"ok": True, "rows": targets.apply_all(want, base, job.say)}
        return self.jobs.start("Connecting AI apps", "connect", run)

    def connect_undo(self, body: dict) -> dict:
        """Connect's "Undo the last change" for one app: its newest backups come back
        (targets.undo_last). The app sends an id only; it never names a file."""
        if not isinstance(body, dict) or set(body) != {"id"} or not isinstance(body["id"], str):
            raise ValueError("send the app id only.")
        return {"ok": True, "rows": targets.undo_last(body["id"])}

    def app_move(self, body: dict) -> dict:
        """Move Keepfold to Applications: copy the translocated Mac app there and quit; the copy
        opens once this app has ended."""
        bundle.move_to_applications()
        threading.Timer(2, self.stop.set).start()  # the page reads the answer first
        return {"ok": True}

    def app_uninstall(self, body: dict) -> dict:
        """Uninstall Keepfold… (Mac/Linux app only; Windows keeps Apps & Features): the steps of
        `keepfold uninstall --yes`, then the app itself goes (update.remove_app) and it quits.
        The vault and ~/.keepfold are kept."""
        if update.mode() != "app":
            raise ValueError("Uninstall from here is only in the Mac and Linux app.")

        def run(job):
            from keepfold import cli
            failed = cli.uninstall_steps(job.say)
            out = update.remove_app(job.say)
            if out["removed"]:
                threading.Timer(2, self.stop.set).start()  # the page reads the result first
            return {**out, "steps_failed": failed}
        return self.jobs.start("Uninstalling Keepfold", "uninstall", run)

    def connect_path(self, body: dict) -> dict:
        """Status's Claude Code Fix (status.claude_code_row): the installed Keepfold's bin folder
        first on the user PATH, so the `keepfold` Claude Code starts is the app's."""
        from keepfold import clients
        from keepfold.app import winpath
        exe = clients.installed_keepfold() if update.mode() != "app" else None  # a Windows fix
        if exe is None:
            raise ValueError("This copy of Keepfold was not installed with KeepfoldSetup.exe.")
        winpath.put_first(str(exe.parent))
        # this app's own PATH too, so Status and what it starts see the change at once
        os.environ["PATH"] = winpath.first(os.environ.get("PATH", ""), str(exe.parent))
        print("PATH: Keepfold moved first")  # never the PATH itself: it may name private folders
        return {"ok": True, "say": "Done. Quit Claude completely and open it again, so its "
                                   f"Keepfold tools use {update.current()}."}

    def llm(self) -> dict:
        """The Local model page: both runners' chat models (embedding models left out by the
        chat's own name rule) and the model sized to this computer."""
        from keepfold import localllm
        if self.warming not in (None, threading.current_thread()):
            self.warming.join()  # the warm-up is asking the same probes: wait, not ask twice
        d, hw = status.llm(quick=True), wizard.hardware()  # kept like Status's (~3 s fresh)

        def chats(names):
            return [n for n in names if not localllm._NOT_CHAT.search(n)]
        return {**d, "ollama": None if d["ollama"] is None else chats(d["ollama"]),
                "ollama_state": wizard._ollama_state(d["ollama"]),  # the words Setup uses
                "models": [m for m in status.kept(("lms_models", None), status.lms_models)
                           if chats([m["key"]])],
                "hardware": hw, "recommend": wizard.model(wizard.recommend(hw["gb"], hw["kind"]))}

    def llm_settings(self, body: dict) -> dict:
        from keepfold import taint

        def merge(cfg: dict) -> dict:  # under the config lock
            old = cfg.get("localllm")
            new = dict(old) if isinstance(old, dict) else {}
            if "enabled" in body:
                new["enabled"] = _bool(body, "enabled")
            if "model" in body:
                m = body["model"]
                if not isinstance(m, str) or not m:  # never write `model: null`
                    raise ValueError("Choose a model first.")
                if len(m) > 200 or not all(c.isalnum() or c in "._:@/-" for c in m):
                    raise ValueError("not a model name")
                r = body.get("runner", "lmstudio")  # no runner: an LM Studio choice, as before
                if r not in wizard.RUNNERS:
                    raise ValueError("runner must be ollama or lmstudio")
                new["model"], new["runner"] = m, r
            if "background_model" in body:  # "" or null: the main model does background work too
                b = body["background_model"] or ""
                if not isinstance(b, str) or len(b) > 200 or not all(
                        c.isalnum() or c in "._:@/-" for c in b):
                    raise ValueError("not a model name")
                if b:
                    new["background_model"] = b
                else:
                    new.pop("background_model", None)
            return {"localllm": new}
        taint.write_config(merge)
        return {"ok": True}

    def llm_load(self, body: dict) -> dict:
        m = body.get("model")
        if not isinstance(m, str) or not m:
            raise ValueError("choose a model first")
        return self.jobs.start(f"Loading {m}", "llm", lambda job: actions.llm_load(job, m))

    def llm_size(self, body: dict) -> dict:
        """A model's download size, asked before the page offers to download it (one request
        to ollama.com for a name Setup does not list)."""
        n = wizard.ollama_size(body.get("name"))
        return {"name": body["name"], "bytes": n, "size": wizard.size_text(n),
                "source": "ollama.com"}

    def llm_pull(self, body: dict) -> dict:
        from keepfold import localllm
        name = wizard.ollama_name(body.get("name"))
        if not localllm._local_name(status.ollama_models() or [], name) and (
                stop := _disk(body, wizard.models_dir("ollama"), wizard.ollama_size(name))):
            return stop
        return self.jobs.start(f"Downloading {name}", "llm",
                               lambda job: wizard.pull_ollama(job, name), cancellable=True)

    def llm_test(self, body: dict) -> dict:
        # a test may start LM Studio and load the chosen model, as `enabled: true` would
        return self.jobs.start("Testing the local model", "llm",
                               lambda job: actions.llm_test(job, actions.llm_config(enabled=True)))

    def browse(self) -> dict:
        b = status.browse(self.root if self.entry() else None)
        return {**b, "history": status.history(self.root if self.entry() else None, self.scanned),
                "cloud": status.cloud(self.root if self.entry() else None)}

    def browse_settings(self, body: dict) -> dict:
        """Save `browse:`, keeping its other keys and the rest of the config. The page sends
        per-category lists; the legacy adult/sensitive pair is then written to match them."""
        from keepfold import taint
        from keepfold.browse import lists
        for key, ok in (("adult", ("skip", "private")), ("sensitive", ("private", "skip"))):
            if key in body and body[key] not in ok:
                raise ValueError(f"{key} must be {' or '.join(ok)}")
        if "audience" in body:  # one three-way choice per category, as the two lists
            aud = body["audience"]
            if not isinstance(aud, dict) or set(aud) != set(lists.CATEGORIES) or not all(
                    v in ("skip", "local", "cloud") for v in aud.values()):
                raise ValueError("audience must give every category skip, local or cloud")
            body = {**body, "skip_categories": [c for c, v in aud.items() if v == "skip"],
                    "hidden_from_claude": [c for c, v in aud.items() if v == "local"]}

        def merge(cfg: dict) -> dict:  # under the config lock
            old = cfg.get("browse")
            new = {**(old if isinstance(old, dict) else {}),
                   **{k: body[k] for k in ("adult", "sensitive") if k in body},
                   "visible_to_claude": _bool(body, "visible_to_claude"),
                   "deny_domains": _domains(body.get("deny", []), "deny"),
                   "allow_domains": _domains(body.get("allow", []), "allow"),
                   "private_domains": _domains(body.get("private", []), "private")}
            for key in ("hidden_from_claude", "skip_categories"):
                if key in body:
                    v = body[key]
                    if not isinstance(v, list) or not all(c in lists.CATEGORIES for c in v):
                        raise ValueError(f"{key} must list categories from "
                                         f"{', '.join(lists.CATEGORIES)}")
                    new[key] = [c for c in lists.CATEGORIES if c in v]
            if "skip_categories" in body:
                skip = new["skip_categories"]
                new["adult"] = "skip" if "adult" in skip else "private"
                new["sensitive"] = "skip" if all(c in skip for c in lists.CATEGORIES
                                                 if c != "adult") else "private"
            if "private_mcp" in body:
                new["private_mcp"] = _bool(body, "private_mcp")
            if "hidden_from_claude" in body:  # C9: stored as the cloud allowlist; new stay local
                hidden = new.pop("hidden_from_claude")
                new["cloud_categories"] = [c for c in lists.CATEGORIES if c not in hidden]
            return {"browse": new}
        taint.write_config(merge)
        return {"ok": True}

    def cloud(self, body: dict) -> dict:
        """Save the cloud model: `browse.cloud_processing` and `cloudllm` (other keys kept).
        Never a key: only the name of the variable that holds it. Empty fields are removed."""
        from keepfold import cloudllm, taint
        from keepfold.vault import secret_pattern
        if extra := set(body) - set(CLOUD_FIELDS):
            raise ValueError(f"unknown field {sorted(extra)[0]!r}: the key itself is never "
                             "sent here, only the name of the variable that holds it")
        on = _bool(body, "cloud_processing")
        got = {k: body.get(k) or None for k in CLOUD_FIELDS[1:]}
        if not all(v is None or (isinstance(v, str) and len(v) <= 300) for v in got.values()):
            raise ValueError("provider, model, base_url and api_key_env must be text")
        if got["provider"] is not None and got["provider"] not in (*cloudllm.PRESETS, "custom"):
            raise ValueError(f"provider must be one of {', '.join(cloudllm.PRESETS)} or custom")
        m = got["model"]
        if m is not None and not all(c.isalnum() or c in "._:@/-" for c in m):
            raise ValueError("not a model name")
        if got["provider"] != "custom":
            got["base_url"] = None  # a preset's base is its own
        elif got["base_url"] is not None:
            cloudllm._check(got["base_url"])  # https with a host, or ValueError
        env = got["api_key_env"]
        if env is not None:
            cloudllm.check_env_name(env)
            if secret_pattern(env):
                raise ValueError("api_key_env is the NAME of an environment variable, such as "
                                 "OPENAI_API_KEY; never paste the key here")
        need = {"provider": "a provider", "model": "a model", "api_key_env": "the variable name"}
        if got["provider"] == "custom":
            need["base_url"] = "the Base URL"
        if on and (gap := [w for k, w in need.items() if got[k] is None]):
            raise ValueError(f"To turn the cloud model on, fill in {' and '.join(gap)} first.")

        def merge(old: dict) -> dict:  # under the config lock
            c = old.get("cloudllm") if isinstance(old.get("cloudllm"), dict) else {}
            c = {k: v for k, v in c.items() if k not in CLOUD_FIELDS}
            b = old.get("browse") if isinstance(old.get("browse"), dict) else {}
            return {"cloudllm": {**c, **{k: v for k, v in got.items() if v is not None}},
                    "browse": {**b, "cloud_processing": on}}
        taint.write_config(merge)
        return {"ok": True}

    def auto_settings(self, body: dict) -> dict:
        """Save `browse.auto_import_hours` (0-168; 0 = off), the other browse keys kept."""
        from keepfold import taint
        h = body.get("hours")
        if not (isinstance(h, (int, float)) and not isinstance(h, bool) and 0 <= h <= 168):
            raise ValueError("hours must be a number from 0 (off) to 168")
        taint.write_config(lambda cfg: {"browse": {
            **(cfg["browse"] if isinstance(cfg.get("browse"), dict) else {}),
            "auto_import_hours": int(h) if h == int(h) else h}})
        return {"ok": True}

    def login(self, body: dict) -> dict:
        on, root = _bool(body, "on"), self.need_root()
        return self.jobs.start("Start at login" if on else "Removing start at login", "browse",
                               lambda job: actions.login(job, root, on))

    def process(self, body: dict) -> dict:
        root = self.need_root()
        return self.jobs.start("Processing browsing", "browse",
                               lambda job: actions.browse_process(job, root), quiet_errors=True)

    def selftest(self, body: dict) -> dict:
        return self.jobs.start("Browse self-test", "browse", actions.browse_selftest,
                               quiet_errors=True)

    def _section(self, body: dict) -> str:
        return "setup" if body.get("from") == "setup" else "browse"

    def scan(self, body: dict) -> dict:
        def run(job):
            out = actions.browse_scan(job)
            self.scanned = {r["source"]: r for r in out["rows"]}
            return out
        return self.jobs.start("Counting browser history", self._section(body), run,
                               quiet_errors=True)

    def history_import(self, body: dict) -> dict:
        root, keys = self.need_root(), body.get("sources")
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
            raise ValueError("Tick at least one browser.")
        if "skip" in body:  # only the tick list sends it; Status's button must not clear it
            skip = body["skip"]
            if not isinstance(skip, list) or not all(isinstance(k, str) for k in skip):
                raise ValueError("skip must list profile keys")
            self._skip_profiles(skip, keys)
        return self.jobs.start("Importing past history", self._section(body),
                               lambda job: actions.history_import(job, root, set(keys)),
                               cancellable=True, quiet_errors=True)

    @staticmethod
    def _skip_profiles(skip: list, ticked: list) -> None:
        """Keep `browse.skip_profiles` as the user's last ticks: the unticked keys added, the
        ticked ones removed; the automatic imports (`sources.chosen`) leave those out."""
        from keepfold import taint

        def merge(cfg: dict) -> dict:  # under the config lock; unchanged: no write
            old = cfg.get("browse")
            old = old if isinstance(old, dict) else {}
            had = old.get("skip_profiles")
            had = [k for k in had if isinstance(k, str)] if isinstance(had, list) else []
            new = sorted((set(had) | set(skip)) - set(ticked))
            return {"browse": {**old, "skip_profiles": new}} if new != sorted(had) else {}
        taint.write_config(merge)

    def setup_skip(self, body: dict) -> dict:
        """Skip the Connect step (Status becomes the landing page), or un-skip it."""
        from keepfold import taint
        skip = _bool(body, "skipped")

        def merge(cfg: dict) -> dict:  # under the config lock
            app = cfg.get("app")
            app = {k: v for k, v in app.items() if k != "connect_skipped"} \
                if isinstance(app, dict) else {}
            return {"app": {**app, "connect_skipped": True} if skip else app}
        taint.write_config(merge)
        return {"ok": True}

    def setup_vault(self, body: dict) -> dict:
        path = body.get("path")
        wizard.vault_path(path)  # a refused folder: one plain message (400), no failed job

        def run(job):
            old = self.root
            root, out = wizard.make_vault(job, path)
            self.root = root
            if old is None or Path(old).resolve() == root:
                return out
            # FX1 6: the vault moved. Start at login is Keepfold's own: it follows at once, but
            # only the entry this Keepfold made for the old folder (never another install's).
            # AI apps' configs are the owner's: Connect re-points them after a confirm.
            if status.login_is_for(Path(old)):
                try:
                    actions.login(job, root, True)
                    out["lines"].append("Start at login now uses the new folder.")
                except Exception as e:  # noqa: BLE001  (the vault itself is set up)
                    out["lines"].append(f"Start at login still uses the old folder: {e}")
            if any(t.get("stale") for t in targets.describe(self.entry())):
                out["reconnect"] = True
            return out
        return self.jobs.start("Setting up the vault", "setup", run)

    def setup_embed(self, body: dict) -> dict:
        root = self.entry() and self.root

        def run(job):  # a successful download goes straight on to indexing the notes
            out = wizard.embed(job, root)
            if not out["ok"] or not root or job.cancelled.is_set():
                return out
            job.say("downloaded; now indexing your notes")
            idx = wizard.index(job, root)
            return {**idx, "lines": [*out["lines"][-5:], *idx["lines"]]}
        return self.jobs.start("Downloading the search model", "setup", run, cancellable=True)

    def setup_voice(self, body: dict) -> dict:
        return self.jobs.start("Installing the speech model", "setup", wizard.voice,
                               cancellable=True)

    def setup_index(self, body: dict) -> dict:
        root = self.need_root()
        return self.jobs.start("Indexing your notes", "setup",
                               lambda job: wizard.index(job, root), cancellable=True)

    def setup_install(self, body: dict) -> dict:
        if stop := _disk(body, wizard.install_dir(), wizard.OLLAMA_BYTES):
            return stop
        return self.jobs.start("Installing Ollama", "setup", wizard.install_ollama,
                               cancellable=True)

    def setup_git(self, body: dict) -> dict:
        """winget's Git for this Windows account; scope "machine" only when the owner chose
        it after that failed. `from` "status": the result shows on Status."""
        scope = "machine" if body.get("scope") == "machine" else "user"
        if stop := _disk(body, wizard.install_dir(), wizard.GIT_BYTES):
            return stop
        root = self.root
        return self.jobs.start("Installing Git", "status" if body.get("from") == "status"
                               else "setup", lambda job: wizard.install_git(job, scope, root),
                               cancellable=True)

    def _model_runner(self, body: dict) -> tuple[str, str]:
        m, r = body.get("model"), body.get("runner")
        wizard.model(m if isinstance(m, str) else "")
        if r not in ("ollama", "lmstudio"):
            raise ValueError("runner must be ollama or lmstudio")
        return m, r

    @staticmethod
    def _model_disk(body: dict, m: str, r: str) -> dict | None:
        """The free-space refusal for a Setup model this runner does not hold yet."""
        have = wizard._have(wizard.model(m), status.ollama_models(),
                            status.lms_models() if r == "lmstudio" else [])[r]
        return None if have else _disk(body, wizard.models_dir(r), wizard.model(m)["bytes"])

    def setup_pull(self, body: dict) -> dict:
        m, r = self._model_runner(body)
        if stop := self._model_disk(body, m, r):
            return stop
        return self.jobs.start(f"Downloading {wizard.model(m)['label']}", "setup",
                               lambda job: wizard.pull(job, m, r), cancellable=True)

    def setup_test(self, body: dict) -> dict:
        m, r = self._model_runner(body)
        return self.jobs.start(f"Testing {wizard.model(m)['label']}", "setup",
                               lambda job: wizard.test_and_save(job, m, r))

    def setup_start(self, body: dict) -> dict:
        r = body.get("runner")
        if r not in ("ollama", "lmstudio"):
            raise ValueError("runner must be ollama or lmstudio")
        name = "Ollama" if r == "ollama" else "LM Studio"
        return self.jobs.start(f"Starting {name}", "setup", lambda job: wizard.start(r))

    def setup_model(self, body: dict) -> dict:
        """One click: download the model, then test it and turn it on."""
        m, r = self._model_runner(body)
        if stop := self._model_disk(body, m, r):
            return stop

        def run(job):
            out = wizard.pull(job, m, r)
            return wizard.test_and_save(job, m, r) if out["ok"] else out
        return self.jobs.start(f"Setting up {wizard.model(m)['label']}", "setup", run,
                               cancellable=True)
