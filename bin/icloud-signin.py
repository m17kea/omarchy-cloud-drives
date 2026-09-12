#!/usr/bin/python3 -Es
"""Supervise a short-lived themed sign-in process, outside the desktop shell.

Only code and packaged UI imports are staged in the private runtime directory.
Credentials never pass through this launcher or the bar's IPC. This is process
separation, not a sandbox against a malicious program running as the same user.
"""
import fcntl
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

from cloud_drives_security import SecurityError, protect_process, safe_environment

PLUGIN_DIR = Path(__file__).resolve().parents[1]
UI_KIT = Path("/usr/share/omarchy/shell")
SESSION_SECONDS = 15 * 60
CLEANUP_SECONDS = 25
FAILURE_MESSAGE = "The protected iCloud sign-in window could not open. Run Prepare or update Cloud Drives."
ALREADY_OPEN_MESSAGE = "An iCloud sign-in window is already open."


def private_runtime_root():
    root = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    if not root.is_absolute():
        raise SecurityError("The login runtime directory must be absolute.")
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SecurityError("The login runtime directory must be private.")
    return root


def prepare_ui(destination, plugin=PLUGIN_DIR, kit=UI_KIT):
    """Only launcher-selected source files enter the isolated QML root."""
    for name, source in (("shell.qml", plugin / "signin/shell.qml"),
                         ("ICloudSetup.qml", plugin / "ICloudSetup.qml"),
                         ("ProcessEnvironment.js", plugin / "ProcessEnvironment.js")):
        with source.open("rb") as incoming, (destination / name).open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        (destination / name).chmod(0o600)
    # No user shell/plugin tree is loaded. Only the packaged shared UI kit.
    for name in ("Ui", "Commons"):
        source = kit / name
        if not (source / "qmldir").is_file():
            raise SecurityError("The Omarchy UI kit is unavailable.")
        (destination / name).symlink_to(source, target_is_directory=True)


def window_environment():
    env = safe_environment(gui=True)
    env.update(CLOUD_DRIVES_PLUGIN_DIR=str(PLUGIN_DIR),
               QT_QPA_PLATFORM="wayland", QT_QPA_PLATFORMTHEME="",
               QT_QUICK_CONTROLS_STYLE="Basic", QML_DISABLE_DISK_CACHE="1",
               QT_LOGGING_RULES="*.debug=false;*.info=false")
    return env


def supervise(command, env):
    stopped = False
    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    child = None
    try:
        child = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        deadline = time.monotonic() + SESSION_SECONDS
        while not stopped and time.monotonic() < deadline:
            try:
                return child.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                continue
        return 130
    finally:
        if child is not None:
            # Also remove orphaned helpers if the UI exits unexpectedly. Each
            # helper has its own cancellation/cleanup handler; allow it time.
            deadline = time.monotonic() + CLEANUP_SECONDS
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            # The group may outlive its leader. One shared deadline gives a
            # helper enough time for RC shutdown and restoring the old mount,
            # even when the UI has already exited. Reap the leader as it exits
            # so its zombie cannot keep an otherwise empty group alive.
            group_alive = True
            while True:
                child.poll()
                try:
                    os.killpg(child.pid, 0)
                except ProcessLookupError:
                    group_alive = False
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.05, remaining))
            if group_alive:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            child.wait(timeout=2)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main():
    if len(sys.argv) != 1:
        raise SecurityError("The sign-in launcher does not accept account data or commands.")
    protect_process()
    os.umask(0o077)
    root = private_runtime_root()
    fd = os.open(root / "omarchy-icloud-window.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "a+b") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SecurityError("The sign-in lock is not private.")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(ALREADY_OPEN_MESSAGE, file=sys.stderr)
            return 75
        with tempfile.TemporaryDirectory(prefix="omarchy-icloud-window-", dir=root) as directory:
            config = Path(directory)
            prepare_ui(config)
            return supervise(["/usr/bin/qs", "--no-duplicate", "--path", str(config / "shell.qml")], window_environment())


def notify_failure(status):
    """Best-effort fixed-text feedback for a detached, already-started launcher.

    A missing supervisor/interpreter cannot report its own startup failure.
    Never include account input, paths or exception details in notifications.
    """
    message = ALREADY_OPEN_MESSAGE if status == 75 else FAILURE_MESSAGE
    try:
        subprocess.run(["/usr/bin/notify-send", "--app-name", "Cloud Drives",
                        "Cloud Drives", message], env=safe_environment(gui=True),
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=3, check=False)
    except Exception:
        # Missing notifications, an unavailable session bus or a timeout must
        # not obscure the original launch result or expose raw diagnostics.
        pass


def cli():
    try:
        status = main()
    except (SecurityError, OSError, subprocess.SubprocessError):
        print(FAILURE_MESSAGE, file=sys.stderr)
        status = 1
    if status not in (0, 130):
        notify_failure(status)
    return status


if __name__ == "__main__":
    raise SystemExit(cli())
