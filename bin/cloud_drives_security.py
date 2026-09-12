"""Process protections for code that handles Cloud Drives credentials.

Call protect_process() before reading credentials or decrypting configuration.
It changes only the calling process and its future children. It cannot protect
against code already running as the same user or a compromised login keyring.

Linux resets PR_SET_DUMPABLE during an ordinary exec, so that setting protects
the current process, not an arbitrary executable launched afterwards. The hard
RLIMIT_CORE of zero and zero coredump_filter survive fork/exec. systemd-coredump
uses the kernel's supplied core-size limit (%c) when deciding whether to process
a dump; the inherited filter also excludes memory mappings from a dump. Call
protect_process() again inside a child that handles secrets when possible.

Do not add no_new_privs here: rclone mounts require the setuid fusermount3 helper.
"""

import ctypes
import os
import resource
import sys


SECURITY_MESSAGE = "Cloud Drives could not protect this process. Close it and try again."
CORE_FILTER = "/proc/self/coredump_filter"
PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4

_BASE_ENVIRONMENT = frozenset({
    "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "USER", "LOGNAME",
    "LANG", "TZ", "NOTIFY_SOCKET", "TERM", "COLORTERM",
})
_GUI_ENVIRONMENT = frozenset({"WAYLAND_DISPLAY", "DISPLAY", "XDG_SESSION_TYPE",
                              "HYPRLAND_INSTANCE_SIGNATURE", "XDG_CURRENT_DESKTOP"})


class SecurityError(Exception):
    """Fixed, non-sensitive failure suitable for display without diagnostics."""


def safe_environment(source=None, gui=False):
    """Build a minimal child environment, without inherited debug/log controls.

    In particular, loader/Python injection variables, rclone options, Qt/QML/QS
    debugging settings, proxy settings and TLS trust overrides are excluded.
    A GUI caller may add explicitly chosen, fixed Qt settings to the result.
    """
    source = os.environ if source is None else source
    allowed = _BASE_ENVIRONMENT | (_GUI_ENVIRONMENT if gui else frozenset())
    environment = {
        key: value for key, value in source.items()
        if key in allowed or key.startswith("LC_")
    }
    environment["PATH"] = "/usr/bin:/bin"
    environment["OMARCHY_PATH"] = "/usr/share/omarchy"
    return environment


def _prctl(option, value=0):
    function = ctypes.CDLL(None, use_errno=True).prctl
    function.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                        ctypes.c_ulong, ctypes.c_ulong]
    function.restype = ctypes.c_int
    result = function(option, value, 0, 0, 0)
    if result < 0:
        raise OSError(ctypes.get_errno(), "Process protection call failed")
    return result


def protect_process():
    """Disable credential-bearing core dumps, or stop with SecurityError.

    The hard resource limit cannot be restored by an unprivileged caller. Run
    this in the actual helper/GUI process, never in a shared test runner. Set
    the proc filter before clearing dumpability, which can change proc-file
    ownership. Every setting is read back before returning successfully.
    """
    try:
        if sys.platform != "linux":
            raise RuntimeError("Linux process protection is required")
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if resource.getrlimit(resource.RLIMIT_CORE) != (0, 0):
            raise RuntimeError("Core limit was not applied")
        with open(CORE_FILTER, "r", encoding="ascii") as source:
            current_filter = int(source.read(64).strip(), 16)
        # After dumpability is cleared, proc files may be root-owned. Avoid
        # reopening the filter for writing when it is already protected, so
        # calling this function again remains safe and idempotent.
        if current_filter != 0:
            with open(CORE_FILTER, "w", encoding="ascii") as target:
                target.write("0\n")
        with open(CORE_FILTER, "r", encoding="ascii") as source:
            if int(source.read(64).strip(), 16) != 0:
                raise RuntimeError("Core filter was not applied")
        if _prctl(PR_SET_DUMPABLE, 0) != 0 or _prctl(PR_GET_DUMPABLE) != 0:
            raise RuntimeError("Process remains dumpable")
    except Exception:
        # Do not expose inherited environment values, paths or low-level
        # exception text; callers must not proceed after a protection failure.
        raise SecurityError(SECURITY_MESSAGE) from None
