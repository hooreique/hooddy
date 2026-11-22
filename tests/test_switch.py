"""Transactional activation tests with isolated Nix/systemd and real Caddy validation."""

from contextlib import contextmanager
import http.server
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import threading
import time
import unittest

SCRIPT, EXECUTABLE, CADDY, FAKE_SYSTEM = sys.argv[1:]
sys.argv = sys.argv[:1]
HOST_SYSTEM = {"x86_64": "x86_64-linux", "aarch64": "aarch64-linux"}[platform.machine()]


class SwitchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hooddy-switch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config"
        self.directory = self.root / ".config/hooddy"
        self.unit_dir = self.config / "systemd/user"
        self.runtime = self.root / "run"
        self.runtime.mkdir(mode=0o700)
        self.state_file = self.root / "system.json"
        self.state_file.write_text(json.dumps({
            "linger": "no", "units": {}, "calls": [], "builds": [], "generation": "",
        }))
        binaries = self.root / "bin"
        binaries.mkdir()
        for name in ["systemctl", "loginctl", "nix", "nix-store", "systemd-analyze"]:
            destination = binaries / name
            destination.write_text(f"#!{sys.executable}\n" + Path(FAKE_SYSTEM).read_text().split("\n", 1)[1])
            destination.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.root), XDG_CONFIG_HOME=str(self.config),
                        XDG_DATA_HOME=str(self.root / "data"), XDG_RUNTIME_DIR=str(self.runtime),
                        HOODDY_TEST_STATE=str(self.state_file), PATH=f"{binaries}:{os.environ['PATH']}")
        self.env.pop("NOTIFY_SOCKET", None)
        self.generation_number = 0
        self.directory.mkdir(parents=True)
        self.lock_text = '{"nodes":{},"root":"root","version":7}\n'

    def state(self):
        return json.loads(self.state_file.read_text())

    def change_state(self, **changes):
        self.state_file.write_text(json.dumps(self.state() | changes))

    def unit(self, name):
        return self.state()["units"][f"hooddy-{name}.service"]

    def cli(self, *args, code=0):
        result = subprocess.run(["bash", SCRIPT, *args], env=self.env,
                                capture_output=True, text=True, timeout=30)
        if code is not None:
            self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def configure(self):
        (self.directory / "flake.nix").write_text("{}\n")
        (self.directory / "flake.lock").write_text(self.lock_text)
        (self.directory / "untracked.nix").write_text("untracked configuration\n")

    def git(self, *args, directory=None):
        subprocess.run(["git", *args], cwd=directory or self.directory,
                       env=self.env, capture_output=True, check=True)

    def generation(self, applications=None, sites="", **manifest_changes):
        self.generation_number += 1
        generation = self.root / f"generation-{self.generation_number}"
        (generation / "units").mkdir(parents=True)
        enabled = manifest_changes.get("enabled", True)
        manifest = {"protocolVersion": 2, "system": HOST_SYSTEM, "enabled": enabled,
                    "caddy": CADDY if enabled else None, "services": {}}
        for name, specification in (applications or {}).items():
            if isinstance(specification, str):
                specification = {"description": specification}
            filename = f"units/hooddy-{name}.service"
            (generation / filename).write_text(
                "[Unit]\nDescription=" + specification.get("description", name) + "\n\n"
                "[Service]\nExecStart=" + CADDY + " version\n\n"
                "[Install]\nWantedBy=default.target\n")
            manifest["services"][name] = {"unit": filename,
                                             "healthCheck": specification.get("healthCheck")}
        manifest.update(manifest_changes)
        (generation / "manifest.json").write_text(json.dumps(manifest))
        if enabled:
            config = generation / "Caddyfile"
            config.write_text("{\n admin unix/{$XDG_RUNTIME_DIR}/hooddy/admin.sock\n}\n" + sites)
            (generation / "units/hooddy.service").write_text(
                "[Unit]\nDescription=Hooddy shared Caddy server\n\n"
                "[Service]\nType=notify\nRuntimeDirectory=hooddy\n"
                f"ExecStart={CADDY} run --config {config} --adapter caddyfile\n"
                f"ExecReload={CADDY} reload --config {config} --adapter caddyfile\n\n"
                "[Install]\nWantedBy=default.target\n")
        self.change_state(generation=str(generation))
        return generation

    def managed_files(self):
        return {path.name: path.read_bytes() for path in self.unit_dir.glob("hooddy*.service")}

    def assert_no_journal(self):
        self.assertFalse((self.directory / ".switch/journal.json").exists())

    def rooted(self, generation):
        return any(path.is_symlink() and path.resolve() == generation
                   for path in (self.directory / ".switch").rglob("*"))

    def test_requires_the_flake_under_home(self):
        misplaced = self.config / "hooddy"
        misplaced.mkdir(parents=True)
        (misplaced / "flake.nix").write_text("{}")
        (misplaced / "flake.lock").write_text(self.lock_text)
        self.cli("switch", code=1)
        self.assertFalse(self.state()["builds"])
        self.assertEqual(self.managed_files(), {})

    def test_first_switch_enables_linger_and_starts_all_units(self):
        self.configure()
        generation = self.generation({"app": "App"}, "http://localhost:18080 {\n respond test\n}\n")
        self.cli("switch")
        self.assertEqual(self.state()["linger"], "yes")
        for name in ("hooddy.service", "hooddy-app.service"):
            state = self.state()["units"][name]
            self.assertTrue(state["active"])
            self.assertTrue(state["enabled"])
            self.assertEqual((self.unit_dir / name).read_bytes(), (generation / "units" / name).read_bytes())
        self.assertTrue(self.rooted(generation))
        self.assert_no_journal()

    def test_site_update_reloads_caddy_from_the_new_generation(self):
        self.configure()
        manual = self.directory / "Caddyfile"
        manual.write_text("invalid handwritten configuration\n")
        self.generation(sites="http://localhost:18080 {\n respond first\n}\n")
        self.cli("switch")
        new = self.generation(sites="http://localhost:18080 {\n respond second\n}\n")
        self.cli("switch")
        unit = self.state()["units"]["hooddy.service"]
        self.assertEqual(unit["starts"], 1)
        self.assertEqual(unit["stops"], 0)
        self.assertEqual(unit["reloads"], 1)
        self.assertEqual((self.unit_dir / "hooddy.service").read_bytes(),
                         (new / "units/hooddy.service").read_bytes())
        self.assertEqual(manual.read_text(), "invalid handwritten configuration\n")

    def test_linger_permission_failure_leaves_no_running_units(self):
        self.configure()
        self.generation({"app": "App"})
        self.change_state(fail_once=["loginctl:enable-linger"])
        self.cli("switch", code=1)
        self.assertEqual(self.state()["linger"], "no")
        self.assertEqual(self.managed_files(), {})
        self.assert_no_journal()

    def test_snapshot_includes_local_files_and_preserves_lock(self):
        self.configure()
        self.env["GIT_DISCOVERY_ACROSS_FILESYSTEM"] = "1"
        generation = self.generation()
        self.cli("switch")
        builds = self.state()["builds"]
        self.assertEqual(len(builds), 1)
        self.assertNotEqual(builds[0]["source"], str(self.directory))
        self.assertEqual(builds[0]["lock"], self.lock_text)
        self.assertEqual(bytes.fromhex(builds[0]["files"]["untracked.nix"]["contents"]),
                         b"untracked configuration\n")
        self.assertEqual((self.directory / "flake.lock").read_text(), self.lock_text)
        self.assertTrue(self.rooted(generation))

    def test_git_snapshot_uses_worktree_contents_and_preserves_file_types(self):
        self.configure()
        self.git("init", "--object-format=sha1")
        unusual = os.fsdecode(b"with space\nline-\xff")
        script = self.directory / unusual
        script.write_bytes(b"staged\n")
        script.chmod(0o755)
        (self.directory / "link").symlink_to(unusual)
        (self.directory / "removed").touch()
        nested = self.directory / "nested"
        nested.mkdir()
        (nested / "tracked").write_text("tracked\n")
        (self.directory / ".lock").touch()
        (self.directory / ".switch").mkdir()
        (self.directory / ".switch/source").touch()
        self.git("add", "--", "flake.nix", unusual, "link", "removed", "nested", ".lock", ".switch")
        self.git("update-index", "--add", "--cacheinfo", "160000," + "1" * 40 + ",submodule")
        (self.directory / "submodule").mkdir()
        (self.directory / "submodule/file").touch()
        script.write_bytes(b"unstaged\n")
        (self.directory / "removed").unlink()
        (nested / "tracked").rename(self.root / "tracked")
        nested.rmdir()
        nested.symlink_to(self.root)
        for name in ("result", "result-extra"):
            (self.directory / name).symlink_to(self.root)

        self.generation()
        self.cli("switch")
        build, = self.state()["builds"]
        self.assertEqual(set(build["files"]), {"flake.nix", "flake.lock", unusual, "link"})
        self.assertEqual(build["files"][unusual], {"contents": b"unstaged\n".hex(), "mode": 0o755})
        self.assertEqual(build["files"]["link"], {"target": unusual})
        self.assertEqual(build["lock"], self.lock_text)
        self.assertFalse(any(call[:3] == ["nix", "flake", "lock"] for call in self.state()["calls"]))

    def test_snapshot_is_scoped_to_config_in_linked_worktree(self):
        repository = self.root / "repository"
        repository.mkdir()
        self.git("init", directory=repository)
        (repository / ".config/hooddy").mkdir(parents=True)
        (repository / ".config/hooddy/flake.nix").write_text("{}\n")
        (repository / "outside").touch()
        self.git("add", ".", directory=repository)
        self.git("-c", "user.name=Hooddy Test", "-c", "user.email=test@example.com",
                 "commit", "-m", "Initial configuration", directory=repository)
        self.directory.rmdir()
        self.directory.parent.rmdir()
        worktree = self.root / "worktree"
        self.git("worktree", "add", "--detach", str(worktree), directory=repository)
        self.env["HOME"] = str(worktree)
        self.directory = worktree / ".config/hooddy"
        self.configure()
        self.generation()
        self.cli("switch")
        self.assertEqual(set(self.state()["builds"][0]["files"]), {"flake.nix", "flake.lock"})

    def test_failed_build_hints_at_untracked_nix_files(self):
        self.configure()
        self.git("init")
        self.git("add", "flake.nix")
        self.generation()
        hint = "Hint: Check git status and stage any needed files."
        self.assertNotIn(hint, self.cli("switch").stderr)

        self.change_state(fail_once=["nix:build"])
        self.assertIn(hint, self.cli("switch", code=1).stderr)

        (self.directory / ".gitignore").write_text("untracked.nix\n")
        self.change_state(fail_once=["nix:build"])
        self.assertNotIn(hint, self.cli("switch", code=1).stderr)

    def test_untracked_git_flake_fails_before_nix(self):
        self.configure()
        self.git("init")
        result = self.cli("switch", code=1)
        self.assertIn("git add flake.nix", result.stderr)
        self.assertFalse(any(call[0] == "nix" for call in self.state()["calls"]))

    def test_git_lookup_failure_fails_before_nix(self):
        self.configure()
        self.git("init")
        (self.directory / ".git/index").write_bytes(b"invalid index")
        result = self.cli("switch", code=1)
        self.assertIn("git ls-files:", result.stderr)
        self.assertFalse(any(call[0] == "nix" for call in self.state()["calls"]))

    def test_first_git_switch_creates_a_lock_and_subsequent_switch_reuses_it(self):
        self.configure()
        self.git("init")
        self.git("add", "flake.nix")
        lock = self.directory / "flake.lock"
        lock.unlink()
        self.generation()
        self.cli("switch")
        first_lock = lock.read_text()
        self.assertEqual(json.loads(first_lock)["version"], 7)
        self.cli("switch")
        self.assertEqual(lock.read_text(), first_lock)
        state = self.state()
        self.assertEqual([build["lock"] for build in state["builds"]], [first_lock, first_lock])
        self.assertEqual(sum(call[:3] == ["nix", "flake", "lock"] for call in state["calls"]), 1)

    def test_first_lock_failure_does_not_activate(self):
        self.configure()
        lock = self.directory / "flake.lock"
        lock.unlink()
        self.generation()
        self.change_state(fail_once=["nix:flake"])
        self.cli("switch", code=1)
        self.assertFalse(lock.exists())
        self.assertFalse(self.state()["builds"])
        self.assertEqual(self.managed_files(), {})
        self.assertEqual(self.state()["linger"], "no")

    def test_rejects_protocol_platform_paths_and_health_urls_before_mutation(self):
        self.configure()
        definitions = [
            {"protocolVersion": 1}, {"system": "unsupported-linux"},
            {"caddy": "/tmp/caddy"}, {"enabled": "yes"},
            {"services": {"../bad": {"unit": "units/hooddy-bad.service", "healthCheck": None}}},
            {"services": {"app": {"unit": "../../etc/passwd", "healthCheck": None}}},
            {"services": {"app": {"unit": "units/hooddy-app.service", "healthCheck": {
                "url": "https://example.com/", "timeoutSeconds": 1}}}},
            {"services": {"app": {"unit": "units/hooddy-app.service", "healthCheck": {
                "url": "http://127.0.0.1/", "timeoutSeconds": 0}}}},
        ]
        for changes in definitions:
            with self.subTest(changes=changes):
                self.generation({"app": "App"}, **changes)
                self.cli("switch", code=1)
                self.assertEqual(self.managed_files(), {})
                self.assertFalse(any(unit["active"] for unit in self.state()["units"].values()))

    def test_refuses_to_adopt_an_existing_legacy_or_application_unit(self):
        self.configure()
        self.generation({"app": "App"})
        self.unit_dir.mkdir(parents=True)
        for name in ("hooddy.service", "hooddy-app.service"):
            with self.subTest(name=name):
                unit = self.unit_dir / name
                unit.write_text("# Managed by hooddy.\noperator-owned\n")
                self.cli("switch", code=1)
                self.assertIn("operator-owned", unit.read_text())
                unit.unlink()

    def test_refuses_symlinked_unit_directory(self):
        self.configure()
        self.generation({"app": "App"})
        external = self.root / "external"
        external.mkdir()
        self.unit_dir.parent.mkdir(parents=True)
        self.unit_dir.symlink_to(external, target_is_directory=True)
        self.cli("switch", code=1)
        self.assertEqual(list(external.iterdir()), [])

    def test_rejects_unit_conflicts_and_dropins(self):
        self.configure()
        self.generation({"app": "App"})
        self.change_state(fragments={"hooddy-app.service": "/etc/systemd/user/hooddy-app.service"})
        self.cli("switch", code=1)
        self.assertEqual(self.managed_files(), {})
        self.change_state(fragments={}, dropins={"hooddy-app.service": "/etc/systemd/user/hooddy-app.service.d/custom.conf"})
        self.cli("switch", code=1)
        self.assertEqual(self.managed_files(), {})

    def test_ownership_hash_detects_manual_edits_and_missing_files_reconcile(self):
        self.configure()
        self.generation({"app": "App"})
        self.cli("switch")
        path = self.unit_dir / "hooddy-app.service"
        original = path.read_bytes()
        path.write_bytes(original + b"# local edit\n")
        self.cli("switch", code=1)
        self.assertTrue(path.read_bytes().endswith(b"# local edit\n"))
        path.unlink()
        self.cli("switch")
        self.assertEqual(path.read_bytes(), original)
        caddy_unit = self.unit_dir / "hooddy.service"
        caddy_unit.write_text("# local edit\n")
        self.cli("switch", code=1)
        self.assertEqual(caddy_unit.read_text(), "# local edit\n")

    def test_managed_file_changed_during_validation_is_preserved(self):
        self.configure()
        self.generation({"app": "Before"})
        self.cli("switch")
        self.generation({"app": "After"})
        path = self.unit_dir / "hooddy-app.service"
        changed = path.read_text() + "# concurrently edited\n"
        self.change_state(mutate_once=[{"selector": "systemd-analyze:verify",
                                       "path": str(path), "content": changed}])
        self.cli("switch", code=1)
        self.assertEqual(path.read_text(), changed)
        self.assertEqual(self.unit("app")["starts"], 1)
        self.assert_no_journal()

    def test_lock_changed_during_evaluation_is_not_activated_or_overwritten(self):
        self.configure()
        self.generation({"app": "App"})
        lock = self.directory / "flake.lock"
        changed = self.lock_text + "\n"
        self.change_state(mutate_once=[{"selector": "nix:build", "path": str(lock),
                                       "content": changed}])
        self.cli("switch", code=1)
        self.assertEqual(lock.read_text(), changed)
        self.assertEqual(self.managed_files(), {})

    def test_add_change_remove_and_reconcile_without_restarting_healthy_units(self):
        self.configure()
        first = self.generation({"keep": "Keep", "change": "Before", "remove": "Remove"})
        self.cli("switch")
        second = self.generation({"keep": "Keep", "change": "After", "added": "Added"})
        self.cli("switch")
        self.assertEqual(self.unit("keep")["starts"], 1)
        self.assertEqual(self.unit("keep")["stops"], 0)
        self.assertEqual(self.unit("change")["starts"], 2)
        self.assertEqual(self.unit("change")["stops"], 1)
        self.assertFalse(self.unit("remove")["active"])
        self.assertFalse(self.unit("remove")["enabled"])
        self.assertFalse((self.unit_dir / "hooddy-remove.service").exists())
        self.assertTrue(self.unit("added")["active"])
        self.assertTrue(self.rooted(first))
        self.assertTrue(self.rooted(second))
        state = self.state()
        state["units"]["hooddy-keep.service"]["active"] = False
        state["units"]["hooddy-keep.service"]["enabled"] = False
        self.state_file.write_text(json.dumps(state))
        self.cli("switch")
        self.assertEqual(self.unit("keep")["starts"], 2)
        self.assertTrue(self.unit("keep")["enabled"])
        self.assertEqual(self.unit("change")["starts"], 2)
        self.assertTrue(self.rooted(first))
        self.assertTrue(self.rooted(second))

    def test_repeated_switch_restores_caddy_enablement_and_activity(self):
        self.configure()
        self.generation()
        self.cli("switch")
        state = self.state()
        state["units"]["hooddy.service"].update(active=False, enabled=False)
        self.state_file.write_text(json.dumps(state))
        self.cli("switch")
        self.assertTrue(self.state()["units"]["hooddy.service"]["enabled"])
        self.assertTrue(self.state()["units"]["hooddy.service"]["active"])

    def test_changed_systemd_unit_directory_cannot_redirect_teardown(self):
        self.configure()
        old = self.generation({"app": "App"})
        self.cli("switch")
        units = self.managed_files()
        other = self.root / "other"
        self.env["XDG_CONFIG_HOME"] = str(other)
        self.generation(enabled=False)
        self.cli("switch", code=1)
        self.assertEqual(self.managed_files(), units)
        self.assertTrue(self.rooted(old))
        self.assertTrue(self.unit("app")["active"])
        self.assertFalse((other / "systemd/user").exists())
        self.env["XDG_CONFIG_HOME"] = str(self.config)
        self.cli("switch")
        self.assertEqual(self.managed_files(), {})

    def test_validates_caddyfile_from_the_generation(self):
        self.configure()
        self.generation({"app": "App"}, "http://app.test:18080 {\n unknown_directive\n}\n")
        self.cli("switch", code=1)
        self.assertEqual(self.managed_files(), {})

    def test_unit_validation_failure_precedes_mutation(self):
        self.configure()
        self.generation({"app": "App"})
        self.change_state(fail_once=["systemd-analyze:verify"])
        self.cli("switch", code=1)
        self.assertEqual(self.managed_files(), {})

    def test_unit_contract_rejects_nonstore_executables_and_extra_install_links(self):
        self.configure()
        for change in [lambda text: text.replace(CADDY, "/bin/sh"),
                       lambda text: text.replace("WantedBy=default.target", "WantedBy=default.target\nAlias=other.service")]:
            with self.subTest(change=change):
                generation = self.generation({"app": "App"})
                unit = generation / "units/hooddy-app.service"
                unit.write_text(change(unit.read_text()))
                self.cli("switch", code=1)
                self.assertEqual(self.managed_files(), {})

    @contextmanager
    def health_server(self, status=204, redirect=None):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                if redirect:
                    self.send_header("Location", redirect)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/health"
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_health_success_and_failure_restore_previous_files_and_states(self):
        self.configure()
        with self.health_server() as url:
            first = self.generation({"app": {"description": "Before", "healthCheck": {
                "url": url, "timeoutSeconds": 1}}})
            self.cli("switch")
        previous = self.managed_files()
        with self.health_server(status=503) as url:
            self.generation({"app": {"description": "After", "healthCheck": {
                "url": url, "timeoutSeconds": 1}}, "new": "New"})
            result = self.cli("switch", code=1)
        self.assertIn("health", result.stderr.lower())
        self.assertEqual(self.managed_files(), previous)
        self.assertTrue(self.unit("app")["active"])
        self.assertTrue(self.unit("app")["enabled"])
        self.assertFalse(self.unit("new")["active"])
        self.assertFalse(self.unit("new")["enabled"])
        self.assertTrue(self.rooted(first))
        self.assert_no_journal()

    def test_rollback_restores_inactive_disabled_service_state(self):
        self.configure()
        self.generation({"app": "Before"})
        self.cli("switch")
        state = self.state()
        state["units"]["hooddy-app.service"].update(active=False, enabled=False)
        self.state_file.write_text(json.dumps(state))
        previous = self.managed_files()
        with self.health_server(status=503) as url:
            self.generation({"app": {"description": "After", "healthCheck": {
                "url": url, "timeoutSeconds": 1}}})
            self.cli("switch", code=1)
        self.assertFalse(self.unit("app")["active"])
        self.assertFalse(self.unit("app")["enabled"])
        self.assertEqual(self.managed_files(), previous)
        self.assert_no_journal()

    def test_rollback_restores_runtime_only_enablement(self):
        self.configure()
        self.generation({"app": "Before"})
        self.cli("switch")
        state = self.state()
        state["units"]["hooddy-app.service"].update(enabled=True, enabled_kind="enabled-runtime")
        self.state_file.write_text(json.dumps(state))
        with self.health_server(status=503) as url:
            self.generation({"app": {"description": "After", "healthCheck": {
                "url": url, "timeoutSeconds": 1}}})
            self.cli("switch", code=1)
        self.assertTrue(self.unit("app")["enabled"])
        self.assertEqual(self.unit("app")["enabled_kind"], "enabled-runtime")
        self.assertTrue(self.unit("app")["active"])
        self.assert_no_journal()

    def test_health_does_not_follow_redirects(self):
        self.configure()
        with self.health_server() as good:
            with self.health_server(status=302, redirect=good) as url:
                self.generation({"app": {"healthCheck": {"url": url, "timeoutSeconds": 1}}})
                self.cli("switch", code=1)
        self.assertEqual(self.managed_files(), {})
        self.assertFalse(self.unit("app")["active"])

    def test_first_activation_failure_restores_absence_and_stopped_caddy(self):
        self.configure()
        self.generation({"app": "App"}, "http://app.test:18080 {\n respond app\n}\n")
        self.change_state(fail_once=["systemctl:reload-or-restart:hooddy.service"])
        self.cli("switch", code=1)
        self.assertEqual(self.managed_files(), {})
        self.assertFalse(self.unit("app")["active"])
        self.assertFalse(self.unit("app")["enabled"])
        self.assertFalse(self.state()["units"]["hooddy.service"]["active"])
        self.assert_no_journal()

    def test_caddy_reload_failure_restores_previous_generation(self):
        self.configure()
        self.generation({"app": "Before"})
        self.cli("switch")
        previous = self.managed_files()
        self.generation({"app": "After"})
        self.change_state(fail_once=["systemctl:reload-or-restart:hooddy.service"])
        self.cli("switch", code=1)
        self.assertEqual(self.managed_files(), previous)
        self.assertTrue(self.unit("app")["active"])
        self.assert_no_journal()

    def test_rollback_failure_keeps_roots_and_recovers_before_next_switch(self):
        self.configure()
        first = self.generation({"app": "Before"})
        self.cli("switch")
        second = self.generation({"app": "After"})
        self.change_state(fail=["systemctl:reload-or-restart:hooddy.service"])
        result = self.cli("switch", code=1)
        self.assertIn("rollback", result.stderr.lower())
        self.assertTrue((self.directory / ".switch/journal.json").exists())
        self.assertTrue(self.rooted(first))
        self.assertTrue(self.rooted(second))
        self.change_state(fail=[])
        self.cli("switch")
        self.assert_no_journal()
        self.assertIn("After", (self.unit_dir / "hooddy-app.service").read_text())

    def test_failure_before_new_units_exist_can_roll_back(self):
        self.configure()
        self.generation({"existing": "Before"})
        self.cli("switch")
        previous = self.managed_files()
        self.generation({"existing": "After", "new": "New"})
        self.change_state(fail_once=["systemctl:stop:hooddy-existing.service"])
        self.cli("switch", code=1)
        self.assertEqual(self.managed_files(), previous)
        self.assertTrue(self.unit("existing")["active"])
        self.assertFalse((self.unit_dir / "hooddy-new.service").exists())
        self.assert_no_journal()

    def test_interrupted_activation_recovers_before_accepting_next_switch(self):
        self.configure()
        self.generation({"app": "Before"})
        self.cli("switch")
        previous = self.managed_files()
        self.generation({"app": "After", "added": "Added"})
        self.change_state(interrupt_once=["systemctl:daemon-reload"])
        result = self.cli("switch", code=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.directory / ".switch/journal.json").exists())
        # Recovery must complete even if the new evaluation subsequently fails.
        self.change_state(fail_once=["nix:build"])
        self.cli("switch", code=1)
        self.assertEqual(self.managed_files(), previous)
        self.assertTrue(self.unit("app")["active"])
        self.assert_no_journal()

    def test_interrupted_recovery_preserves_manual_edits_and_recovery_roots(self):
        self.configure()
        first = self.generation({"app": "Before"})
        self.cli("switch")
        second = self.generation({"app": "After"})
        self.change_state(interrupt_once=["systemctl:daemon-reload"])
        self.assertNotEqual(self.cli("switch", code=None).returncode, 0)
        path = self.unit_dir / "hooddy-app.service"
        edited = path.read_text() + "# operator recovery edit\n"
        path.write_text(edited)
        result = self.cli("switch", code=1)
        self.assertIn("recovery", result.stderr.lower())
        self.assertEqual(path.read_text(), edited)
        self.assertTrue((self.directory / ".switch/journal.json").exists())
        self.assertTrue(self.rooted(first))
        self.assertTrue(self.rooted(second))

    def test_recovery_refuses_new_unit_conflict(self):
        self.configure()
        self.generation({"app": "Before"})
        self.cli("switch")
        self.generation({"app": "After"})
        self.change_state(interrupt_once=["systemctl:daemon-reload"])
        self.assertNotEqual(self.cli("switch", code=None).returncode, 0)
        before = len(self.state()["calls"])
        self.change_state(fragments={"hooddy-app.service": "/etc/systemd/user/hooddy-app.service"})
        self.cli("switch", code=1)
        self.assertTrue((self.directory / ".switch/journal.json").exists())
        self.assertNotIn(["systemctl", "--user", "start", "hooddy-app.service"],
                         self.state()["calls"][before:])

    def test_crash_during_first_activation_rolls_back_before_the_next_build(self):
        self.configure()
        candidate = self.generation({"app": "App"})
        self.change_state(interrupt_once=["systemctl:daemon-reload"])
        self.assertNotEqual(self.cli("switch", code=None).returncode, 0)
        self.assertTrue((self.directory / ".switch/journal.json").exists())
        self.change_state(fail_once=["nix:build"])
        self.cli("switch", code=1)
        self.assertEqual(self.managed_files(), {})
        self.assertFalse(self.rooted(candidate))
        self.assertFalse(any(unit["active"] or unit["enabled"] for unit in self.state()["units"].values()))
        self.assert_no_journal()

    def test_switch_holds_existing_lock_through_build_and_activation(self):
        import fcntl
        self.configure()
        self.generation()
        with (self.directory / ".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process = subprocess.Popen(["bash", SCRIPT, "switch"], env=self.env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                time.sleep(0.2)
                self.assertIsNone(process.poll())
                self.assertEqual(self.state()["builds"], [])
                fcntl.flock(lock, fcntl.LOCK_UN)
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stdout + stderr)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_disabled_generation_removes_services_and_roots_but_preserves_user_data(self):
        self.configure()
        first = self.generation({"app": "App"})
        self.cli("switch")
        second = self.generation({"app": "Updated"})
        self.cli("switch")
        cert = self.root / "data/caddy/certificates/example.crt"
        cert.parent.mkdir(parents=True)
        cert.write_text("keep")
        disabled = self.generation(enabled=False)
        for _ in range(2):
            self.cli("switch")
            self.assertEqual(self.managed_files(), {})
            self.assertFalse(any(unit["active"] or unit["enabled"] for unit in self.state()["units"].values()))
            self.assertFalse(any(self.rooted(generation) for generation in (first, second, disabled)))
            self.assertEqual(self.state()["linger"], "yes")
            self.assertEqual(cert.read_text(), "keep")
            self.assertEqual((self.directory / "flake.lock").read_text(), self.lock_text)
            self.assert_no_journal()
        self.generation({"new": "New"})
        self.cli("switch")
        self.assertTrue(self.unit("new")["active"])

    def test_initial_disabled_switch_does_not_enable_linger(self):
        self.configure()
        self.generation(enabled=False)
        self.cli("switch")
        self.assertEqual(self.managed_files(), {})
        self.assertEqual(self.state()["linger"], "no")

    def test_disable_clears_persistent_and_runtime_unit_links(self):
        self.configure()
        self.generation({"app": "App"})
        self.cli("switch")
        state = self.state()
        state["units"]["hooddy-app.service"]["also_enabled_runtime"] = True
        state["units"]["hooddy.service"]["enabled_kind"] = "enabled-runtime"
        self.state_file.write_text(json.dumps(state))
        self.generation(enabled=False)
        self.cli("switch")
        for name in ("hooddy-app.service", "hooddy.service"):
            self.assertFalse(self.state()["units"][name]["enabled"])
        self.assertEqual(self.managed_files(), {})

    def test_failed_disable_rolls_back_running_units(self):
        self.configure()
        old = self.generation({"app": "App"})
        self.cli("switch")
        previous = self.managed_files()
        self.generation(enabled=False)
        self.change_state(fail_once=["systemctl:daemon-reload"])
        self.cli("switch", code=1)
        self.assertEqual(self.managed_files(), previous)
        self.assertTrue(self.rooted(old))
        for name in ("hooddy.service", "hooddy-app.service"):
            self.assertTrue(self.state()["units"][name]["active"])
            self.assertTrue(self.state()["units"][name]["enabled"])
        self.assert_no_journal()


if __name__ == "__main__":
    unittest.main()
