"""Exercise the panel's state boundary without a desktop, keyring or account."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'bin' / 'omarchy-cloud-drives'


class StateContract(unittest.TestCase):
    def fixture(self, root, version, key_available=False):
        """Copy the real dispatcher, substituting only external test dependencies.

        The fixture launcher applies the real process protections. Its PATH is
        deliberately the fixture-only command directory, unlike production's
        fixed system PATH. No keyring, service or rclone command can fall back
        to the host: those names exist only as strict synthetic implementations.
        A nonexistent session-bus address is an additional keyring tripwire.
        """
        commands = root / 'commands'
        commands.mkdir()
        plugin_bin = root / 'plugin' / 'bin'
        plugin_bin.mkdir(parents=True)
        script = plugin_bin / SCRIPT.name
        shutil.copy2(SCRIPT, script)
        fixture_home = root / 'home'
        fixture_home.mkdir(mode=0o700)
        events = root / 'calls.jsonl'

        launcher = f'''import os, sys
sys.dont_write_bytecode = True
sys.path.insert(0, {str(ROOT / 'bin')!r})
from cloud_drives_security import protect_process, safe_environment
protect_process()
environment = safe_environment(gui=True)
# TEST ONLY: dependency substitution never appears in the production launcher.
environment['PATH'] = {str(commands)!r}
os.execve(sys.argv[1], sys.argv[1:], environment)
'''
        (plugin_bin / 'protected-exec.py').write_text(launcher)
        runtime = f'''import re, subprocess, sys
if sys.argv[1:] != ['path']:
    raise SystemExit(94)
candidate = {str(commands / 'rclone')!r}
result = subprocess.run([candidate, 'version'], capture_output=True, text=True, timeout=3)
match = re.fullmatch(r'rclone v([0-9]+)\\.([0-9]+)\\.([0-9]+)', result.stdout.strip())
if result.returncode != 0 or not match or tuple(map(int, match.groups())) < (1, 75, 1):
    raise SystemExit(1)
print(candidate)
'''
        (plugin_bin / 'cloud_drives_runtime.py').write_text(runtime)

        # Every sensitive dependency verifies that the dispatcher crossed the
        # real protected-exec boundary and inherited both core protections.
        probe = f'''#!/usr/bin/python3 -I
import json, os, resource, sys
from pathlib import Path
name = Path(sys.argv[0]).name
limits = resource.getrlimit(resource.RLIMIT_CORE)
with open('/proc/self/coredump_filter') as source:
    core_filter = int(source.read().strip(), 16)
with open({str(events)!r}, 'a') as target:
    target.write(json.dumps({{'name': name, 'limits': limits, 'filter': core_filter}}) + '\\n')
if limits != (0, 0) or core_filter != 0:
    raise SystemExit(95)
if os.environ.get('RCLONE_CONFIG_PASS') or os.environ.get('RCLONE_DUMP'):
    raise SystemExit(91)
arguments = sys.argv[1:]
'''
        fake = {
            'secret-tool': probe + f'''
if arguments != ['lookup', 'service', 'omarchy-cloud-drives', 'key', 'config-password']:
    raise SystemExit(94)
raise SystemExit({0 if key_available else 1})
''',
            'rclone': probe + f'''
if arguments == ['version']:
    print('rclone v{version}')
elif arguments == ['listremotes', '--ask-password=false']:
    config = Path(os.environ['RCLONE_CONFIG'])
    if config.parent != Path({str(root / 'config' / 'omarchy-cloud-drives')!r}):
        raise SystemExit(96)
    if config.stem not in ('GoogleDrive', 'OneDrive', 'iCloudDrive'):
        raise SystemExit(97)
    print(config.stem + ':')
else:
    raise SystemExit(93)
''',
            'systemctl': probe + '''
if len(arguments) != 4 or arguments[:3] not in (
        ['--user', 'is-active', '--quiet'], ['--user', 'is-failed', '--quiet']):
    raise SystemExit(94)
if arguments[3] not in ('omarchy-cloud-drive@GoogleDrive.service',
                        'omarchy-cloud-drive@OneDrive.service',
                        'omarchy-cloud-drive@iCloudDrive.service'):
    raise SystemExit(94)
raise SystemExit(3)
''',
            'mountpoint': probe + f'''
if len(arguments) != 2 or arguments[0] != '-q':
    raise SystemExit(94)
path = Path(arguments[1])
if path.parent != Path({str(fixture_home / 'Cloud')!r}):
    raise SystemExit(96)
raise SystemExit(1)
''',
        }
        for name, content in fake.items():
            executable = commands / name
            executable.write_text(content)
            executable.chmod(0o700)
        # Only stateless text tools are admitted. PATH has no fallback system
        # directory, so any unexpected external dependency fails closed.
        for name in ('dirname', 'timeout', 'jq', 'head', 'grep', 'sed', 'cut', 'python3'):
            executable = Path('/usr/bin') / name
            self.assertTrue(executable.is_file(), f'missing test dependency: {name}')
            (commands / name).symlink_to(executable)
        environment = {
            'HOME': str(fixture_home), 'PATH': str(commands), 'LANG': 'C.UTF-8',
            'XDG_CONFIG_HOME': str(root / 'config'), 'XDG_DATA_HOME': str(root / 'data'),
            'XDG_CACHE_HOME': str(root / 'cache'), 'XDG_RUNTIME_DIR': str(root / 'runtime'),
            'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + str(root / 'no-session-bus'),
        }
        return script, environment, events

    def run_state(self, script, environment, events):
        result = subprocess.run(['/bin/bash', '--noprofile', '--norc', str(script), 'state'],
                                env=environment, capture_output=True, text=True,
                                timeout=10, check=True)
        calls = [json.loads(line) for line in events.read_text().splitlines()]
        names = {call['name'] for call in calls}
        self.assertTrue({'rclone', 'secret-tool', 'systemctl', 'mountpoint'} <= names)
        self.assertTrue(all(call['limits'] == [0, 0] and call['filter'] == 0 for call in calls))
        self.assertNotIn('unrelated-secret', result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_old_distribution_package_requests_preparation_not_manual_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, environment, events = self.fixture(root, '1.75.0')
            state = self.run_state(script, environment, events)
            self.assertFalse(state['ready'])
            self.assertIn('Prepare this computer', state['error'])
            self.assertFalse((root / 'data').exists())

    def test_prepared_configs_are_recognized_and_ambient_secrets_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, environment, events = self.fixture(root, '1.75.1', key_available=True)
            config = root / 'config'
            plugin_config = config / 'omarchy-cloud-drives'
            plugin_config.mkdir(parents=True)
            units = config / 'systemd/user'
            units.mkdir(parents=True)
            (units / 'omarchy-cloud-drive@.service').touch()
            for remote in ('GoogleDrive', 'OneDrive', 'iCloudDrive'):
                (plugin_config / (remote + '.conf')).write_text(
                    '# Encrypted rclone configuration File\n\nRCLONE_ENCRYPT_V0:\nfixture\n')
            environment.update(RCLONE_CONFIG_PASS='unrelated-secret', RCLONE_DUMP='auth')
            state = self.run_state(script, environment, events)
            self.assertTrue(state['ready'])
            self.assertTrue(all(p['configured'] for p in state['providers']))
            self.assertFalse(any(p['mounted'] for p in state['providers']))
