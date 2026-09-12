#!/usr/bin/env python3
"""Resolve or explicitly prepare the plugin's supported rclone runtime.

Resolution is read-only and never downloads. Preparation uses the installed
system version when suitable, or a pinned, checksum-verified official release.
"""

import fcntl
import hashlib
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from cloud_drives_security import safe_environment


MINIMUM_VERSION = (1, 75, 1)
VERSION = "1.75.1"
DIRECTORY = "rclone-v1.75.1-linux-amd64"
ASSET_URL = "https://github.com/rclone/rclone/releases/download/v1.75.1/rclone-v1.75.1-linux-amd64.zip"
ASSET_SHA256 = "982b5aa772841168f8e380f139e9e787b2a105403e32b94da8676a0e1c0a13ab"
# This exact member is from the archive authenticated by ASSET_SHA256. Verify
# it before reusing a private install, without first executing a damaged file.
BINARY_SHA256 = "f66d8c1d552ad90296a11bc8b46d56a7fa5da1a7fa05e7ca522d95df92c4a4c0"
MAX_DOWNLOAD = 64 * 1024 * 1024
MAX_BINARY = 128 * 1024 * 1024
MAX_README = 8 * 1024 * 1024
DOWNLOAD_TIMEOUT = 120
CHUNK = 1024 * 1024


class RuntimeUnavailable(Exception):
    """An actionable, non-sensitive error safe to display in the setup UI."""


def _environment():
    return safe_environment()


def _check_directory(path, private):
    try:
        info = path.lstat()
    except OSError as exc:
        raise RuntimeUnavailable("The Cloud Drives runtime directory is unavailable.") from exc
    unsafe_permissions = info.st_mode & (0o077 if private else 0o022)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or unsafe_permissions:
        raise RuntimeUnavailable("The Cloud Drives runtime directory must be owned by you and private, with no symlinks.")


def _location(create=False):
    data_root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    if not data_root.is_absolute():
        raise RuntimeUnavailable("XDG_DATA_HOME must be an absolute directory path.")
    # Check existing ancestors as well: making a private child must not follow
    # a symlink in the supplied installation path.
    for ancestor in (*reversed(data_root.parents), data_root):
        if ancestor.is_symlink():
            raise RuntimeUnavailable("The Cloud Drives runtime path must not contain symlinks.")
    if create:
        data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if data_root.exists():
        _check_directory(data_root, private=False)
    parent = data_root
    for name in ("omarchy-cloud-drives", "runtime"):
        parent = parent / name
        if create:
            parent.mkdir(mode=0o700, exist_ok=True)
        if parent.exists() or parent.is_symlink():
            _check_directory(parent, private=True)
    return parent / DIRECTORY


def _digest(path, limit):
    digest = hashlib.sha256()
    size = 0
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeUnavailable("The private rclone runtime is not a regular file.")
        for chunk in iter(lambda: source.read(CHUNK), b""):
            size += len(chunk)
            if size > limit:
                raise RuntimeUnavailable("The rclone release is larger than expected.")
            digest.update(chunk)
    return digest.hexdigest()


def _private_binary(location):
    if not location.exists() and not location.is_symlink():
        return None
    _check_directory(location, private=True)
    binary = location / "rclone"
    try:
        info = binary.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or not info.st_mode & stat.S_IXUSR:
            raise RuntimeUnavailable("The private rclone runtime has unsafe permissions.")
        if _digest(binary, MAX_BINARY) != BINARY_SHA256:
            raise RuntimeUnavailable("The private rclone runtime failed its integrity check. Remove that runtime version directory and run Prepare again.")
    except OSError as exc:
        raise RuntimeUnavailable("The private rclone runtime is incomplete. Run Prepare after removing the incomplete version directory.") from exc
    return str(binary)


def _system_binary():
    # Only the distribution-managed search path is eligible. Never execute a
    # user PATH entry merely to ask whether it claims to be a recent rclone.
    candidate = shutil.which("rclone", path="/usr/bin:/bin")
    if not candidate:
        return None
    try:
        resolved = Path(candidate).resolve(strict=True)
        info = os.stat(resolved, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022 or not info.st_mode & 0o111):
            return None
        result = subprocess.run([str(resolved), "version"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=5, check=False, env=_environment())
        first_line = result.stdout[:4096].split(b"\n", 1)[0]
        match = re.fullmatch(rb"rclone v([0-9]+)\.([0-9]+)\.([0-9]+)\s*", first_line)
        if result.returncode == 0 and match and tuple(int(value) for value in match.groups()) >= MINIMUM_VERSION:
            return str(resolved)
    except (OSError, subprocess.TimeoutExpired, ValueError, RuntimeError):
        pass
    return None


def resolve_rclone():
    """Return a supported absolute executable path, without changing anything."""
    location = _location()
    if platform.machine().lower() in ("x86_64", "amd64"):
        private = _private_binary(location)
        if private:
            return private
    system = _system_binary()
    if system:
        return system
    raise RuntimeUnavailable("Cloud Drives needs rclone 1.75.1 or newer. Choose Prepare to install its private runtime.")


class _OfficialRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        parsed = urllib.parse.urlparse(new_url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"
        }:
            raise RuntimeUnavailable("The rclone release download redirected outside GitHub's HTTPS release service.")
        return super().redirect_request(request, response, code, message, headers, new_url)


def _download(destination):
    # Preparation is deliberately direct HTTPS: an inherited proxy must not
    # change the provenance boundary for the pinned official release.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _OfficialRedirects())
    request = urllib.request.Request(ASSET_URL, headers={"User-Agent": "Omarchy-Cloud-Drives"})
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    digest = hashlib.sha256()
    size = 0
    try:
        with opener.open(request, timeout=20) as response, destination.open("xb") as target:
            length = response.headers.get("Content-Length")
            if length and (not length.isdecimal() or int(length) > MAX_DOWNLOAD):
                raise RuntimeUnavailable("The rclone release download is larger than expected.")
            while True:
                if time.monotonic() > deadline:
                    raise RuntimeUnavailable("The rclone download timed out. Check your connection and run Prepare again.")
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    raise RuntimeUnavailable("The rclone release download is larger than expected.")
                digest.update(chunk)
                target.write(chunk)
        if digest.hexdigest() != ASSET_SHA256:
            raise RuntimeUnavailable("The rclone release failed its checksum check. Nothing was installed; try Prepare again.")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeUnavailable("The official rclone release could not be downloaded. Check your connection and try Prepare again.") from exc


def _extract_verified(archive, destination):
    # Verify again at the extraction boundary, so neither a partial download nor
    # a caller using this helper directly can execute unauthenticated contents.
    if _digest(archive, MAX_DOWNLOAD) != ASSET_SHA256:
        raise RuntimeUnavailable("The rclone release failed its checksum check. Nothing was installed.")
    try:
        with zipfile.ZipFile(archive) as package:
            names = set()
            for item in package.infolist():
                path = PurePosixPath(item.filename)
                mode = item.external_attr >> 16
                if (item.filename in names or path.is_absolute() or ".." in path.parts
                        or "\\" in item.filename or "\x00" in item.filename
                        or not path.parts or path.parts[0] != DIRECTORY
                        or stat.S_ISLNK(mode)):
                    raise RuntimeUnavailable("The rclone release contains an unsafe archive entry.")
                names.add(item.filename)
            binary_info = package.getinfo(DIRECTORY + "/rclone")
            readme_info = package.getinfo(DIRECTORY + "/README.txt")
            if binary_info.is_dir() or not 0 < binary_info.file_size <= MAX_BINARY or not 0 < readme_info.file_size <= MAX_README:
                raise RuntimeUnavailable("The rclone release has unexpected file sizes.")
            destination.mkdir(mode=0o700)
            binary = destination / "rclone"
            with package.open(binary_info) as source, binary.open("xb") as target:
                size = 0
                for chunk in iter(lambda: source.read(CHUNK), b""):
                    size += len(chunk)
                    if size > MAX_BINARY:
                        raise RuntimeUnavailable("The rclone executable is larger than expected.")
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            binary.chmod(0o700)
            if _digest(binary, MAX_BINARY) != BINARY_SHA256:
                raise RuntimeUnavailable("The extracted rclone executable failed its integrity check.")
            # Official binary archives carry the MIT notice in README.txt,
            # rather than a standalone COPYING file. Retain that exact section.
            with package.open(readme_info) as source:
                readme = source.read(MAX_README + 1).decode("utf-8")
            if len(readme.encode("utf-8")) > MAX_README:
                raise RuntimeUnavailable("The rclone license document is larger than expected.")
            license_text = readme.split("\nLicense\n\n", 1)[1].split("\nAuthors and contributors\n", 1)[0].strip() + "\n"
            if "Permission is hereby granted" not in license_text or "THE SOFTWARE." not in license_text or len(license_text) > 8192:
                raise RuntimeUnavailable("The rclone release does not contain the expected license notice.")
            license_file = destination / "LICENSE.txt"
            with license_file.open("x", encoding="utf-8") as target:
                target.write(license_text)
            license_file.chmod(0o600)
    except (zipfile.BadZipFile, KeyError, IndexError, UnicodeError, OSError) as exc:
        raise RuntimeUnavailable("The rclone release could not be safely unpacked. Nothing was installed.") from exc


def prepare_rclone():
    """Explicitly install the pinned private runtime if no supported one exists."""
    location = _location()
    if platform.machine().lower() in ("x86_64", "amd64"):
        private = _private_binary(location)
        if private:
            return private
    system = _system_binary()
    if system:
        return system
    if platform.system() != "Linux" or platform.machine().lower() not in ("x86_64", "amd64"):
        raise RuntimeUnavailable("Automatic rclone installation supports Linux x86_64 only. Install rclone 1.75.1 or newer for your architecture, then choose Prepare again.")
    try:
        location = _location(create=True)
        fd = os.open(location.parent / "install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeUnavailable("Another Cloud Drives runtime installation is running. Try again when it finishes.") from exc
            private = _private_binary(location)
            if private:
                return private
            with tempfile.TemporaryDirectory(prefix=".install-", dir=location.parent) as temporary:
                temporary = Path(temporary)
                archive = temporary / "release.zip"
                staged = temporary / DIRECTORY
                _download(archive)
                _extract_verified(archive, staged)
                os.rename(staged, location)
            return _private_binary(location)
    except OSError as exc:
        raise RuntimeUnavailable("The private rclone runtime could not be installed. Check directory permissions and free disk space.") from exc


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("path", "prepare"):
        print("Usage: cloud_drives_runtime.py path|prepare", file=sys.stderr)
        return 2
    try:
        print(prepare_rclone() if sys.argv[1] == "prepare" else resolve_rclone())
        return 0
    except RuntimeUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
