import configparser
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "bin" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


unit = load("render_unit", "render-unit.py")
mount = load("mount_drive", "mount-drive.py")


class MountContract(unittest.TestCase):
    def test_unit_protects_before_interpreter_and_suppresses_output(self):
        config = configparser.ConfigParser(interpolation=None)
        config.read(ROOT / 'systemd/omarchy-cloud-drive@.service')
        service = config['Service']
        self.assertEqual(service['LimitCORE'], '0')
        self.assertEqual(service['CoredumpFilter'], '0x0')
        self.assertEqual(service['StandardOutput'], 'null')
        self.assertEqual(service['StandardError'], 'null')
        self.assertTrue(service['ExecStart'].startswith('/usr/bin/python3 -Es '))
        self.assertTrue({'LD_PRELOAD', 'LD_AUDIT', 'LD_LIBRARY_PATH', 'LD_DEBUG',
                         'LD_DEBUG_OUTPUT', 'LD_PROFILE', 'LD_PROFILE_OUTPUT',
                         'LD_TRACE_LOADED_OBJECTS', 'GLIBC_TUNABLES', 'GCONV_PATH',
                         'LOCPATH'} <= set(service['UnsetEnvironment'].split()))
        self.assertNotIn('NoNewPrivileges', service)

    def test_unit_paths_do_not_expand_specifiers_or_environment(self):
        template = 'ExecStart="@WORKER@" %i "@CONFIG@" "@MOUNT@" "@CACHE@"'
        value = '/tmp/space path/quote"/percent%/dollar$NAME/slash\\'
        rendered = unit.render(template, value, value, value, value, value)
        self.assertIn('" %i "', rendered)
        self.assertIn('percent%%/dollar$$NAME', rendered)
        self.assertIn('quote\\"', rendered)
        self.assertIn('slash\\\\', rendered)

    def test_control_characters_rejected(self):
        with self.assertRaises(ValueError):
            unit.render('@MOUNT@', '/tmp/config', '/tmp/bad\npath', '/tmp/cache', '/tmp/worker', '/tmp/data')

    def test_runtime_data_home_is_preserved_in_service_environment(self):
        rendered = unit.render('Environment="XDG_DATA_HOME=@DATA@"', '/config', '/mount', '/cache', '/worker', '/data/a b%$c')
        self.assertEqual(rendered, 'Environment="XDG_DATA_HOME=/data/a b%%$c"')

    def test_account_cache_isolation(self):
        one = mount.mount_command('iCloudDrive', 'a' * 32, '/tmp/mount', '/tmp/cache', '/private/rclone')
        two = mount.mount_command('iCloudDrive', 'b' * 32, '/tmp/mount', '/tmp/cache', '/private/rclone')
        self.assertEqual(one[0], '/private/rclone')
        self.assertNotEqual(one[one.index('--cache-dir') + 1], two[two.index('--cache-dir') + 1])
        self.assertIn('--vfs-cache-mode', one)
        self.assertIn('--allow-other=false', one)

    def test_invalid_remote_or_cache_rejected(self):
        for remote, cache in [('other', 'a' * 32), ('iCloudDrive', '../old'), ('iCloudDrive', '')]:
            with self.assertRaises(ValueError):
                mount.mount_command(remote, cache, '/tmp/mount', '/tmp/cache', '/private/rclone')

    def test_mount_uses_shared_runtime_and_preserves_systemd_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = mock.Mock(stdout=b'[iCloudDrive]\nomarchy_cache_id = aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n')
            argv = ['mount-drive.py', 'iCloudDrive', str(root / 'config'), str(root / 'mount'), str(root / 'cache')]
            with mock.patch.object(mount, 'resolve_rclone', return_value='/private/rclone'), \
                 mock.patch.object(mount, 'protect_process') as protect, \
                 mock.patch.object(mount.sys, 'argv', argv), \
                 mock.patch.dict(mount.os.environ, {'NOTIFY_SOCKET': '/run/user/test/notify', 'RCLONE_DUMP': 'auth'}), \
                 mock.patch.object(mount.subprocess, 'run', return_value=result) as run, \
                 mock.patch.object(mount.os, 'execve') as execute:
                mount.main()
            protect.assert_called_once_with()
            self.assertEqual(run.call_args.args[0][0], '/private/rclone')
            self.assertEqual(execute.call_args.args[0], '/private/rclone')
            env = execute.call_args.args[2]
            self.assertEqual(env['NOTIFY_SOCKET'], '/run/user/test/notify')
            self.assertNotIn('RCLONE_DUMP', env)
