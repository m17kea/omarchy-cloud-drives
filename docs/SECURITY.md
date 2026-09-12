# Credential security and remaining risks

This is a development client, not a security-certified or compromise-proof
integration. The hardening below addresses specific exposure paths found in
an agent-assisted source review. An independent professional security review
and real-account validation remain release gates.

## What you are trusting

The iCloud backend requires your primary Apple password and two-factor
authentication, not an app-specific password or a Drive-only OAuth grant.
Authentication uses SRP over HTTPS, but the password must still exist in
client memory. [Upstream authentication documentation](https://rclone.org/iclouddrive/#authentication).

rclone 1.75.1 retains a reversibly obscured password together with session
material inside the provider configuration. This plugin encrypts the entire
configuration, restricts it to mode 0600, creates new config directories with
mode 0700, and keeps a random encryption key in the login keyring. Preparation
does not repair permissions on pre-existing parent directories; keep those
private and trusted. Encryption is not a password-free design.
[Tagged password handling](https://github.com/rclone/rclone/blob/v1.75.1/backend/iclouddrive/icloud.go),
[session renewal](https://github.com/rclone/rclone/blob/v1.75.1/backend/iclouddrive/api/client.go).

The keyring lookup is not a per-application authorization boundary. Another
process with your unlocked user session may obtain the same key, access the
private auth socket, replace user-owned plugin files, or inspect other
accessible process state. Root or a compromised kernel can bypass these
protections. We therefore trust the operating system, desktop session, login
keyring, installed plugin and runtime code. The Secret Service specification
does not require application-specific access controls.
[Secret Service specification](https://specifications.freedesktop.org/secret-service/latest-single/).

## Implemented protections

- Credential input is hosted in a dedicated, short-lived Quickshell process.
  The shared bar holds no password/code fields and parses no authentication
  responses. Only packaged Omarchy UI modules are staged alongside this
  plugin's sign-in components; other bar plugins are not loaded into it.
- A detached supervisor owns the sign-in process group, serializes windows
  with a private lock, imposes a 15-minute lifetime, and allows bounded helper
  cleanup before terminating remaining descendants. Normal cancellation
  preserves the existing account. Hard kills, power loss and OS failures are
  not transactional guarantees.
- Credential-handling launchers and Python helpers fail closed unless core
  limits, memory-dump filtering and applicable dumpability checks succeed.
  The mount unit specifies `LimitCORE=0` and `CoredumpFilter=0x0`. These are
  per-process settings; the desktop shell and global crash policy are not
  changed.
- The zero hard core limit and zero mapping filter survive child execution.
  Linux resets `PR_SET_DUMPABLE` on an ordinary `exec`, so that control is
  reapplied inside Python helpers; it is **not** claimed to remain disabled
  inside unmodified Quickshell or rclone. Omarchy's systemd-coredump honors
  the inherited core-size limit. A custom core collector requires its own
  review. [systemd core handling](https://systemd.io/COREDUMP/),
  [Linux execution behavior](https://man7.org/linux/man-pages/man2/execve.2.html).
- Every QML child process starts with an explicit environment allowlist.
  Python launches use the system interpreter with environment/user-site
  loading disabled. Debug/dump settings, inherited rclone options, proxies,
  TLS-trust overrides and code-loading variables are not forwarded. Known
  loader/debug variables are also explicitly unset by systemd before the
  mount worker's interpreter starts, not merely in its later child processes.
  Required desktop/session paths remain trusted inputs. Custom proxy/CA environments
  are not supported by this policy.
- The old Bash/curl Apple-password implementation is removed. CLI iCloud
  connection delegates to the same isolated window. Shell tracing is disabled
  before any configuration handling; shell children inherit dump restrictions.
- Passwords and verification codes travel through stdin and a user-private
  Unix socket, not command arguments or environment variables. Raw auth output
  is suppressed and UI messages are curated. The mount service suppresses
  stdout/stderr and rclone file logging; this deliberately limits diagnostics.
- Input fields mask immediately and `clear()` removes text and pending input
  composition. This does not securely erase every Qt, Python or Go allocation.
- The private fallback runtime is SHA-256-pinned to the official 1.75.1
  executable and archive; downloads require the allowed official HTTPS hosts.
  System fallback selection uses fixed system directories and rejects
  non-root-owned or group/world-writable executables. Version checks do not
  authorize a user-PATH substitute. Hash/provenance checks do not establish
  that upstream code is free of vulnerabilities.
- Authentication is staged in a private temporary directory, verifies read
  access before replacing the encrypted provider config, and isolates caches
  by account identity. Existing configurations are retained on normal failure
  or cancellation.

## Still unresolved or outside this protection

- An independent review of the complete iCloud transport and dependency chain
  has not been completed. Tagged rclone download code forwards session headers
  to service-supplied URLs and follows a custom HTTP-330 redirect without an
  explicit scheme/host check at that point. Normal URLs come from Apple's
  authenticated service. This is a trust-boundary review item, **not evidence
  of a demonstrated remote credential theft**, and this fork does not patch
  that upstream behavior. [Tagged download implementation](https://github.com/rclone/rclone/blob/v1.75.1/backend/iclouddrive/api/drive.go).
- Same-user malware, malicious updates, root access, a compromised compositor
  or input method, screen capture and keylogging are not contained. The
  isolated window reduces shared-process exposure but is not an OS sandbox.
- Clipboard managers can retain pasted passwords. Avoid clipboard history for
  credentials. No attempt is made to erase unrelated clipboard history.
- Secret memory is not locked against swapping, and hibernation can persist
  memory. Use trusted system storage protection; this plugin does not configure
  disk/swap encryption or promise secure erasure.
- Cached cloud file contents are plaintext, restricted to your user. The cache
  is retained for upload recovery. A mounted drive is not proof that all writes
  reached Apple, and this is not a conflict-resolving offline sync engine.

## Testing and release policy

The automated checks use synthetic credentials, scratch configs and a local
rclone RC process. Optional offscreen QML checks exercise masking, input
clearing, cancellation and environment filtering. Kernel inheritance is tested
in disposable child processes without causing crashes. These checks are not
penetration testing or certification. See [validation](VALIDATION.md).

Do not broadly publish this as a secure primary-account client until the
independent review and live-account gates are satisfied. For security reports,
provide versions, reproduction steps and sanitized errors only; never attach
passwords, codes, decrypted configs, authentication dumps or crash memory.
