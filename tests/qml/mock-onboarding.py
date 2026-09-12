"""Fake authentication only: never imports network or credential libraries."""
import json
import os
import sys

assert "CLOUD_DRIVES_TEST_MARKER" not in os.environ
assert os.environ.get("PATH") == "/usr/bin:/bin"
assert os.environ.get("OMARCHY_PATH") == "/usr/share/omarchy"

for line in sys.stdin:
    request = json.loads(line)
    if request["op"] == "cancel":
        break
    if request["op"] == "begin":
        if request["email"].startswith("cancel"):
            event = {"phase": "working", "message": "Mock working"}
        else:
            event = {"phase": "challenge", "kind": "code", "password": False,
                     "smsAvailable": False, "title": "Mock verification", "message": "Mock only"}
        print(json.dumps(event), flush=True)
    elif request["op"] == "answer":
        print(json.dumps({"phase": "connected", "message": "Mock connected"}), flush=True)
        break
