"""Onboarding boundary tests. No real Apple credentials or network required."""

import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "icloud_onboarding", Path(__file__).resolve().parents[1] / "bin" / "icloud-onboarding.py"
)
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)

OLD = b"# Encrypted rclone configuration\nRCLONE_ENCRYPT_V0:\nold-ciphertext\n"
NEW = b"# Encrypted rclone configuration\nRCLONE_ENCRYPT_V0:\nnew-ciphertext\n"
SECRET = "test-password-do-not-emit"
CHALLENGE = {"State": "2fa_do", "Option": {"Name": "config_2fa", "Help": SECRET}}


class FakeRC:
    def __init__(self, config, runtime, cancelled, replies):
        self.config = config
        self.runtime = runtime
        self.cancelled = cancelled
        self.replies = list(replies)
        self.calls = []
        self.stopped = False
        self.started = False
        self.existing = {}

    def start(self):
        self.started = True

    def call(self, endpoint, params):
        if endpoint == "config/get":
            return dict(self.existing)
        self.calls.append((endpoint, params))
        expected, reply = self.replies.pop(0)
        if endpoint != expected:
            raise AssertionError((endpoint, expected))
        if isinstance(reply, Exception):
            raise reply
        if endpoint == "config/update" and not reply.get("State"):
            self.config.write_bytes(NEW)
        return reply

    def abort(self):
        self.stopped = True

    def stop(self):
        self.stopped = True


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config" / "iCloudDrive.conf"
        self.config.parent.mkdir(mode=0o700)
        self.config.write_bytes(OLD)
        self.config.chmod(0o600)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.events = []

    def flow(self, replies):
        def factory(config, runtime, cancelled):
            self.rc = FakeRC(config, runtime, cancelled, replies)
            return self.rc
        flow = bridge.Onboarding(lambda **event: self.events.append(event),
                                 config=self.config, runtime_root=self.runtime, rc_factory=factory)
        flow.service = mock.Mock(side_effect=[3, 0, 3])
        self.addCleanup(flow.close)
        return flow

    def test_success_sends_parameters_and_promotes_only_verified_ciphertext(self):
        flow = self.flow([("config/create", CHALLENGE), ("config/update", {}), ("operations/list", {})])
        flow.begin(" person@example.test ", SECRET)
        self.assertEqual(self.config.read_bytes(), OLD)
        self.assertEqual(self.events[-1]["kind"], "code")
        self.assertTrue(self.events[-1]["smsAvailable"])
        self.assertNotIn(SECRET, json.dumps(self.events))
        flow.answer("123456")
        self.assertTrue(flow.connected)
        self.assertEqual(self.config.read_bytes(), NEW)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        create, update, probe = self.rc.calls
        self.assertEqual(create[1]["parameters"]["apple_id"], "person@example.test")
        self.assertEqual(create[1]["opt"], {"obscure": True, "nonInteractive": True})
        self.assertEqual(update[1]["parameters"], {})
        self.assertEqual(update[1]["opt"]["state"], "2fa_do")
        self.assertEqual(update[1]["opt"]["result"], "123456")
        self.assertEqual(probe[0], "operations/list")
        self.assertFalse(probe[1]["opt"]["recurse"])
        self.assertEqual(self.events[-1]["phase"], "connected")
        self.assertTrue(self.rc.stopped)

    def test_cancel_removes_staging_and_preserves_existing_account(self):
        flow = self.flow([("config/create", CHALLENGE)])
        flow.begin("person@example.test", SECRET)
        staged_directory = flow.runtime
        flow.cancel()
        with self.assertRaises(bridge.Cancelled):
            flow.answer("123456")
        flow.close()
        self.assertFalse(staged_directory.exists())
        self.assertEqual(self.config.read_bytes(), OLD)
        flow.service.assert_not_called()

    def test_same_account_retains_cache_identity(self):
        flow = self.flow([("config/create", CHALLENGE)])
        original_prepare = flow.prepare
        def prepare():
            original_prepare()
            self.rc.existing = {"apple_id": "Person@Example.Test", "omarchy_cache_id": "a" * 32}
        flow.prepare = prepare
        flow.begin("person@example.test", SECRET)
        self.assertEqual(self.rc.calls[0][1]["parameters"]["omarchy_cache_id"], "a" * 32)

    def test_different_account_gets_a_separate_cache(self):
        flow = self.flow([("config/create", CHALLENGE)])
        original_prepare = flow.prepare
        def prepare():
            original_prepare()
            self.rc.existing = {"apple_id": "other@example.test", "omarchy_cache_id": "a" * 32}
        flow.prepare = prepare
        flow.begin("person@example.test", SECRET)
        cache_id = self.rc.calls[0][1]["parameters"]["omarchy_cache_id"]
        self.assertNotEqual(cache_id, "a" * 32)
        self.assertRegex(cache_id, r"^[a-f0-9]{32}$")

    def test_failed_probe_never_promotes_or_stops_mount(self):
        flow = self.flow([("config/create", CHALLENGE), ("config/update", {}),
                          ("operations/list", bridge.FlowError("network"))])
        flow.begin("person@example.test", SECRET)
        with self.assertRaisesRegex(bridge.FlowError, "network"):
            flow.answer("123456")
        self.assertEqual(self.config.read_bytes(), OLD)
        flow.service.assert_not_called()

    def test_adp_access_can_be_approved_without_disabling_protection(self):
        flow = self.flow([("config/create", CHALLENGE), ("config/update", {}),
                          ("operations/list", bridge.FlowError("approval")), ("operations/list", {})])
        flow.begin("person@example.test", SECRET)
        flow.answer("123456")
        self.assertEqual(self.events[-1]["kind"], "approval")
        self.assertIn("can stay on", self.events[-1]["message"])
        self.assertEqual(self.config.read_bytes(), OLD)
        flow.answer("true")
        self.assertTrue(flow.connected)

    def test_sms_phone_selection_uses_allowed_values_without_raw_help(self):
        phone = {"State": "2fa_sms_select", "Option": {
            "Name": "config_2fa_phone", "Help": SECRET,
            "Examples": [{"Value": "7_sms", "Help": SECRET}, {"Value": "8_sms", "Help": SECRET}],
        }}
        sms = {"State": "2fa_sms_8_sms", "Option": {"Name": "config_2fa_sms"}}
        flow = self.flow([("config/create", CHALLENGE), ("config/update", phone), ("config/update", sms)])
        flow.begin("person@example.test", SECRET)
        flow.answer("sms")
        self.assertEqual(self.events[-1]["kind"], "answer")
        flow.answer("2")
        self.assertEqual(self.rc.calls[-1][1]["opt"]["result"], "8_sms")
        self.assertFalse(self.events[-1]["smsAvailable"])
        self.assertNotIn(SECRET, json.dumps(self.events))

    def test_mistyped_code_keeps_challenge_without_apple_request(self):
        flow = self.flow([("config/create", CHALLENGE)])
        flow.begin("person@example.test", SECRET)
        flow.answer("123")
        self.assertEqual(len(self.rc.calls), 1)
        self.assertEqual(self.events[-1]["phase"], "challenge")

    def test_unknown_prompt_fails_without_exposing_backend_help(self):
        flow = self.flow([("config/create", {"State": "new_state", "Option": {"Name": "new_option", "Help": SECRET}})])
        with self.assertRaisesRegex(bridge.FlowError, "protocol"):
            flow.begin("person@example.test", SECRET)
        self.assertNotIn(SECRET, json.dumps(self.events))
        self.assertEqual(self.config.read_bytes(), OLD)

    def test_existing_mount_is_stopped_before_promotion(self):
        flow = self.flow([("config/create", CHALLENGE), ("config/update", {}), ("operations/list", {})])
        checks = iter([0, 0, 3])
        def service(action, timeout=45):
            self.assertEqual(self.config.read_bytes(), OLD)
            return next(checks)
        flow.service = mock.Mock(side_effect=service)
        flow.begin("person@example.test", SECRET)
        flow.answer("123456")
        self.assertEqual([call.args[0] for call in flow.service.call_args_list], ["is-active", "stop", "is-active"])

    def test_mount_stop_failure_preserves_live_config(self):
        flow = self.flow([("config/create", CHALLENGE), ("config/update", {}), ("operations/list", {})])
        flow.service = mock.Mock(side_effect=[0, 1, 0])
        flow.begin("person@example.test", SECRET)
        with self.assertRaisesRegex(bridge.FlowError, "mount_busy"):
            flow.answer("123456")
        self.assertEqual(self.config.read_bytes(), OLD)

    def test_concurrent_config_change_is_not_overwritten(self):
        flow = self.flow([("config/create", CHALLENGE), ("config/update", {}), ("operations/list", {})])
        flow.begin("person@example.test", SECRET)
        concurrent = OLD + b"concurrent-refresh\n"
        self.config.write_bytes(concurrent)
        with self.assertRaisesRegex(bridge.FlowError, "config_changed"):
            flow.answer("123456")
        self.assertEqual(self.config.read_bytes(), concurrent)

    def test_write_failure_preserves_old_config(self):
        flow = self.flow([("config/create", CHALLENGE), ("config/update", {}), ("operations/list", {})])
        flow.begin("person@example.test", SECRET)
        with mock.patch.object(bridge.os, "replace", side_effect=OSError()), self.assertRaisesRegex(bridge.FlowError, "storage"):
            flow.answer("123456")
        self.assertEqual(self.config.read_bytes(), OLD)
        self.assertEqual(list(self.config.parent.glob(".icloud-connect-*")), [])

    def test_cancellation_before_commit_preserves_original_ciphertext(self):
        flow = self.flow([])
        self.assertTrue(flow.cancel())
        with self.assertRaises(bridge.Cancelled):
            flow.promote(NEW)
        self.assertFalse(flow.connected)
        self.assertEqual(self.config.read_bytes(), OLD)
        self.assertEqual(list(self.config.parent.glob(".icloud-connect-*")), [])

    def test_reader_cancellation_after_rename_cannot_report_preservation(self):
        flow = self.flow([])
        replaced = threading.Event()
        cancel_started = threading.Event()
        cancel_finished = threading.Event()
        cancel_results = []
        original_replace = os.replace
        def replace(source, destination):
            original_replace(source, destination)
            replaced.set()
            self.assertTrue(cancel_started.wait(2))
            self.assertFalse(cancel_finished.is_set())
        def cancel_after_rename():
            if replaced.wait(2):
                cancel_started.set()
                cancel_results.append(flow.cancel())
                cancel_finished.set()
        thread = threading.Thread(target=cancel_after_rename)
        thread.start()
        with mock.patch.object(bridge.os, "replace", side_effect=replace):
            flow.promote(NEW)
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(cancel_results, [False])
        self.assertTrue(flow.connected)
        self.assertFalse(flow.cancelled.is_set())
        self.assertEqual(self.config.read_bytes(), NEW)

    def test_sigterm_after_rename_is_deferred_until_committed(self):
        """Exercise run's real signal handler at the exact old failure point."""
        flow = self.flow([])
        completed = threading.Event()
        original_replace = os.replace
        def replace(source, destination):
            original_replace(source, destination)
            # Send specifically to the main thread while its commit mask is
            # active; Python must not run the cancellation handler yet.
            signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
        def emit(**event):
            self.events.append(event)
            if event["phase"] == "connected":
                completed.set()
        def begin(_email, _password):
            flow.promote(NEW)
            emit(phase="connected", message="Connected")
        flow.begin = begin
        read_fd, write_fd = os.pipe()
        def writer():
            # Match the real reader: only main receives handled signals.
            signal.pthread_sigmask(signal.SIG_BLOCK, bridge.COMMIT_SIGNALS)
            try:
                os.write(write_fd, b'{"op":"begin"}\n')
                completed.wait(2)
            finally:
                os.close(write_fd)
        thread = threading.Thread(target=writer)
        thread.start()
        with os.fdopen(read_fd, "rb") as source, mock.patch.object(bridge.os, "replace", side_effect=replace):
            result = bridge.run(input_stream=source, emit=emit, flow_factory=lambda _: flow)
        thread.join(timeout=2)
        self.assertEqual(result, 0)
        self.assertTrue(flow.connected)
        self.assertFalse(flow.cancelled.is_set())
        self.assertEqual(self.config.read_bytes(), NEW)
        self.assertEqual(self.events[-1]["phase"], "connected")

    def test_unencrypted_or_symlink_config_is_rejected(self):
        flow = self.flow([])
        self.config.write_text("[iCloudDrive]\npassword = plain\n")
        with self.assertRaisesRegex(bridge.FlowError, "setup_required"):
            flow.begin("person@example.test", SECRET)
        flow.close()
        self.config.unlink()
        self.config.symlink_to(self.root / "elsewhere")
        other = self.flow([])
        with self.assertRaisesRegex(bridge.FlowError, "setup_required"):
            other.begin("person@example.test", SECRET)

    def test_helper_sessions_are_serialized(self):
        first = self.flow([("config/create", CHALLENGE)])
        first.begin("person@example.test", SECRET)
        other = self.flow([])
        with self.assertRaisesRegex(bridge.FlowError, "busy"):
            other.begin("person@example.test", SECRET)

    def test_subprocess_environment_ignores_ambient_rclone_configuration(self):
        with mock.patch.dict(os.environ, {"RCLONE_CONFIG": "/unrelated", "RCLONE_DUMP": "auth", "RCLONE_PASSWORD_COMMAND": "untrusted"}):
            env = bridge.clean_environment()
        self.assertFalse(any(key.startswith("RCLONE_") for key in env))

    def test_curated_errors_never_include_source_payload(self):
        for raw in (SECRET, "invalid password " + SECRET, "requestPCS: " + SECRET,
                    "verification code " + SECRET, "429 " + SECRET):
            self.assertNotIn(SECRET, bridge.MESSAGES[bridge.classify_error(raw)])

    def exercise_input_pipe(self, cancel_command):
        flow = self.flow([("config/create", CHALLENGE)])
        ready = threading.Event()
        def emit(**event):
            self.events.append(event)
            if event["phase"] == "challenge":
                ready.set()
        flow.emit = emit
        read_fd, write_fd = os.pipe()
        def writer():
            try:
                os.write(write_fd, (json.dumps({"op": "begin", "email": "person@example.test", "password": SECRET}) + "\n").encode())
                if ready.wait(2) and cancel_command:
                    os.write(write_fd, b'{"op":"cancel"}\n')
            finally:
                os.close(write_fd)
        thread = threading.Thread(target=writer)
        thread.start()
        with os.fdopen(read_fd, "rb") as source:
            result = bridge.run(input_stream=source, emit=emit, flow_factory=lambda _: flow)
        thread.join(timeout=3)
        self.assertTrue(ready.is_set())
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, 130)
        self.assertEqual(self.events[-1]["code"], "cancelled")
        self.assertEqual(self.config.read_bytes(), OLD)
        self.assertIsNone(flow.runtime)
        self.assertNotIn(SECRET, json.dumps(self.events))

    def test_ndjson_cancel_cleans_staging(self):
        self.exercise_input_pipe(cancel_command=True)

    def test_ndjson_eof_cleans_staging(self):
        self.exercise_input_pipe(cancel_command=False)


class HTTPTests(unittest.TestCase):
    def response(self, status, payload):
        response = mock.Mock()
        response.status = status
        response.read.return_value = payload
        connection = mock.Mock()
        connection.getresponse.return_value = response
        return connection

    def test_http_errors_are_sanitized(self):
        server = bridge.RCServer(Path("/config"), Path("/runtime"), threading.Event())
        connection = self.response(400, json.dumps({"error": "invalid password " + SECRET}).encode())
        with mock.patch.object(bridge, "UnixConnection", return_value=connection), self.assertRaisesRegex(bridge.FlowError, "credentials"):
            server.call("config/update", {"parameters": {}})

    def test_response_size_is_bounded(self):
        server = bridge.RCServer(Path("/config"), Path("/runtime"), threading.Event())
        connection = self.response(200, b"x" * (bridge.MAX_RESPONSE + 1))
        with mock.patch.object(bridge, "UnixConnection", return_value=connection), self.assertRaisesRegex(bridge.FlowError, "response_limit"):
            server.call("operations/list", {})
        connection.getresponse.return_value.read.assert_called_once_with(bridge.MAX_RESPONSE + 1)

    def test_http_timeout_maps_to_fixed_message(self):
        server = bridge.RCServer(Path("/config"), Path("/runtime"), threading.Event())
        connection = mock.Mock()
        connection.getresponse.side_effect = socket.timeout()
        with mock.patch.object(bridge, "UnixConnection", return_value=connection), self.assertRaisesRegex(bridge.FlowError, "timeout"):
            server.call("config/create", {})


@unittest.skipUnless(os.environ.get("RCLONE_TEST_BINARY"), "set RCLONE_TEST_BINARY for the local rclone contract check")
class RcloneContractTests(unittest.TestCase):
    def test_private_socket_encrypted_config_and_update_shape_without_apple(self):
        """Exercise the released rclone RC contract using its local backend."""
        binary = Path(os.environ["RCLONE_TEST_BINARY"]).resolve()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bindir = root / "bin"
            bindir.mkdir()
            (bindir / "rclone").symlink_to(binary)
            secret_tool = bindir / "secret-tool"
            secret_tool.write_text("#!/bin/sh\nprintf '%s' 'integration-only-config-key'\n")
            secret_tool.chmod(0o700)
            config = root / "contract.conf"
            config.touch(mode=0o600)
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            env = bridge.clean_environment()
            env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
            encrypted = subprocess.run(
                [str(binary), "config", "encryption", "set", "--config", str(config),
                 "--password-command", bridge.PASSWORD_COMMAND, "--ask-password=false"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env, timeout=10, check=False,
            )
            self.assertEqual(encrypted.returncode, 0)
            self.assertIn(bridge.ENCRYPTED_MARKER, bridge.read_ciphertext(config))
            server = bridge.RCServer(config, runtime, threading.Event())
            try:
                with mock.patch.dict(os.environ, env, clear=True):
                    server.start()
                created = server.call("config/create", {"name": "ContractTest", "type": "local",
                                      "parameters": {"omarchy_cache_id": "a" * 32},
                                      "opt": {"nonInteractive": True}})
                self.assertFalse(created.get("State"))
                saved = server.call("config/get", {"name": "ContractTest"})
                self.assertEqual(saved["omarchy_cache_id"], "a" * 32)
                updated = server.call("config/update", {"name": "ContractTest", "parameters": {},
                                      "opt": {"continue": True, "nonInteractive": True,
                                              "state": "", "result": ""}})
                self.assertFalse(updated.get("State"))
                listing = server.call("operations/list", {"fs": "ContractTest:" + str(root), "remote": "",
                                      "opt": {"dirsOnly": True, "recurse": False,
                                              "noModTime": True, "noMimeType": True}})
                self.assertIsInstance(listing["list"], list)
            finally:
                server.stop()
            self.assertIn(bridge.ENCRYPTED_MARKER, bridge.read_ciphertext(config))
            self.assertNotIn(b"[ContractTest]", config.read_bytes())


if __name__ == "__main__":
    unittest.main()
