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
        self.directory = self.config / "hooddy"
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

    def install(self, with_import=True):
        self.cli("install")
        if with_import:
            config = self.directory / "Caddyfile"
            config.write_text(config.read_text() + "\nimport generated/sites.caddy\n")
        (self.directory / "flake.nix").write_text("{}\n")
        (self.directory / "flake.lock").write_text(self.lock_text)
        (self.directory / "untracked.nix").write_text("untracked configuration\n")

    def generation(self, applications=None, sites="", **manifest_changes):
        self.generation_number += 1
        generation = self.root / f"generation-{self.generation_number}"
        (generation / "units").mkdir(parents=True)
        manifest = {"protocolVersion": 1, "system": HOST_SYSTEM,
                    "sites": "sites.caddy", "services": {}}
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
        (generation / "sites.caddy").write_text(sites)
        self.change_state(generation=str(generation))
        return generation

    def managed_files(self):
        files = list(self.unit_dir.glob("hooddy-*.service"))
        generated = self.directory / "generated/sites.caddy"
        if generated.exists():
            files.append(generated)
        return {str(path.relative_to(self.config)): path.read_bytes() for path in files}

    def assert_no_journal(self):
        self.assertFalse((self.directory / ".switch/journal.json").exists())

    def rooted(self, generation):
        return any(path.is_symlink() and path.resolve() == generation
                   for path in (self.directory / ".switch").rglob("*"))

    def test_requires_installation_and_enabled_linger(self):
        self.assertIn("not installed", self.cli("switch", code=1).stderr)
        self.install()
        self.generation()
        self.change_state(linger="no")
        result = self.cli("switch", code=1)
        self.assertIn("loginctl enable-linger", result.stderr)
        self.assertEqual(self.state()["linger"], "no")
        self.assertEqual(self.state()["builds"], [])

    def test_missing_import_is_actionable_without_editing_main_file(self):
        self.install(with_import=False)
        self.generation()
        config = self.directory / "Caddyfile"
        old = config.read_bytes()
        result = self.cli("switch", code=1)
        self.assertIn("import generated/sites.caddy", result.stderr)
        self.assertEqual(config.read_bytes(), old)
        self.assertEqual(self.managed_files(), {})

    def test_commented_import_does_not_authorize_generated_config(self):
        self.install(with_import=False)
        config = self.directory / "Caddyfile"
        config.write_text(config.read_text() + "\n# import generated/sites.caddy\n")
        self.generation({"app": "App"})
        self.cli("switch", code=1)
        self.assertEqual(self.managed_files(), {})

    def test_unused_snippet_import_does_not_authorize_generated_config(self):
        self.install(with_import=False)
        config = self.directory / "Caddyfile"
        config.write_text(config.read_text() + "\n(unused) {\n import generated/sites.caddy\n}\n")
        self.generation({"app": "App"})
        self.cli("switch", code=1)
        self.assertEqual(self.managed_files(), {})

    def test_empty_and_populated_generations_include_ordinary_sites(self):
        self.install()
        ordinary = self.directory / "sites.d/existing.caddy"
        ordinary.write_text("http://ordinary.test:18080 {\n respond ordinary\n}\n")
        main = (self.directory / "Caddyfile").read_bytes()
        self.generation()
        self.cli("switch")
        generated = self.directory / "generated/sites.caddy"
        self.assertEqual(generated.read_text(), "")
        content = "http://generated.test:18081 {\n respond generated\n}\n"
        self.generation({"example": "Example"}, sites=content)
        self.cli("switch")
        self.assertEqual(generated.read_text(), content)
        self.assertEqual((self.directory / "Caddyfile").read_bytes(), main)
        self.assertTrue(ordinary.exists())
        self.assertTrue(self.unit("example")["active"])
        self.assertTrue(self.unit("example")["enabled"])
        self.assertTrue(self.state()["units"]["hooddy.service"]["active"])
        self.assertFalse(self.state()["units"]["hooddy.service"]["enabled"])
        self.assert_no_journal()

    def test_snapshot_includes_local_files_and_preserves_lock(self):
        self.install()
        generation = self.generation()
        self.cli("switch")
        builds = self.state()["builds"]
        self.assertEqual(len(builds), 1)
        self.assertNotEqual(builds[0]["source"], str(self.directory))
        self.assertEqual(builds[0]["lock"], self.lock_text)
        self.assertEqual(builds[0]["local_file"], "untracked configuration\n")
        self.assertEqual((self.directory / "flake.lock").read_text(), self.lock_text)
        self.assertTrue(self.rooted(generation))

    def test_rejects_protocol_platform_paths_and_health_urls_before_mutation(self):
        self.install()
        definitions = [
            {"protocolVersion": 2}, {"system": "unsupported-linux"},
            {"sites": "../sites.caddy"},
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
                self.assertFalse(self.state()["units"]["hooddy.service"]["active"])

    def test_rejects_unmanaged_units_and_generated_file(self):
        self.install()
        self.generation({"app": "App"})
        unit = self.unit_dir / "hooddy-app.service"
        unit.write_text("# Managed by hooddy.\noperator-owned\n")
        self.cli("switch", code=1)
        self.assertIn("operator-owned", unit.read_text())
        unit.unlink()
        generated = self.directory / "generated/sites.caddy"
        generated.parent.mkdir(exist_ok=True)
        generated.write_text("operator-owned\n")
        self.cli("switch", code=1)
        self.assertEqual(generated.read_text(), "operator-owned\n")
        generated.unlink()
        victim = self.root / "victim"
        victim.write_text("untouched")
        generated.symlink_to(victim)
        self.cli("switch", code=1)
        self.assertTrue(generated.is_symlink())
        self.assertEqual(victim.read_text(), "untouched")

    def test_rejects_symlinked_generated_directory(self):
        self.install()
        self.generation({"app": "App"})
        external = self.root / "external"
        external.mkdir()
        (self.directory / "generated").symlink_to(external, target_is_directory=True)
        self.cli("switch", code=1)
        self.assertEqual(list(external.iterdir()), [])
        self.assertFalse((self.unit_dir / "hooddy-app.service").exists())

    def test_rejects_unit_conflicts_and_dropins(self):
        self.install()
        self.generation({"app": "App"})
        self.change_state(fragments={"hooddy-app.service": "/etc/systemd/user/hooddy-app.service"})
        self.cli("switch", code=1)
        self.assertEqual(self.managed_files(), {})
        self.change_state(fragments={}, dropins={"hooddy-app.service": "/etc/systemd/user/hooddy-app.service.d/custom.conf"})
        self.cli("switch", code=1)
        self.assertEqual(self.managed_files(), {})

    def test_ownership_hash_detects_manual_edits_and_missing_files_reconcile(self):
        self.install()
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
        generated = self.directory / "generated/sites.caddy"
        generated.write_text("# local edit\n")
        self.cli("switch", code=1)
        self.assertEqual(generated.read_text(), "# local edit\n")

    def test_managed_file_changed_during_validation_is_preserved(self):
        self.install()
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
        self.install()
        self.generation({"app": "App"})
        lock = self.directory / "flake.lock"
        changed = self.lock_text + "\n"
        self.change_state(mutate_once=[{"selector": "nix:build", "path": str(lock),
                                       "content": changed}])
        self.cli("switch", code=1)
        self.assertEqual(lock.read_text(), changed)
        self.assertEqual(self.managed_files(), {})

    def test_add_change_remove_and_reconcile_without_restarting_healthy_units(self):
        self.install()
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

    def test_caddy_enablement_is_preserved(self):
        self.install()
        self.cli("start")
        self.generation()
        self.cli("switch")
        self.assertTrue(self.state()["units"]["hooddy.service"]["enabled"])

    def test_full_caddy_validation_detects_invalid_existing_and_generated_sites(self):
        self.install()
        self.generation({"app": "App"})
        existing = self.directory / "sites.d/invalid.caddy"
        existing.write_text("http://existing.test:18080 {\n unknown_directive\n}\n")
        self.cli("switch", code=1)
        self.assertEqual(self.managed_files(), {})
        existing.unlink()
        self.generation({"app": "App"}, "http://app.test:18080 {\n unknown_directive\n}\n")
        self.cli("switch", code=1)
        self.assertEqual(self.managed_files(), {})

    def test_unit_validation_failure_precedes_mutation(self):
        self.install()
        self.generation({"app": "App"})
        self.change_state(fail_once=["systemd-analyze:verify"])
        self.cli("switch", code=1)
        self.assertEqual(self.managed_files(), {})

    def test_unit_contract_rejects_nonstore_executables_and_extra_install_links(self):
        self.install()
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
        self.install()
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
        self.install()
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
        self.install()
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
        self.install()
        with self.health_server() as good:
            with self.health_server(status=302, redirect=good) as url:
                self.generation({"app": {"healthCheck": {"url": url, "timeoutSeconds": 1}}})
                self.cli("switch", code=1)
        self.assertEqual(self.managed_files(), {})
        self.assertFalse(self.unit("app")["active"])

    def test_first_activation_failure_restores_absence_and_stopped_caddy(self):
        self.install()
        self.generation({"app": "App"}, "http://app.test:18080 {\n respond app\n}\n")
        self.change_state(fail_once=["systemctl:reload-or-restart:hooddy.service"])
        self.cli("switch", code=1)
        self.assertEqual(self.managed_files(), {})
        self.assertFalse(self.unit("app")["active"])
        self.assertFalse(self.unit("app")["enabled"])
        self.assertFalse(self.state()["units"]["hooddy.service"]["active"])
        self.assert_no_journal()

    def test_caddy_reload_failure_restores_previous_generation(self):
        self.install()
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
        self.install()
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
        self.install()
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
        self.install()
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
        self.install()
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
        self.install()
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

    def test_interrupted_first_rollback_recovers_its_temporary_empty_import(self):
        self.install(with_import=False)
        self.cli("start")
        config = self.directory / "Caddyfile"
        config.write_text(config.read_text() + "\nimport generated/sites.caddy\n")
        self.generation({"app": "App"})
        self.change_state(interrupt_once=["systemctl:daemon-reload"])
        self.assertNotEqual(self.cli("switch", code=None).returncode, 0)
        self.change_state(interrupt_once=["systemctl:reload-or-restart:hooddy.service"])
        self.assertNotEqual(self.cli("restart", code=None).returncode, 0)
        generated = self.directory / "generated/sites.caddy"
        self.assertEqual(generated.read_text(), "")
        self.assertTrue((self.directory / ".switch/journal.json").exists())
        self.change_state(fail_once=["nix:build"])
        self.cli("switch", code=1)
        self.assertEqual(self.managed_files(), {})
        self.assertTrue(self.state()["units"]["hooddy.service"]["active"])
        self.assertTrue(self.state()["units"]["hooddy.service"]["enabled"])
        self.assert_no_journal()

    def test_switch_holds_existing_lock_through_build_and_activation(self):
        import fcntl
        self.install()
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

    def test_uninstall_refuses_until_services_removed(self):
        self.install()
        self.generation({"app": "App"})
        self.cli("switch")
        self.cli("uninstall", code=1)
        self.assertTrue((self.unit_dir / "hooddy.service").exists())
        self.assertTrue(self.unit("app")["active"])
        self.generation()
        self.cli("switch")
        self.cli("uninstall")
        self.assertFalse((self.unit_dir / "hooddy.service").exists())


if __name__ == "__main__":
    unittest.main()
