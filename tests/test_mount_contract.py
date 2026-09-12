import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "bin" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


unit = load("render_unit", "render-unit.py")
mount = load("mount_drive", "mount-drive.py")


class MountContract(unittest.TestCase):
    def test_unit_paths_do_not_expand_specifiers_or_environment(self):
        template = 'ExecStart="@WORKER@" %i "@CONFIG@" "@MOUNT@" "@CACHE@"'
        value = '/tmp/space path/quote"/percent%/dollar$NAME/slash\\'
        rendered = unit.render(template, value, value, value, value)
        self.assertIn('" %i "', rendered)
        self.assertIn('percent%%/dollar$$NAME', rendered)
        self.assertIn('quote\\"', rendered)
        self.assertIn('slash\\\\', rendered)

    def test_control_characters_rejected(self):
        with self.assertRaises(ValueError):
            unit.render('@MOUNT@', '/tmp/config', '/tmp/bad\npath', '/tmp/cache', '/tmp/worker')

    def test_account_cache_isolation(self):
        one = mount.mount_command('iCloudDrive', 'a' * 32, '/tmp/mount', '/tmp/cache')
        two = mount.mount_command('iCloudDrive', 'b' * 32, '/tmp/mount', '/tmp/cache')
        self.assertNotEqual(one[one.index('--cache-dir') + 1], two[two.index('--cache-dir') + 1])
        self.assertIn('--vfs-cache-mode', one)
        self.assertIn('--allow-other=false', one)

    def test_invalid_remote_or_cache_rejected(self):
        for remote, cache in [('other', 'a' * 32), ('iCloudDrive', '../old'), ('iCloudDrive', '')]:
            with self.assertRaises(ValueError):
                mount.mount_command(remote, cache, '/tmp/mount', '/tmp/cache')
