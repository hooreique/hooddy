"""Exercise public CLI states without touching the host's user services."""

import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT, EXECUTABLE, CADDY, FAKE_SYSTEM = sys.argv[1:]
sys.argv = sys.argv[:1]


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hooddy-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config"
        self.data = self.root / "data"
        self.runtime = self.root / "run"
        self.runtime.mkdir(mode=0o700)
        self.unit = self.config / "systemd/user/hooddy.service"
        self.directory = self.config / "hooddy"
        self.state_file = self.root / "state.json"
        self.state_file.write_text(json.dumps({
            "active": False, "enabled": False, "linger": "no",
            "starts": 0, "reloads": 0, "calls": [], "fail": [],
        }))
        binaries = self.root / "bin"
        binaries.mkdir()
        for name in ["systemctl", "loginctl", "nix-store"]:
            destination = binaries / name
            destination.write_text(f"#!{sys.executable}\n" +
                                   Path(FAKE_SYSTEM).read_text().split("\n", 1)[1])
            destination.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.root),
                        XDG_CONFIG_HOME=str(self.config), XDG_DATA_HOME=str(self.data),
                        XDG_RUNTIME_DIR=str(self.runtime), HOODDY_TEST_STATE=str(self.state_file),
                        PATH=f"{binaries}:{os.environ['PATH']}")
        self.env.pop("NOTIFY_SOCKET", None)

    def state(self):
        return json.loads(self.state_file.read_text())

    def change_state(self, **changes):
        self.state_file.write_text(json.dumps(self.state() | changes))

    def cli(self, *args, code=0, env=None):
        result = subprocess.run(["bash", SCRIPT, *args], env=env or self.env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def site(self, name, content):
        path = self.directory / "sites.d" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_packaged_help_and_version_do_not_require_installation(self):
        for args in [[], ["--help"], ["install", "--help"], ["--version"]]:
            result = subprocess.run([EXECUTABLE, *args], env=self.env,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("0.1.0", result.stdout)
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.state()["calls"], [])

    def test_commands_require_installation(self):
        for command in ["start", "stop", "restart", "uninstall"]:
            self.assertIn("not installed", self.cli(command, code=1).stderr)
        self.assertEqual(self.state()["calls"], [])

    def test_invalid_arguments(self):
        for args in [["unknown"], ["install", "--disable-linger"],
                     ["start", "extra"], ["--version", "extra"]]:
            self.cli(*args, code=1)
        self.assertEqual(self.state()["calls"], [])

    def test_install_is_strict_and_only_installs(self):
        module = self.site("unrelated.caddy", "http://foo.test {\n respond hello\n}\n")
        self.cli("install")
        self.assertEqual(self.state()["linger"], "yes")
        self.assertFalse(self.state()["enabled"])
        self.assertFalse(self.state()["active"])
        self.assertEqual((self.directory / "caddy").resolve(), Path(CADDY).parent.parent)
        self.assertTrue(module.exists())
        calls = self.state()["calls"]
        self.assertIn("already installed", self.cli("install", code=1).stderr)
        self.assertEqual(self.state()["calls"], calls)

    def test_start_and_stop_are_idempotent(self):
        self.cli("install")
        self.cli("start")
        self.cli("start")
        self.assertTrue(self.state()["enabled"])
        self.assertTrue(self.state()["active"])
        self.assertEqual(self.state()["starts"], 1)
        self.cli("stop")
        self.cli("stop")
        self.assertFalse(self.state()["enabled"])
        self.assertFalse(self.state()["active"])

    def test_restart_reloads_or_starts_without_changing_enablement(self):
        self.cli("install")
        self.cli("restart")
        self.assertTrue(self.state()["active"])
        self.assertFalse(self.state()["enabled"])
        self.cli("restart")
        self.assertEqual(self.state()["starts"], 1)
        self.assertEqual(self.state()["reloads"], 1)
        self.cli("start")
        self.cli("restart")
        self.assertTrue(self.state()["enabled"])
        self.assertEqual(self.state()["starts"], 1)

    def test_uninstall_preserves_sites_config_and_certificates(self):
        module = self.site("foo.caddy", "http://foo.test {\n respond hello\n}\n")
        self.cli("install")
        config = (self.directory / "Caddyfile").read_text()
        certificate = self.data / "caddy/certificate"
        certificate.parent.mkdir(parents=True)
        certificate.write_text("retain")
        self.cli("start")
        self.cli("uninstall")
        self.assertFalse(self.unit.exists())
        self.assertFalse((self.directory / "caddy").is_symlink())
        self.assertFalse(self.state()["active"])
        self.assertFalse(self.state()["enabled"])
        self.assertEqual(self.state()["linger"], "yes")
        self.assertTrue(module.exists())
        self.assertEqual(certificate.read_text(), "retain")
        self.assertEqual((self.directory / "Caddyfile").read_text(), config)
        self.cli("uninstall", code=1)
        self.cli("install")
        self.assertEqual((self.directory / "Caddyfile").read_text(), config)

    def test_uninstall_can_disable_linger(self):
        self.cli("install")
        self.cli("uninstall", "--disable-linger")
        self.assertEqual(self.state()["linger"], "no")

    def test_linger_already_enabled(self):
        self.change_state(linger="yes")
        self.cli("install")
        self.assertFalse(any(call[:2] == ["loginctl", "enable-linger"]
                             for call in self.state()["calls"]))

    def test_linger_failure_is_actionable_and_does_not_install(self):
        self.change_state(fail=["enable-linger"])
        self.assertIn("loginctl enable-linger", self.cli("install", code=1).stderr)
        self.assertFalse(self.unit.exists())
        self.assertFalse((self.directory / "caddy").is_symlink())

    def test_install_failures_remove_partial_installation(self):
        for failure in ["nix-store", "daemon-reload", "disable"]:
            with self.subTest(failure=failure):
                self.change_state(fail=[failure])
                self.cli("install", code=1)
                self.assertFalse(self.unit.exists())
                self.assertFalse((self.directory / "caddy").is_symlink())
        self.change_state(fail=[])
        self.cli("install")

    def test_uninstall_failure_retains_installation_for_retry(self):
        self.cli("install")
        for failure, args in [("disable", []), ("disable-linger", ["--disable-linger"])]:
            self.change_state(fail=[failure])
            self.cli("uninstall", *args, code=1)
            self.assertTrue(self.unit.exists())
            self.assertTrue((self.directory / "caddy").is_symlink())
        self.change_state(fail=[])
        self.cli("uninstall", "--disable-linger")

    def test_failed_reload_keeps_running_state(self):
        self.cli("install")
        self.cli("start")
        self.change_state(fail=["reload-or-restart"])
        self.cli("restart", code=1)
        self.assertTrue(self.state()["active"])
        self.assertTrue(self.state()["enabled"])

    def test_existing_unowned_unit_is_never_replaced_or_removed(self):
        self.unit.parent.mkdir(parents=True)
        self.unit.write_text("unrelated unit\n")
        for command in ["install", "uninstall", "start", "stop", "restart"]:
            self.cli(command, code=1)
        self.assertEqual(self.unit.read_text(), "unrelated unit\n")
        self.assertEqual(self.state()["calls"], [])

    def test_unit_in_another_systemd_search_path_is_not_overridden(self):
        self.change_state(fragment="/etc/systemd/user/hooddy.service")
        self.assertIn("already exists", self.cli("install", code=1).stderr)
        self.assertFalse(self.unit.exists())

    def test_user_caddyfile_is_preserved(self):
        self.directory.mkdir(parents=True)
        content = "{\n debug\n}\nimport sites.d/*.caddy\n"
        (self.directory / "Caddyfile").write_text(content)
        self.cli("install")
        self.assertEqual((self.directory / "Caddyfile").read_text(), content)

    def test_default_xdg_config_location(self):
        env = self.env.copy()
        del env["XDG_CONFIG_HOME"]
        del env["XDG_DATA_HOME"]
        self.cli("install", env=env)
        self.assertTrue((self.root / ".config/hooddy/Caddyfile").exists())

    def test_relative_xdg_paths_are_rejected(self):
        env = self.env | {"XDG_CONFIG_HOME": "relative"}
        self.cli("install", env=env, code=1)
        self.assertEqual(self.state()["calls"], [])

    def test_generated_unit_handles_spaces_and_systemd_metacharacters(self):
        config = self.root / 'config space $cash %n "quoted"'
        env = self.env | {"XDG_CONFIG_HOME": str(config)}
        self.cli("install", env=env)
        unit = config / "systemd/user/hooddy.service"
        # Use the real parser, without connecting to a systemd manager.
        checked = subprocess.run(["systemd-analyze", "--user", "verify", str(unit)],
                                 env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertNotIn("Unknown", checked.stderr)
        self.assertNotIn("Failed to resolve", checked.stderr)

    def test_real_caddy_empty_config_multiple_hosts_and_failed_reload(self):
        self.cli("install")
        (self.runtime / "hooddy").mkdir()
        config = self.directory / "Caddyfile"
        command = [CADDY, "run", "--config", str(config), "--adapter", "caddyfile"]
        with (self.root / "caddy.log").open("w+") as log:
            process = subprocess.Popen(command, env=self.env, stdout=log, stderr=log)
            try:
                admin = self.runtime / "hooddy/admin.sock"
                for _ in range(100):
                    if admin.exists() or process.poll() is not None:
                        break
                    time.sleep(0.05)
                self.assertIsNone(process.poll())
                self.assertTrue(admin.exists(), "empty configuration did not start")
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]

                def reload(code=0):
                    result = subprocess.run([CADDY, "reload", "--config", str(config),
                                             "--adapter", "caddyfile"], env=self.env,
                                            capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, code, result.stderr)

                def request(host, expected):
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    try:
                        connection.request("GET", "/", headers={"Host": host})
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.read().decode(), expected)
                    finally:
                        connection.close()

                for host in ["foo", "bar"]:
                    self.site(f"{host}.caddy", f'http://{host}.test:{port} {{\n'
                              f' bind 127.0.0.1\n respond "{host}"\n}}\n')
                reload()
                request("foo.test", "foo")
                request("bar.test", "bar")
                bad = self.site("broken.caddy", "this is not a valid Caddyfile {\n")
                reload(code=1)
                request("foo.test", "foo")
                request("bar.test", "bar")
                bad.unlink()
                (self.directory / "sites.d/foo.caddy").unlink()
                reload()
                request("bar.test", "bar")
                (self.directory / "sites.d/bar.caddy").unlink()
                reload()
                self.assertIsNone(process.poll())
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    unittest.main(verbosity=2)
