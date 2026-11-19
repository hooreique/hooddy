"""Check the packaged command surface without connecting to a user manager."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

EXECUTABLE = sys.argv.pop()


class CommandTests(unittest.TestCase):
    def test_only_switch_is_advertised_and_accepted(self):
        with tempfile.TemporaryDirectory() as home:
            def invoke(*args):
                return subprocess.run([EXECUTABLE, *args], env=os.environ | {"HOME": home},
                                      capture_output=True, text=True, timeout=10)

            for args in [[], ["--help"], ["switch", "--help"], ["--version"]]:
                result = invoke(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("0.2.0", result.stdout)
            for args in [["install"], ["uninstall"], ["start"], ["stop"], ["restart"],
                         ["switch", "--http-port", "8080"], ["switch", "--help", "extra"],
                         ["--version", "extra"], ["switch", "./flake.nix"]]:
                with self.subTest(args=args):
                    self.assertNotEqual(invoke(*args).returncode, 0)
            result = invoke("switch")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(str(Path(home) / ".config/hooddy/flake.nix"), result.stderr)
            self.assertFalse(list(Path(home).iterdir()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
