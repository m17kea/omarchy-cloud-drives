#!/usr/bin/python3 -Es
"""Opt-in live test: create only a unique test directory; retain it for inspection.

This writes to the connected iCloud account. It never scans or modifies existing
documents. Each save is read back through a separate rclone process, bypassing
the mounted filesystem's cache. Run only after the user authorizes live testing.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from cloud_drives_runtime import resolve_rclone
from cloud_drives_security import protect_process, safe_environment

def emit(message):
    print(message, flush=True)


def save(path, data):
    with path.open("xb") as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())


def verify_remote(remote_path, expected, config, binary, deadline_seconds=90):
    env = safe_environment()
    command = [binary, "cat", "iCloudDrive:" + remote_path,
               "--config", str(config), "--password-command",
               "/usr/bin/secret-tool lookup service omarchy-cloud-drives key config-password",
               "--ask-password=false", "--retries", "1", "--low-level-retries", "1",
               "--contimeout", "5s", "--timeout", "10s"]
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(command, env=env, capture_output=True, timeout=15)
            if result.returncode == 0 and result.stdout == expected:
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(3)
    raise RuntimeError("The expected test content did not reach iCloud within the test deadline.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="explicitly permit creating test files")
    parser.add_argument("--mount", type=Path, required=True)
    args = parser.parse_args()
    if not args.run:
        parser.error("Pass --run only after authorizing writes to a new iCloud test directory.")
    protect_process()
    if not args.mount.is_mount():
        parser.error("The selected directory is not a mounted filesystem.")
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    config = config_root / "omarchy-cloud-drives/iCloudDrive.conf"
    if not config.is_file():
        parser.error("The plugin's iCloud configuration is missing.")
    binary = resolve_rclone()
    name = "Omarchy-iCloud-Test-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    folder = args.mount / name
    folder.mkdir()
    first = b"Omarchy iCloud live test: initial save.\n"
    updated = b"Omarchy iCloud live test: atomic replacement verified.\n"
    emit("Test folder: " + str(folder))
    save(folder / "roundtrip.txt", first)
    emit("Created roundtrip.txt; waiting for an independent read from iCloud...")
    verify_remote(name + "/roundtrip.txt", first, config, binary)
    emit("PASS: initial upload and independent readback")
    save(folder / ".save-in-progress", updated)
    (folder / ".save-in-progress").replace(folder / "roundtrip.txt")
    verify_remote(name + "/roundtrip.txt", updated, config, binary)
    emit("PASS: application-style atomic replacement and independent readback")
    (folder / "roundtrip.txt").rename(folder / "verified.txt")
    verify_remote(name + "/verified.txt", updated, config, binary)
    if (folder / "verified.txt").read_bytes() != updated:
        raise RuntimeError("The mounted file read does not match the uploaded test content.")
    emit("PASS: rename and final local/cloud readback")
    emit("SHA256: " + hashlib.sha256(updated).hexdigest())
    emit("Retained verified.txt in the test folder for inspection on an Apple device.")


if __name__ == "__main__":
    main()
