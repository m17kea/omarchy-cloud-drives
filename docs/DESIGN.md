# Design: make connection a complete experience

The original native panel, rclone backend, login keyring and systemd services
are useful foundations. The opportunity is the path from first connection to
daily use and recovery.

Account and verification forms live in a separate, short-lived Quickshell
process, not inside the long-lived desktop shell. A detached supervisor owns
that window and its helpers, including cancellation and a bounded lifetime;
reloading the bar cannot kill the supervisor and abandon credential handling.
Only package installation needs Omarchy's setup terminal. Each screen has one
clear primary action, cancellation, deliberate keyboard focus, and a short
explanation. Fonts, colors, spacing and controls come from the packaged Omarchy
UI kit, without loading other shell plugins into the sign-in process.

The shared bar is credential-free. Secret-bearing processes inherit a zero
hard core limit and zero memory-dump filter, with dumpability disabled inside
Python helpers. Child environments are allowlisted before interpreters start.
The old Bash/curl Apple-password path is removed. These are exposure-reduction
measures, not a sandbox from other programs running as the same user. Password
retention, keyring trust and outstanding upstream review are explicit in the
[security model](SECURITY.md).

Preparation must resolve dependencies, not strand users at a version error.
When the distribution supplies an older rclone, install a checksum-pinned
official release privately. Never replace package-managed binaries or require
a whole-system upgrade just to connect a drive. A shared read-only resolver
keeps setup, authentication and systemd mounts on the same compatible runtime.

Reauthentication must preserve the current account until a replacement works.
Stage an encrypted config; authenticate; verify read access; stop the old mount;
then commit the new credentials. Authentication success is separate from mount
readiness. Do not label a filesystem "synced" merely because it is mounted.

This iteration uses a fixed folder per provider. Folder selection, selective
offline availability, upload-queue visibility, native Google/Microsoft sign-in,
and migration are later milestones. The fork keeps the upstream ID and is not
a separate marketplace release. Contribute upstream when the work fits its roadmap.
