"""Synthetic security checks; real process changes occur in child processes only."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cloud_drives_security", ROOT / "bin" / "cloud_drives_security.py"
)
security = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(security)


class EnvironmentTests(unittest.TestCase):
    def test_only_required_environment_is_retained(self):
        source = {
            "HOME": "/synthetic/home", "USER": "fixture", "LOGNAME": "fixture",
            "XDG_CONFIG_HOME": "/synthetic/config", "XDG_DATA_HOME": "/synthetic/data",
            "XDG_CACHE_HOME": "/synthetic/cache", "XDG_RUNTIME_DIR": "/synthetic/run",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/synthetic/bus",
            "LANG": "C.UTF-8", "LC_ALL": "C", "LC_MESSAGES": "C",
            "TZ": "UTC", "NOTIFY_SOCKET": "/synthetic/notify",
            "PATH": "/untrusted/bin", "TERM": "xterm", "COLORTERM": "truecolor",
            "OMARCHY_PATH": "/untrusted/omarchy",
            "RCLONE_CONFIG_PASS": "fixture-secret", "RCLONE_DUMP": "auth",
            "LD_PRELOAD": "/fixture/inject.so", "LD_LIBRARY_PATH": "/fixture/lib",
            "PYTHONPATH": "/fixture/python", "PYTHONINSPECT": "1",
            "QT_LOGGING_RULES": "*.debug=true", "QT_PLUGIN_PATH": "/fixture/qt",
            "QML_IMPORT_PATH": "/fixture/qml", "QML_DEBUG_SERVER": "fixture",
            "QS_CONFIG_PATH": "/fixture/shell", "QS_DEBUG": "1",
            "HTTP_PROXY": "http://fixture", "https_proxy": "http://fixture",
            "ALL_PROXY": "http://fixture", "SSL_CERT_FILE": "/fixture/cert",
            "SSL_CERT_DIR": "/fixture/certs", "SSLKEYLOGFILE": "/fixture/tls.log",
            "CURL_HOME": "/fixture/curl", "BASH_ENV": "/fixture/bash",
            "SHELLOPTS": "xtrace", "GODEBUG": "fixture",
        }
        expected = {key: source[key] for key in security._BASE_ENVIRONMENT}
        expected.update(LC_ALL="C", LC_MESSAGES="C", PATH="/usr/bin:/bin", OMARCHY_PATH="/usr/share/omarchy")
        actual = security.safe_environment(source)
        self.assertEqual(actual, expected)
        self.assertEqual(source["PATH"], "/untrusted/bin")
        self.assertNotIn("fixture-secret", json.dumps(actual))

    def test_gui_access_is_explicit_and_does_not_enable_debug_settings(self):
        source = {"WAYLAND_DISPLAY": "wayland-fixture", "DISPLAY": ":55",
                  "XDG_SESSION_TYPE": "wayland", "QT_QPA_PLATFORMTHEME": "unsafe",
                  "QML_IMPORT_PATH": "/untrusted", "QS_CONFIG_PATH": "/untrusted"}
        self.assertEqual(security.safe_environment(source), {"PATH": "/usr/bin:/bin", "OMARCHY_PATH": "/usr/share/omarchy"})
        self.assertEqual(security.safe_environment(source, gui=True), {
            "WAYLAND_DISPLAY": "wayland-fixture", "DISPLAY": ":55",
            "XDG_SESSION_TYPE": "wayland", "PATH": "/usr/bin:/bin", "OMARCHY_PATH": "/usr/share/omarchy",
        })

    def test_default_reads_environment_but_empty_mapping_does_not(self):
        with mock.patch.dict(os.environ, {"HOME": "/fixture", "RCLONE_DUMP": "auth"}, clear=True):
            self.assertEqual(security.safe_environment(), {"HOME": "/fixture", "PATH": "/usr/bin:/bin", "OMARCHY_PATH": "/usr/share/omarchy"})
            self.assertEqual(security.safe_environment({}), {"PATH": "/usr/bin:/bin", "OMARCHY_PATH": "/usr/share/omarchy"})


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            mock.patch.object(security.sys, "platform", "linux"),
            mock.patch.object(security.resource, "setrlimit"),
            mock.patch.object(security.resource, "getrlimit", return_value=(0, 0)),
            mock.patch("builtins.open", mock.mock_open(read_data="00000000\n")),
            mock.patch.object(security, "_prctl", return_value=0),
        ]
        self.platform, self.set_limit, self.get_limit, self.open_file, self.prctl = [
            patch.start() for patch in self.patches
        ]
        self.open_file().read.side_effect = ["00000033\n", "00000000\n"]
        self.open_file.reset_mock()
        for patch in self.patches:
            self.addCleanup(patch.stop)

    def assert_protection_rejected(self):
        with self.assertRaises(security.SecurityError) as raised:
            security.protect_process()
        self.assertEqual(str(raised.exception), security.SECURITY_MESSAGE)
        self.assertTrue(raised.exception.__suppress_context__)

    def test_applies_and_verifies_all_three_controls(self):
        security.protect_process()
        self.set_limit.assert_called_once_with(security.resource.RLIMIT_CORE, (0, 0))
        self.get_limit.assert_called_once_with(security.resource.RLIMIT_CORE)
        self.open_file.assert_any_call(security.CORE_FILTER, "w", encoding="ascii")
        self.open_file.assert_any_call(security.CORE_FILTER, "r", encoding="ascii")
        self.open_file().write.assert_called_once_with("0\n")
        self.prctl.assert_has_calls([mock.call(security.PR_SET_DUMPABLE, 0),
                                    mock.call(security.PR_GET_DUMPABLE)])
        self.assertEqual(self.prctl.call_count, 2)

    def test_resource_failure_is_fixed_and_closed(self):
        self.set_limit.side_effect = OSError("private diagnostic must not escape")
        self.assert_protection_rejected()
        self.open_file.assert_not_called()
        self.prctl.assert_not_called()

    def test_resource_readback_mismatch_is_rejected(self):
        self.get_limit.return_value = (0, 1)
        self.assert_protection_rejected()
        self.open_file.assert_not_called()

    def test_filter_write_failure_is_rejected(self):
        read_handle = self.open_file()
        def open_filter(_path, mode, **_kwargs):
            if mode == "w":
                raise PermissionError("private path must not escape")
            return read_handle
        self.open_file.side_effect = open_filter
        self.assert_protection_rejected()
        self.prctl.assert_not_called()

    def test_filter_readback_mismatch_or_invalid_value_is_rejected(self):
        for value in ("00000033\n", "unexpected"):
            with self.subTest(value=value):
                self.open_file().read.side_effect = ["00000033\n", value]
                self.assert_protection_rejected()
        self.prctl.assert_not_called()

    def test_already_protected_filter_is_not_reopened_for_writing(self):
        self.open_file().read.side_effect = ["00000000\n", "00000000\n"]
        self.open_file.reset_mock()
        security.protect_process()
        self.assertEqual(self.open_file.call_args_list, [
            mock.call(security.CORE_FILTER, "r", encoding="ascii"),
            mock.call(security.CORE_FILTER, "r", encoding="ascii"),
        ])

    def test_dumpability_failure_is_rejected(self):
        for result in (OSError("private diagnostic"), 1):
            with self.subTest(result=type(result).__name__):
                self.open_file().read.side_effect = ["00000033\n", "00000000\n"]
                self.prctl.side_effect = result if isinstance(result, Exception) else None
                self.prctl.return_value = result if not isinstance(result, Exception) else 0
                self.assert_protection_rejected()

    def test_dumpability_readback_mismatch_is_rejected(self):
        self.prctl.side_effect = [0, 1]
        self.assert_protection_rejected()

    def test_non_linux_process_is_rejected_before_mutation(self):
        with mock.patch.object(security.sys, "platform", "other"):
            self.assert_protection_rejected()
        self.set_limit.assert_not_called()


@unittest.skipUnless(sys.platform == "linux", "Linux process protections")
class ChildProcessTests(unittest.TestCase):
    def test_real_controls_and_exec_inheritance_without_crashing(self):
        # No secrets or crashes: only the throwaway child changes its limits.
        # Its second Python image reports which protections survived exec.
        after_exec = """
import ctypes, json, os, resource
libc = ctypes.CDLL(None)
dumpable = libc.prctl(3, 0, 0, 0, 0)
with open('/proc/self/coredump_filter') as source:
    core_filter = int(source.read().strip(), 16)
with open('/proc/self/status') as source:
    no_new_privs = next(line.split(':', 1)[1].strip() for line in source if line.startswith('NoNewPrivs:'))
print(json.dumps({'limits': resource.getrlimit(resource.RLIMIT_CORE),
                  'filter': core_filter, 'dumpable': dumpable,
                  'no_new_privs': no_new_privs}))
"""
        before_exec = """
import json, os, resource, sys
sys.path.insert(0, sys.argv[1])
import cloud_drives_security as security
with open('/proc/self/status') as source:
    before = next(line.split(':', 1)[1].strip() for line in source if line.startswith('NoNewPrivs:'))
security.protect_process()
security.protect_process()
print(json.dumps({'protected_dumpable': security._prctl(security.PR_GET_DUMPABLE),
                  'original_no_new_privs': before}), flush=True)
os.execve(sys.executable, [sys.executable, '-I', '-B', '-c', sys.argv[2]], security.safe_environment())
"""
        result = subprocess.run([sys.executable, "-I", "-B", "-c", before_exec,
                                 str(ROOT / "bin"), after_exec],
                                env=security.safe_environment(), capture_output=True,
                                text=True, timeout=10, check=True)
        before, after = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(before["protected_dumpable"], 0)
        self.assertEqual(after["limits"], [0, 0])
        self.assertEqual(after["filter"], 0)
        self.assertEqual(after["no_new_privs"], before["original_no_new_privs"])
        # Ordinary exec resets dumpability; callers must not rely on it being
        # inherited. The independently verified limit/filter remain in force.
        self.assertEqual(after["dumpable"], 1)


if __name__ == "__main__":
    unittest.main()
