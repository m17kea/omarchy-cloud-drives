"""Isolated launcher contracts; no desktop, account, secrets or keyring access."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
spec = importlib.util.spec_from_file_location('icloud_signin', ROOT / 'bin/icloud-signin.py')
signin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(signin)


class LauncherTests(unittest.TestCase):
    def test_only_code_and_packaged_ui_are_staged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin = root / 'plugin'
            (plugin / 'signin').mkdir(parents=True)
            (plugin / 'signin/shell.qml').write_text('shell fixture')
            (plugin / 'ICloudSetup.qml').write_text('wizard fixture')
            (plugin / 'ProcessEnvironment.js').write_text('environment fixture')
            kit = root / 'kit'
            for name in ('Ui', 'Commons'):
                (kit / name).mkdir(parents=True)
                (kit / name / 'qmldir').touch()
            target = root / 'target'
            target.mkdir()
            signin.prepare_ui(target, plugin, kit)
            self.assertEqual({p.name for p in target.iterdir()},
                             {'shell.qml', 'ICloudSetup.qml', 'ProcessEnvironment.js', 'Ui', 'Commons'})
            self.assertEqual((target / 'ICloudSetup.qml').read_text(), 'wizard fixture')
            self.assertEqual((target / 'shell.qml').stat().st_mode & 0o777, 0o600)
            self.assertEqual((target / 'Ui').resolve(), kit / 'Ui')

    def test_window_environment_has_no_inherited_injection_or_debug_settings(self):
        with mock.patch.dict(os.environ, {
            'LD_PRELOAD': '/not-real.so', 'PYTHONPATH': '/not-real',
            'QML_IMPORT_PATH': '/not-real', 'QML_DEBUG_SERVER': '1234',
            'CLOUD_DRIVES_PLUGIN_DIR': '/untrusted', 'QT_LOGGING_RULES': '*.debug=true',
            'RCLONE_CONFIG_PASS': 'synthetic-secret', 'HTTPS_PROXY': 'http://untrusted',
        }):
            env = signin.window_environment()
        for name in ('LD_PRELOAD', 'PYTHONPATH', 'QML_IMPORT_PATH', 'QML_DEBUG_SERVER',
                     'RCLONE_CONFIG_PASS', 'HTTPS_PROXY'):
            self.assertNotIn(name, env)
        self.assertEqual(env['CLOUD_DRIVES_PLUGIN_DIR'], str(ROOT))
        self.assertEqual(env['QT_QPA_PLATFORM'], 'wayland')
        self.assertEqual(env['QML_DISABLE_DISK_CACHE'], '1')
        self.assertNotIn('synthetic-secret', json.dumps(env))

    def test_arguments_cannot_deliver_account_data_to_launcher(self):
        with mock.patch.object(sys, 'argv', ['icloud-signin.py', 'synthetic-secret']), \
             mock.patch.object(signin, 'protect_process') as protect, \
             self.assertRaises(signin.SecurityError):
            signin.main()
        protect.assert_not_called()

    def test_unprotected_process_never_opens_ui(self):
        with mock.patch.object(sys, 'argv', ['icloud-signin.py']), \
             mock.patch.object(signin, 'protect_process', side_effect=signin.SecurityError('fixed')), \
             mock.patch.object(signin, 'supervise') as supervise, \
             self.assertRaises(signin.SecurityError):
            signin.main()
        supervise.assert_not_called()

    def test_symlink_runtime_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'real').mkdir(mode=0o700)
            (root / 'link').symlink_to(root / 'real')
            with mock.patch.dict(os.environ, {'XDG_RUNTIME_DIR': str(root / 'link')}), \
                 self.assertRaises(signin.SecurityError):
                signin.private_runtime_root()

    def test_shared_panel_has_no_credential_ui_or_auth_response_parser(self):
        panel = (ROOT / 'Panel.qml').read_text()
        for fragment in ('ICloudSetup {', 'passwordField', 'pendingBegin', 'handleEvent', 'wizard.'):
            self.assertNotIn(fragment, panel)
        self.assertIn('root.signinLauncher', panel)
        self.assertIn('signinProc.startDetached()', panel)
        self.assertNotIn('signinProc.running', panel)
        standalone = (ROOT / 'signin/shell.qml').read_text()
        self.assertNotIn('IpcHandler', standalone)
        self.assertIn('!wizard.helperRunning', standalone)

    def test_supervisor_does_not_forward_child_output(self):
        code = (
            "import importlib.util,sys;sys.path.insert(0," + repr(str(ROOT / 'bin')) + ");"
            "spec=importlib.util.spec_from_file_location('signin'," + repr(str(ROOT / 'bin/icloud-signin.py')) + ");"
            "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
            "m.protect_process();"
            "status=m.supervise(['/usr/bin/python3','-Es','-c',"
            "\"import sys;print('synthetic-stdout');print('synthetic-stderr',file=sys.stderr)\"],m.safe_environment());"
            "print(status)"
        )
        result = subprocess.run(['/usr/bin/python3', '-Es', '-c', code],
                                capture_output=True, text=True, timeout=12, check=True)
        self.assertEqual(result.stdout.strip(), '0')
        self.assertEqual(result.stderr, '')

    def test_exited_ui_does_not_shorten_descendant_cleanup_grace(self):
        clock = [0.0]
        child = mock.Mock(pid=123456, wait=mock.Mock(return_value=0),
                          poll=mock.Mock(return_value=0))
        forced_kills = []

        def signal_group(pid, number):
            self.assertEqual(pid, child.pid)
            # Simulate a helper finishing its bounded cleanup after 18 seconds,
            # although its UI/group leader has exited before cleanup starts.
            if clock[0] >= 18:
                raise ProcessLookupError()
            if number == signin.signal.SIGKILL:
                forced_kills.append(clock[0])

        def sleep(seconds):
            self.assertGreater(seconds, 0)
            self.assertLessEqual(seconds, 0.05)
            clock[0] += seconds

        with mock.patch.object(signin.subprocess, 'Popen', return_value=child), \
             mock.patch.object(signin.signal, 'signal'), \
             mock.patch.object(signin.os, 'killpg', side_effect=signal_group), \
             mock.patch.object(signin.time, 'monotonic', side_effect=lambda: clock[0]), \
             mock.patch.object(signin.time, 'sleep', side_effect=sleep):
            self.assertEqual(signin.supervise(['/fixture/ui'], {}), 0)
        self.assertGreaterEqual(clock[0], 18)
        self.assertLess(clock[0], signin.CLEANUP_SECONDS)
        self.assertEqual(forced_kills, [])
        child.poll.assert_called()
        self.assertEqual(child.wait.call_args_list, [mock.call(timeout=0.25), mock.call(timeout=2)])

    def test_unresponsive_group_has_one_bounded_cleanup_deadline(self):
        clock = [0.0]
        signals = []
        child = mock.Mock(pid=123456, wait=mock.Mock(return_value=0))
        # The leader itself takes four seconds to exit; descendants never do.
        child.poll.side_effect = lambda: None if clock[0] < 4 else 0

        def signal_group(pid, number):
            self.assertEqual(pid, child.pid)
            if number:
                signals.append((number, clock[0]))

        with mock.patch.object(signin, 'SESSION_SECONDS', 0), \
             mock.patch.object(signin.subprocess, 'Popen', return_value=child), \
             mock.patch.object(signin.signal, 'signal'), \
             mock.patch.object(signin.os, 'killpg', side_effect=signal_group), \
             mock.patch.object(signin.time, 'monotonic', side_effect=lambda: clock[0]), \
             mock.patch.object(signin.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            self.assertEqual(signin.supervise(['/fixture/ui'], {}), 130)
        self.assertEqual(signals, [(signin.signal.SIGTERM, 0.0),
                                   (signin.signal.SIGKILL, signin.CLEANUP_SECONDS)])
        self.assertEqual(clock[0], signin.CLEANUP_SECONDS)
        # No separate leader wait is added to the shared group-cleanup budget.
        child.wait.assert_called_once_with(timeout=2)

    def test_detached_launcher_notifies_for_failures_and_duplicate_but_not_cancellation(self):
        for status in (0, 130, 75, 1, 3, -15):
            with self.subTest(status=status), \
                 mock.patch.object(signin, 'main', return_value=status), \
                 mock.patch.object(signin.subprocess, 'run') as notify:
                self.assertEqual(signin.cli(), status)
                if status in (0, 130):
                    notify.assert_not_called()
                else:
                    expected = signin.ALREADY_OPEN_MESSAGE if status == 75 else signin.FAILURE_MESSAGE
                    self.assertEqual(notify.call_args.args[0], [
                        '/usr/bin/notify-send', '--app-name', 'Cloud Drives', 'Cloud Drives', expected,
                    ])

    def test_protection_staging_and_spawn_errors_notify_without_diagnostics(self):
        for error in (signin.SecurityError('synthetic-secret'),
                      OSError('synthetic-secret'), subprocess.SubprocessError('synthetic-secret')):
            with self.subTest(error=type(error).__name__), \
                 mock.patch.object(signin, 'main', side_effect=error), \
                 mock.patch.object(signin.subprocess, 'run') as notify, \
                 mock.patch.object(signin.sys, 'stderr', new_callable=io.StringIO) as stderr:
                self.assertEqual(signin.cli(), 1)
                self.assertEqual(stderr.getvalue().strip(), signin.FAILURE_MESSAGE)
                self.assertEqual(notify.call_args.args[0][-1], signin.FAILURE_MESSAGE)
                self.assertNotIn('synthetic-secret', repr(notify.call_args))

    def test_failure_notification_uses_sanitized_environment_and_bounded_silent_process(self):
        with mock.patch.dict(os.environ, {
            'LD_PRELOAD': '/untrusted.so', 'HTTPS_PROXY': 'http://untrusted',
            'RCLONE_CONFIG_PASS': 'synthetic-secret', 'PATH': '/untrusted',
        }), mock.patch.object(signin.subprocess, 'run') as notify:
            signin.notify_failure(1)
            self.assertEqual(notify.call_args.kwargs['env'], signin.safe_environment(gui=True))
        options = notify.call_args.kwargs
        self.assertEqual(options['timeout'], 3)
        self.assertFalse(options['check'])
        for name in ('stdin', 'stdout', 'stderr'):
            self.assertEqual(options[name], subprocess.DEVNULL)
        self.assertNotIn('synthetic-secret', repr(notify.call_args))

    def test_notification_failure_never_changes_the_original_exit_status(self):
        for error in (FileNotFoundError('synthetic-secret'),
                      subprocess.TimeoutExpired('/usr/bin/notify-send', 3),
                      RuntimeError('synthetic-secret')):
            with self.subTest(error=type(error).__name__), \
                 mock.patch.object(signin, 'main', return_value=75), \
                 mock.patch.object(signin.subprocess, 'run', side_effect=error), \
                 mock.patch.object(signin.sys, 'stderr', new_callable=io.StringIO) as stderr:
                self.assertEqual(signin.cli(), 75)
                self.assertEqual(stderr.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
