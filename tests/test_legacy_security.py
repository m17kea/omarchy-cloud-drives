"""iCloud shell routes must never collect credentials or invoke legacy auth.

Run a copied dispatcher with an inert launcher and tripwire commands. These
tests never open a real sign-in UI, decrypt a config, or contact a keyring/API.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "omarchy-cloud-drives"


class LegacySecurityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plugin = self.root / "plugin with spaces"
        self.bin = self.plugin / "bin"
        self.bin.mkdir(parents=True)
        self.script = self.bin / SCRIPT.name
        shutil.copyfile(SCRIPT, self.script)
        for name in ('protected-exec.py', 'cloud_drives_security.py'):
            shutil.copyfile(ROOT / 'bin' / name, self.bin / name)
        self.trace = self.root / "forbidden-actions"
        # An absolute Python interpreter must launch this fixture even if PATH
        # contains an unrelated python3. -Es must ignore Python startup options.
        (self.bin / "icloud-signin.py").write_text(
            "import json, sys\n"
            "print(json.dumps({'launcher': True, 'args': sys.argv[1:], "
            "'ignore_environment': sys.flags.ignore_environment, "
            "'no_user_site': sys.flags.no_user_site}))\n"
        )
        # Also catch an absolute-interpreter runtime probe, not only PATH use.
        (self.bin / "cloud_drives_runtime.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "Path(os.environ['CLOUD_DRIVES_TEST_TRACE']).write_text('runtime probe')\n"
            "raise SystemExit(91)\n"
        )
        self.commands = self.root / "commands"
        self.commands.mkdir()
        for command in ("python3", "rclone", "gum", "curl", "secret-tool", "systemctl", "omarchy"):
            path = self.commands / command
            path.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' '" + command + "' >> \"$CLOUD_DRIVES_TEST_TRACE\"\n"
                "exit 92\n"
            )
            path.chmod(0o700)
        self.env = dict(
            os.environ,
            HOME=str(self.root / "home"),
            XDG_CONFIG_HOME=str(self.root / "config"),
            XDG_DATA_HOME=str(self.root / "data"),
            XDG_CACHE_HOME=str(self.root / "cache"),
            PATH=str(self.commands) + os.pathsep + "/usr/bin:/bin",
            CLOUD_DRIVES_TEST_TRACE=str(self.trace),
            PYTHONINSPECT="1",
        )

    def run_route(self, command, trace=False):
        argv = ["/bin/bash"] + (["-x"] if trace else []) + [str(self.script), command, "icloud"]
        return subprocess.run(argv, env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=5, check=True)

    def assert_launcher_only(self, result):
        self.assertEqual(json.loads(result.stdout), {
            "launcher": True, "args": [], "ignore_environment": 1, "no_user_site": 1,
        })
        self.assertFalse(self.trace.exists(), "Sign-in invoked a forbidden shell/runtime action")
        for directory in ("home", "config", "data", "cache"):
            self.assertFalse((self.root / directory).exists(), "Sign-in performed setup before its launcher")

    def test_connect_icloud_execs_isolated_launcher_without_setup_or_rclone(self):
        self.assert_launcher_only(self.run_route("connect"))

    def test_reconnect_icloud_execs_launcher_without_querying_existing_credentials(self):
        # No existing config is needed merely to open the sign-in UI.
        self.assert_launcher_only(self.run_route("reconnect"))

    def test_shell_tracing_is_disabled_before_other_actions(self):
        self.assertEqual(SCRIPT.read_text().splitlines()[:2], ["#!/bin/bash", "set +x"])
        result = self.run_route("connect", trace=True)
        self.assert_launcher_only(result)
        self.assertEqual(result.stderr.strip(), "+ set +x")

    def test_deprecated_apple_credentials_and_curl_rc_path_are_absent(self):
        source = SCRIPT.read_text()
        for deprecated in ("rc_start", "rc_stop", "rc_call", "rc_configure_icloud",
                           "gum input", "--unix-socket", "config/update", "config/create",
                           "local apple_id", "unset pw", ".Option.Help"):
            with self.subTest(deprecated=deprecated):
                self.assertNotIn(deprecated, source)

    def test_google_and_onedrive_keep_browser_authentication(self):
        source = SCRIPT.read_text()
        self.assertIn('config create "$remote" drive scope=drive config_is_local=true', source)
        self.assertIn('config create "$remote" onedrive', source)
        self.assertIn('config reconnect "$(remote_of "$id"):"', source)


if __name__ == "__main__":
    unittest.main()
