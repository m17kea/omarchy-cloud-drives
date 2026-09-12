#!/usr/bin/python3 -Es
"""Apply credential-process protections before exec; no input or secrets logged."""
import os
from pathlib import Path
import sys

from cloud_drives_security import SecurityError, protect_process, safe_environment


def main():
    if len(sys.argv) < 2 or not Path(sys.argv[1]).is_absolute():
        raise SecurityError("An absolute program path is required.")
    protect_process()
    # Dumpability is reset by exec. The hard core limit and zero memory filter
    # survive it; systemd-coredump on Omarchy honors the inherited zero limit.
    os.execve(sys.argv[1], sys.argv[1:], safe_environment(gui=True))


if __name__ == "__main__":
    try:
        main()
    except (SecurityError, OSError):
        print("Cloud Drives could not apply process protections. Nothing was started.", file=sys.stderr)
        raise SystemExit(1)
