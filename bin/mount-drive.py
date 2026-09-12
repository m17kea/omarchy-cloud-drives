#!/usr/bin/env python3
"""Run one private mount with an account-specific cache and clean rclone options."""
import configparser
import os
from pathlib import Path
import re
import subprocess
import sys

from cloud_drives_runtime import RuntimeUnavailable, resolve_rclone
REMOTES = {"iCloudDrive", "GoogleDrive", "OneDrive"}
PASSWORD_COMMAND = "secret-tool lookup service omarchy-cloud-drives key config-password"


def mount_command(remote, cache_id, mount_root, cache_root, binary):
    if remote not in REMOTES or not re.fullmatch(r"[a-f0-9]{32}", cache_id):
        raise ValueError("Reconnect this account to initialize its private cache.")
    return [binary, "mount", remote + ":", str(Path(mount_root) / remote),
            "--cache-dir", str(Path(cache_root) / cache_id),
            "--vfs-cache-mode", "full", "--vfs-cache-max-size", "4G",
            "--vfs-cache-max-age", "72h", "--dir-cache-time", "30s",
            "--poll-interval", "15s", "--umask", "077", "--allow-other=false",
            "--log-level", "NOTICE", "--ask-password=false"]


def main():
    if len(sys.argv) != 5 or sys.argv[1] not in REMOTES:
        raise ValueError("Invalid mount request.")
    remote, config_root, mount_root, cache_root = sys.argv[1:]
    binary = resolve_rclone()
    env = {k: v for k, v in os.environ.items() if not k.startswith("RCLONE_")}
    env.update(RCLONE_CONFIG=str(Path(config_root) / (remote + ".conf")),
               RCLONE_PASSWORD_COMMAND=PASSWORD_COMMAND)
    # This output includes credentials: keep it in memory and never log it.
    result = subprocess.run([binary, "config", "show", remote,
                             "--ask-password=false"], env=env, capture_output=True,
                            timeout=15, check=True)
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(result.stdout.decode())
    cache_id = config.get(remote, "omarchy_cache_id", fallback="")
    command = mount_command(remote, cache_id, mount_root, cache_root, binary)
    mount_dir = Path(mount_root) / remote
    cache_dir = Path(cache_root) / cache_id
    if mount_dir.is_symlink() or cache_dir.is_symlink():
        raise ValueError("The drive and cache paths must be ordinary directories.")
    mount_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.execve(command[0], command, env)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeUnavailable, subprocess.SubprocessError, configparser.Error):
        # Do not include exception text; subprocess arguments/config may contain secrets.
        print("Cloud drive could not start. Unlock the keyring or reconnect the account.", file=sys.stderr)
        sys.exit(1)
