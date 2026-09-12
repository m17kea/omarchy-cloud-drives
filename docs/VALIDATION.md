# Validation

## Automated and isolated

- Authentication state transitions and required rclone continuation fields.
- Cancellation and failure preserve the previous account configuration.
- Secrets/raw authentication responses do not reach UI status output.
- Bounded authentication responses and network waits.
- systemd path escaping, Bash syntax, Omarchy manifest validation.
- Local rclone RC contract tests with scratch configuration and no Apple account.
- Runtime selection and checksum verification without a system-package override.
- Native welcome, account, verification and connected forms rendered offscreen
  using this machine's real Omarchy UI and theme; previews are in `previews/`.

## Live-account release gate

The following have **not** been completed in the automated development session:

- Clean-machine preparation and locked/unlocked keyring behavior.
- Apple login, rejected password/code, trusted-device and SMS flows.
- Advanced Data Protection approval, delayed approval and cancellation.
- Mount/open/create/edit/rename/readback from another Apple device.
- Session renewal while mounted, including a failed renewal.
- Network loss, queued writes, restart and cache recovery.
- Concurrent editing and application save-by-rename behavior.
- Complete keyboard flow, themes, scaling, small screens and shell reload.
- Removal after confirming all uploads reached the cloud.

Use a test folder and expendable files for mutation tests. This development
build is not represented as an end-to-end validated iCloud client.

## Opt-in file round trip

After signing in and mounting iCloud, run:

```sh
python3 tests/live_drive_probe.py --run --mount "$HOME/Cloud/iCloudDrive"
```

This creates a uniquely named `Omarchy-iCloud-Test-*` folder. It tests a save,
application-style atomic replacement, and rename, reading the expected bytes
directly from iCloud after each step through a separate rclone process. It never
scans or edits existing documents and retains `verified.txt` for inspection on
an Apple device. This does not test concurrent editing, offline recovery or
all possible application save patterns. Confirm the retained file on another
device separately; a passing script is not that cross-device confirmation.
