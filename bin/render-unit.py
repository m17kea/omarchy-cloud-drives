#!/usr/bin/env python3
"""Render systemd path arguments without shell evaluation or specifier injection."""
import pathlib
import sys


def systemd_string(value):
    if any(ord(ch) < 32 for ch in value):
        raise ValueError("Paths must not contain control characters")
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")


def render(template, config, mount, cache, worker, data_home):
    # Environment= does not expand $VAR, unlike ExecStart= arguments.
    template = template.replace("@DATA@", systemd_string(data_home))
    for marker, value in (("@CONFIG@", config), ("@MOUNT@", mount), ("@CACHE@", cache), ("@WORKER@", worker)):
        template = template.replace(marker, systemd_string(value).replace("$", "$$"))
    return template


if __name__ == "__main__":
    print(render(pathlib.Path(sys.argv[1]).read_text(), *sys.argv[2:]), end="")
