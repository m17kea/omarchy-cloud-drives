#!/usr/bin/env python3
"""Private NDJSON bridge for iCloud sign-in; no credentials go through argv.

Input: begin(email, password), answer(answer), cancel. Output: working,
challenge(kind, title, message, password), connected, error(code, message).
One process handles one sign-in. A connected event means credentials were
saved, not that the drive is mounted. The UI starts the mount afterwards.

Rclone's iCloud config/create always starts fresh authentication, including
when given a trust token. Therefore we authenticate in an encrypted staging
copy of the provider's config, then atomically promote the ciphertext. No
plaintext config is written. Failed or cancelled sign-in keeps the live config.
"""

import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid


REMOTE = "iCloudDrive"
UNIT = "omarchy-cloud-drive@iCloudDrive.service"
PASSWORD_COMMAND = "secret-tool lookup service omarchy-cloud-drives key config-password"
MAX_INPUT = 16 * 1024
MAX_RESPONSE = 2 * 1024 * 1024
REQUEST_TIMEOUT = 50
START_TIMEOUT = 10
SESSION_TIMEOUT = 15 * 60
ENCRYPTED_MARKER = b"RCLONE_ENCRYPT_V0:"
COMMIT_SIGNALS = (signal.SIGTERM, signal.SIGINT)

MESSAGES = {
    "cancelled": "Sign-in cancelled. Your saved account has not changed.",
    "busy": "Another iCloud sign-in is already open. Finish or close it first.",
    "setup_required": "Prepare Cloud Drives first, then try signing in again.",
    "keyring": "Unlock your login keyring, then try signing in again.",
    "network": "Apple could not be reached. Check your connection and try again.",
    "timeout": "Apple took too long to respond. Try signing in again.",
    "credentials": "Apple did not accept the sign-in. Use your Apple Account password, not an app-specific password.",
    "verification": "Apple did not accept that verification code. Start again to request a fresh code.",
    "approval": "Enable Access iCloud Data on the Web and approve access on a trusted Apple device. Advanced Data Protection can stay on.",
    "rate_limited": "Apple is limiting sign-in attempts. Wait a little before trying again.",
    "protocol": "This version of rclone returned an unsupported sign-in step. Update Cloud Drives and rclone, then retry.",
    "input": "Enter a valid Apple Account email and password to continue.",
    "response_limit": "The connection check returned more data than expected. Your saved account has not changed.",
    "config_changed": "Your saved connection changed during sign-in. Try again so the latest settings are preserved.",
    "mount_busy": "The existing iCloud mount could not be stopped cleanly. Close files using it, then try again.",
    "storage": "The secure connection could not be saved. Check available disk space and retry.",
    "unknown": "iCloud sign-in could not finish. Your saved account has not changed. Try again.",
}


class FlowError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Cancelled(FlowError):
    def __init__(self):
        super().__init__("cancelled")


def classify_error(raw):
    """Classify privately; never return upstream text or echo a response body."""
    value = str(raw).lower()
    if any(part in value for part in ("429", "too many", "rate limit")):
        return "rate_limited"
    if any(part in value for part in ("pcs", "advanced data", "requestpcs", "access_denied")):
        return "approval"
    if any(part in value for part in ("verification code", "2fa code", "security code", "sms code")):
        return "verification"
    if any(part in value for part in ("password command", "decrypt configuration", "config password", "keyring")):
        return "keyring"
    if any(part in value for part in ("401", "invalid password", "invalid credentials", "authentication failed", "unauthorized")):
        return "credentials"
    if any(part in value for part in ("timeout", "deadline exceeded", "timed out")):
        return "timeout"
    if any(part in value for part in ("no such host", "network", "connection refused", "connection reset", "dial tcp")):
        return "network"
    return "unknown"


def clean_environment():
    # Ambient rclone options can otherwise select another config, export
    # secrets as remotes, enable debug dumps, or start extra RC listeners.
    return {key: value for key, value in os.environ.items() if not key.startswith("RCLONE_")}


def read_ciphertext(path):
    """Read a user-owned private regular file without following a final symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise FlowError("setup_required")
            data = source.read(MAX_RESPONSE + 1)
    except OSError as exc:
        raise FlowError("setup_required") from exc
    if len(data) > MAX_RESPONSE or ENCRYPTED_MARKER not in data[:1024]:
        raise FlowError("setup_required")
    return data


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class RCServer:
    def __init__(self, config, runtime, cancelled):
        self.config = config
        self.socket = runtime / "rc.sock"
        self.cancelled = cancelled
        self.process = None
        self.connection = None

    def start(self):
        try:
            self.process = subprocess.Popen(
                ["rclone", "rcd", "--config", str(self.config),
                 "--password-command", PASSWORD_COMMAND, "--ask-password=false",
                 "--rc-addr", "unix://" + str(self.socket), "--rc-no-auth",
                 "--rc-serve=false", "--rc-server-read-timeout", "60s",
                 "--rc-server-write-timeout", "60s", "--contimeout", "10s",
                 "--timeout", "40s", "--retries", "1", "--low-level-retries", "1",
                 "--log-level", "ERROR", "--log-file", os.devnull],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=clean_environment(), umask=0o077,
            )
        except OSError as exc:
            raise FlowError("setup_required") from exc
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if self.cancelled.is_set():
                raise Cancelled()
            if self.process.poll() is not None:
                raise FlowError("keyring")
            if self.socket.exists():
                return
            self.cancelled.wait(0.05)
        raise FlowError("timeout")

    def call(self, endpoint, parameters):
        if self.cancelled.is_set():
            raise Cancelled()
        connection = UnixConnection(self.socket, REQUEST_TIMEOUT)
        self.connection = connection
        try:
            body = json.dumps(parameters, ensure_ascii=True).encode("utf-8")
            connection.request("POST", "/" + endpoint, body=body,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            content = response.read(MAX_RESPONSE + 1)
            if len(content) > MAX_RESPONSE:
                raise FlowError("response_limit")
            reply = json.loads(content)
            if not isinstance(reply, dict):
                raise FlowError("protocol")
            if response.status >= 400:
                raise FlowError(classify_error(reply.get("error", "")))
            return reply
        except (socket.timeout, TimeoutError) as exc:
            raise FlowError("timeout") from exc
        except (OSError, http.client.HTTPException) as exc:
            if self.cancelled.is_set():
                raise Cancelled() from exc
            raise FlowError("network") from exc
        except (ValueError, UnicodeError) as exc:
            raise FlowError("protocol") from exc
        finally:
            self.connection = None
            connection.close()

    def abort(self):
        # Called by the stdin reader on cancel/EOF, including while an Apple
        # request is pending. This wakes the blocking HTTP read promptly.
        connection = self.connection
        if connection and connection.sock:
            try:
                connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        process = self.process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def stop(self):
        self.abort()
        if self.process:
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
            self.process = None


class Onboarding:
    def __init__(self, emit, cancelled=None, config=None, runtime_root=None, rc_factory=RCServer):
        self.emit = emit
        self.cancelled = cancelled or threading.Event()
        config_root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        self.config = config or config_root / "omarchy-cloud-drives" / "iCloudDrive.conf"
        self.runtime_root = runtime_root or Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
        self.rc_factory = rc_factory
        self.runtime = None
        self.rc = None
        self.lock = None
        self.original_hash = None
        self.state = ""
        self.option_name = ""
        self.phone_choices = []
        self.phone_message = ""
        self.approval_attempts = 0
        self.started = False
        self.connected = False
        self.closing = False
        self.resume_mount = False
        self.commit_gate = threading.Lock()

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def prepare(self):
        # The standard runtime directory is owned by this login and private.
        try:
            info = self.runtime_root.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise FlowError("setup_required")
            fd = os.open(self.runtime_root / "omarchy-icloud-onboarding.lock",
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            self.lock = os.fdopen(fd, "a+b")
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FlowError("busy") from exc
        except OSError as exc:
            raise FlowError("setup_required") from exc
        data = read_ciphertext(self.config)
        self.original_hash = hashlib.sha256(data).digest()
        self.runtime = Path(tempfile.mkdtemp(prefix="omarchy-icloud-", dir=self.runtime_root))
        staged = self.runtime / "iCloudDrive.conf"
        with staged.open("xb") as target:
            target.write(data)
        staged.chmod(0o600)
        self.rc = self.rc_factory(staged, self.runtime, self.cancelled)
        self.rc.start()

    def begin(self, email, password):
        if self.started:
            raise FlowError("protocol")
        if not isinstance(email, str) or not isinstance(password, str):
            raise FlowError("input")
        email = email.strip()
        if not re.fullmatch(r"[^\s@\x00-\x1f]{1,256}@[^\s@\x00-\x1f]{1,256}", email) or len(email) > 320:
            raise FlowError("input")
        if not password or len(password) > 1024 or any(c in password for c in "\x00\r\n"):
            raise FlowError("input")
        self.started = True
        self.emit(phase="working", message="Connecting securely to Apple…")
        self.prepare()
        self.check_cancelled()
        previous = self.rc.call("config/get", {"name": REMOTE})
        cache_id = previous.get("omarchy_cache_id", "")
        same_account = str(previous.get("apple_id", "")).strip().lower() == email.lower()
        if not same_account or not isinstance(cache_id, str) or not re.fullmatch(r"[a-f0-9]{32}", cache_id):
            cache_id = uuid.uuid4().hex
        previous.clear()
        reply = self.rc.call("config/create", {
            "name": REMOTE, "type": "iclouddrive",
            "parameters": {"service": "drive", "apple_id": email, "password": password,
                           "omarchy_cache_id": cache_id},
            "opt": {"obscure": True, "nonInteractive": True},
        })
        password = None
        self.handle_reply(reply)

    def handle_reply(self, reply):
        self.check_cancelled()
        if reply.get("Error"):
            raise FlowError(classify_error(reply["Error"]))
        self.state = reply.get("State") or ""
        if not isinstance(self.state, str) or len(self.state) > 4096:
            raise FlowError("protocol")
        if not self.state:
            self.verify_and_commit()
            return
        option = reply.get("Option")
        if not isinstance(option, dict):
            raise FlowError("protocol")
        self.option_name = option.get("Name", "")
        self.phone_choices = []
        if self.option_name == "config_2fa_phone":
            examples = option.get("Examples", [])
            if not isinstance(examples, list) or not 1 <= len(examples) <= 20:
                raise FlowError("protocol")
            labels = []
            for index, example in enumerate(examples, 1):
                value = example.get("Value", "") if isinstance(example, dict) else ""
                if not re.fullmatch(r"\d+_(?:sms|voice)", value):
                    raise FlowError("protocol")
                self.phone_choices.append(value)
                # Display only a bounded numeric suffix, never upstream help
                # verbatim. Masked phone numbers are useful for choosing among
                # several trusted numbers without disclosing the full number.
                suffix = re.search(r"([0-9]{2,4})[^0-9]*$", str(example.get("Help", ""))[-80:])
                labels.append(f"{index} · ending {suffix.group(1)}" if suffix else f"{index} · Trusted phone {index}")
            self.phone_message = "Enter the number of the phone to use:\n" + "\n".join(labels)
            self.emit(phase="challenge", kind="answer", title="Choose a trusted number",
                      message=self.phone_message, password=False)
        elif self.option_name in ("config_2fa", "config_2fa_sms"):
            text = "Enter the six-digit code from your trusted Apple device. You can enter sms to request a text message instead."
            if self.option_name == "config_2fa_sms":
                text = "Enter the six-digit verification code Apple sent to your trusted phone number."
            self.emit(phase="challenge", kind="code", title="Verify it’s you", message=text,
                      password=False, smsAvailable=self.option_name == "config_2fa")
        else:
            # Unknown backend text may include account data or change meaning
            # between versions. Do not guess or display it as trusted UI copy.
            raise FlowError("protocol")

    def answer(self, answer):
        self.check_cancelled()
        if not isinstance(answer, str) or len(answer) > 1024 or any(c in answer for c in "\x00\r\n"):
            raise FlowError("input")
        answer = answer.strip()
        if self.option_name == "approval":
            self.verify_and_commit()
            return
        if not self.state:
            raise FlowError("protocol")
        if self.phone_choices:
            if not answer.isascii() or not answer.isdigit() or not 1 <= int(answer) <= len(self.phone_choices):
                self.emit(phase="challenge", kind="answer", title="Choose a trusted number",
                          message=self.phone_message, password=False)
                return
            answer = self.phone_choices[int(answer) - 1]
        elif not re.fullmatch(r"[0-9]{6}", answer) and not (self.option_name == "config_2fa" and answer.lower() == "sms"):
            # Keep the current challenge for a simple entry typo.
            message = "Enter the six-digit verification code Apple sent to your phone."
            if self.option_name == "config_2fa":
                message = "Enter six digits from Apple, or enter sms to request a text message."
            self.emit(phase="challenge", kind="code", title="Check the code", message=message,
                      password=False, smsAvailable=self.option_name == "config_2fa")
            return
        self.emit(phase="working", message="Verifying with Apple. Approve any access request on your trusted device…")
        self.handle_reply(self.rc.call("config/update", {
            "name": REMOTE, "parameters": {},
            "opt": {"continue": True, "nonInteractive": True, "state": self.state, "result": answer},
        }))

    def service(self, action, timeout=45):
        try:
            return subprocess.run(["systemctl", "--user", action, UNIT],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=timeout, check=False).returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FlowError("mount_busy") from exc

    def verify_and_commit(self):
        self.check_cancelled()
        self.emit(phase="working", message="Checking iCloud Drive. Approve web access on your trusted device if asked…")
        try:
            # Directory metadata only: never create, upload, delete or move.
            self.rc.call("operations/list", {"fs": REMOTE + ":", "remote": "",
                         "opt": {"dirsOnly": True, "recurse": False, "noModTime": True, "noMimeType": True}})
        except FlowError as exc:
            if exc.code == "approval" and self.approval_attempts < 3:
                self.approval_attempts += 1
                self.option_name = "approval"
                self.emit(phase="challenge", kind="approval", title="Approve iCloud access",
                          message=MESSAGES["approval"], password=False)
                return
            raise
        self.check_cancelled()
        self.emit(phase="working", message="Saving your secure connection…")
        # A running rclone may save refreshed cookies. Stop it before replacing
        # credentials so it cannot later write its old session over the new one.
        self.resume_mount = self.service("is-active", timeout=5) == 0
        if self.service("stop") != 0 or self.service("is-active", timeout=5) == 0:
            raise FlowError("mount_busy")
        self.check_cancelled()
        self.rc.stop()
        current = read_ciphertext(self.config)
        if hashlib.sha256(current).digest() != self.original_hash:
            raise FlowError("config_changed")
        staged = read_ciphertext(self.runtime / "iCloudDrive.conf")
        self.check_cancelled()
        self.promote(staged)
        self.emit(phase="connected", message="Your iCloud connection is saved. Opening your drive…")

    def promote(self, ciphertext):
        # The rename stays on the destination filesystem. Only encrypted bytes
        # touch persistent storage, and the existing file survives write failure.
        path = None
        try:
            fd, name = tempfile.mkstemp(prefix=".icloud-connect-", dir=self.config.parent)
            path = Path(name)
            with os.fdopen(fd, "wb") as target:
                target.write(ciphertext)
                target.flush()
                os.fsync(target.fileno())
            # Cancel can arrive from the reader thread or a process signal.
            # Block signals on the main thread, and gate the final cancellation
            # check + rename + committed state against the reader. The reader
            # inherits a blocked signal mask (see run), so a deferred signal is
            # handled only after these flags are committed and the gate released.
            previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, COMMIT_SIGNALS)
            try:
                with self.commit_gate:
                    self.check_cancelled()
                    os.replace(path, self.config)
                    path = None
                    self.connected = True
                    self.resume_mount = False
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        except OSError as exc:
            raise FlowError("storage") from exc
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    def cancel(self):
        with self.commit_gate:
            if self.connected:
                # Credentials have already been committed. Do not report that
                # an after-the-fact cancel preserved the previous account.
                return False
            self.cancelled.set()
        if self.rc:
            self.rc.abort()
        return True

    def close(self):
        self.closing = True
        try:
            if self.rc:
                self.rc.stop()
        finally:
            try:
                if self.resume_mount and not self.connected:
                    self.resume_mount = False
                    try:
                        self.service("start", timeout=10)
                    except FlowError:
                        pass
            finally:
                try:
                    if self.runtime:
                        shutil.rmtree(self.runtime)
                        self.runtime = None
                finally:
                    if self.lock:
                        self.lock.close()
                        self.lock = None


def emit_event(**event):
    # All event fields are fixed UI copy; raw upstream data never reaches here.
    print(json.dumps(event, ensure_ascii=True), flush=True)


def run(input_stream=None, emit=emit_event, flow_factory=Onboarding):
    input_stream = input_stream or sys.stdin.buffer
    messages = queue.Queue(maxsize=8)
    flow = flow_factory(emit)

    def read_input():
        while True:
            try:
                line = input_stream.readline(MAX_INPUT + 1)
                if not line:
                    flow.cancel()
                    return
                if len(line) > MAX_INPUT:
                    raise ValueError()
                command = json.loads(line)
                if not isinstance(command, dict):
                    raise ValueError()
                if command.get("op") == "cancel":
                    flow.cancel()
                    return
                messages.put_nowait(command)
            except (ValueError, UnicodeError, queue.Full, OSError):
                flow.cancel()
                return

    def signal_cancel(_signum, _frame):
        # A second close/terminate request must not interrupt bounded cleanup,
        # including restoring a previously running mount after a failed commit.
        if flow.closing:
            return
        if flow.cancel():
            raise Cancelled()

    previous = {}
    if threading.current_thread() is threading.main_thread():
        for number in COMMIT_SIGNALS:
            previous[number] = signal.signal(number, signal_cancel)
    # Only the main thread should receive the handled process signals. This
    # makes pthread_sigmask effective during the tiny atomic commit window.
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, COMMIT_SIGNALS)
    try:
        threading.Thread(target=read_input, name="icloud-input", daemon=True).start()
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    deadline = time.monotonic() + SESSION_TIMEOUT
    try:
        while not flow.connected:
            flow.check_cancelled()
            if time.monotonic() > deadline:
                raise FlowError("timeout")
            try:
                command = messages.get(timeout=0.1)
            except queue.Empty:
                continue
            operation = command.get("op")
            if operation == "begin":
                flow.begin(command.pop("email", None), command.pop("password", None))
            elif operation == "answer":
                flow.answer(command.pop("answer", None))
            else:
                raise FlowError("protocol")
            command.clear()
        return 0
    except FlowError as exc:
        code = "cancelled" if flow.cancelled.is_set() else exc.code
        emit(phase="error", code=code, message=MESSAGES.get(code, MESSAGES["unknown"]))
        return 130 if code == "cancelled" else 1
    except (BrokenPipeError, KeyboardInterrupt):
        return 130
    except Exception:
        # Tracebacks from HTTP/JSON code can embed request bodies or account
        # data. Errors are intentionally mapped rather than logged.
        emit(phase="error", code="unknown", message=MESSAGES["unknown"])
        return 1
    finally:
        try:
            flow.close()
        except Exception:
            pass
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(run())
