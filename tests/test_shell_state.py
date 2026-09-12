"""Exercise the panel's state boundary without a desktop, keyring or account."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'bin' / 'omarchy-cloud-drives'


class StateContract(unittest.TestCase):
    def test_old_distribution_package_requests_preparation_not_manual_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commands = root / 'bin'
            commands.mkdir()
            for name, content in {
                'rclone': "#!/bin/sh\necho 'rclone v1.75.0'\n",
                'secret-tool': '#!/bin/sh\nexit 1\n',
                'systemctl': '#!/bin/sh\nexit 3\n',
                'mountpoint': '#!/bin/sh\nexit 1\n',
            }.items():
                executable = commands / name
                executable.write_text(content)
                executable.chmod(0o700)
            env = dict(os.environ, XDG_CONFIG_HOME=str(root / 'config'),
                       XDG_DATA_HOME=str(root / 'data'),
                       PATH=str(commands) + os.pathsep + os.environ['PATH'])
            result = subprocess.run(['bash', str(SCRIPT), 'state'], env=env,
                                    capture_output=True, text=True, timeout=10, check=True)
            state = json.loads(result.stdout)
            self.assertFalse(state['ready'])
            self.assertIn('Prepare this computer', state['error'])
            self.assertFalse((root / 'data').exists())

    def test_prepared_configs_are_recognized_and_ambient_secrets_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commands = root / 'bin'
            commands.mkdir()
            config = root / 'config'
            plugin_config = config / 'omarchy-cloud-drives'
            plugin_config.mkdir(parents=True)
            units = config / 'systemd/user'
            units.mkdir(parents=True)
            (units / 'omarchy-cloud-drive@.service').touch()
            for remote in ('GoogleDrive', 'OneDrive', 'iCloudDrive'):
                (plugin_config / (remote + '.conf')).write_text(
                    '# Encrypted rclone configuration File\n\nRCLONE_ENCRYPT_V0:\nfixture\n')
            fake = {
                'secret-tool': '#!/bin/sh\nexit 0\n',
                'systemctl': '#!/bin/sh\nexit 3\n',
                'mountpoint': '#!/bin/sh\nexit 1\n',
                'rclone': '''#!/bin/sh
test -z "$RCLONE_CONFIG_PASS" || exit 91
test -z "$RCLONE_DUMP" || exit 92
case "$1" in
  version) echo 'rclone v1.75.1';;
  listremotes) basename "$RCLONE_CONFIG" .conf | sed 's/$/:/';;
  *) exit 93;;
esac
''',
            }
            for name, content in fake.items():
                executable = commands / name
                executable.write_text(content)
                executable.chmod(0o700)
            env = dict(os.environ, XDG_CONFIG_HOME=str(config), XDG_DATA_HOME=str(root / 'data'),
                       PATH=str(commands) + os.pathsep + os.environ['PATH'],
                       RCLONE_CONFIG_PASS='unrelated-secret', RCLONE_DUMP='auth')
            result = subprocess.run(['bash', str(SCRIPT), 'state'], env=env,
                                    capture_output=True, text=True, timeout=10, check=True)
            state = json.loads(result.stdout)
            self.assertTrue(state['ready'])
            self.assertTrue(all(p['configured'] for p in state['providers']))
            self.assertFalse(any(p['mounted'] for p in state['providers']))
            self.assertNotIn('unrelated-secret', result.stdout + result.stderr)
