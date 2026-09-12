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

## Live preparation check (2026-09-12)

Tested on Omarchy 4.0.2, Linux x86-64 with packaged rclone 1.75.0:

- Reproduced the old preparation path installing a version it then rejected.
- Downloaded and installed the checksum-pinned private 1.75.1 runtime through
  the new preparation path; the package-managed 1.75.0 was unchanged.
- Reopened native preparation and observed `ready`, `encrypted` and `keyring`
  all true. All three provider configs were mode 0600.
- The generated user service passed `systemd-analyze --user verify`.
- The native iCloud sign-in panel opened through the live shell.

These checks do not establish successful Apple authentication or file sync.
The 48-test isolated suite passed, including the real local rclone RC contract.

## Credential hardening checks (2026-09-12)

After the preparation check above, the credential UI was moved out of the
shared bar into a separately supervised process. The legacy Bash/curl Apple
password flow was removed. The updated isolated suite passed 85 tests,
including the pinned official archive and real local rclone RC contract.

Additional checks cover:

- Hard/soft core limits of zero, a zero memory-dump filter, and disabled
  dumpability in disposable Python children; verification that the first two
  survive execution and that Linux resets dumpability on execution.
- Fixed-path runtime selection, rejected user-PATH substitutes, sanitized
  environments, fail-closed protection failures and suppressed child output.
- Mount unit settings remove known loader/debug variables before Python starts,
  in addition to the later child-environment allowlist.
- Detached sign-in ownership and a single 25-second descendant cleanup budget,
  including simulated slow helpers after the UI leader has already exited.
- Fixed, bounded failure notifications; no raw exception or account data.
- `bash tests/qml/run-smoke.sh`: actual offscreen Omarchy controls with a mock
  helper, checking masking, password/code clearing, cancellation, window close
  and removal of an inherited environment marker. This uses synthetic inputs,
  not Apple, the login keyring or a real account. The headless platform emits
  expected IPC/window-mask warnings; it is not a live compositor test.
- Bash syntax, Omarchy manifest validation and whitespace checks.

No credential-bearing crash was triggered and no existing crash memory was
read. Automated and agent-assisted review is not an independent security
audit. See [remaining risks](SECURITY.md).

## Live-account release gate

The following have **not** been completed in the automated development session:

- Independent security review, including upstream iCloud endpoint/redirect
  handling and the documented desktop/keyring trust assumptions.
- Broader clean-machine preparation and locked/unlocked keyring behavior
  (one successful live preparation is recorded above).
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
