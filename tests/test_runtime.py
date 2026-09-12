"""Pinned-runtime tests; downloads and system probes are isolated fixtures."""

import hashlib
import importlib.util
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile
from contextlib import contextmanager


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
SPEC = importlib.util.spec_from_file_location(
    "cloud_drives_runtime", Path(__file__).resolve().parents[1] / "bin" / "cloud_drives_runtime.py"
)
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)

BINARY = b"fixture executable; never run\n"
README = b"Documentation\nLicense\n\nPermission is hereby granted.\nTHE SOFTWARE.\n\nAuthors and contributors\nNames\n"


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir(mode=0o700)
        self.patchers = [
            mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.data)}),
            mock.patch.object(runtime.platform, "machine", return_value="x86_64"),
            mock.patch.object(runtime.platform, "system", return_value="Linux"),
            mock.patch.object(runtime.shutil, "which", return_value=None),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def archive(self, extra=None, binary=BINARY):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
            package.writestr(runtime.DIRECTORY + "/rclone", binary)
            package.writestr(runtime.DIRECTORY + "/README.txt", README)
            if extra:
                package.writestr(*extra)
        return buffer.getvalue()

    def prepared_fixture(self, archive=None):
        archive = archive or self.archive()
        checksum = hashlib.sha256(archive).hexdigest()
        def download(path):
            path.write_bytes(archive)
        return (mock.patch.object(runtime, "ASSET_SHA256", checksum),
                mock.patch.object(runtime, "BINARY_SHA256", hashlib.sha256(BINARY).hexdigest()),
                mock.patch.object(runtime, "_download", side_effect=download))

    @contextmanager
    def system_fixture(self, version=b"rclone v1.75.1\n", uid=0, mode=stat.S_IFREG | 0o755):
        """A real inert file with synthetic root ownership; never execute it."""
        candidate = self.root / "distribution-rclone"
        candidate.write_bytes(BINARY)
        candidate.chmod(0o755)
        real_stat = os.stat

        def system_stat(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if Path(path) == candidate:
                fields = list(info)
                fields[0], fields[4] = mode, uid
                return os.stat_result(fields)
            return info

        result = subprocess.CompletedProcess([], 0, stdout=version)
        with mock.patch.object(runtime.shutil, "which", return_value=str(candidate)) as which, \
             mock.patch.object(runtime.os, "stat", side_effect=system_stat), \
             mock.patch.object(runtime.subprocess, "run", return_value=result) as run:
            yield candidate, which, run

    def test_system_minimum_and_prerelease_rejection(self):
        for version, supported in ((b"rclone v1.75.0\n", False), (b"rclone v1.75.1\n", True),
                                   (b"rclone v1.76.0\n", True), (b"rclone v1.76.0-beta\n", False)):
            with self.subTest(version=version):
                with self.system_fixture(version) as (candidate, which, run):
                    if supported:
                        self.assertEqual(runtime.resolve_rclone(), str(candidate))
                    else:
                        with self.assertRaises(runtime.RuntimeUnavailable):
                            runtime.resolve_rclone()
                    which.assert_called_once_with("rclone", path="/usr/bin:/bin")
                    self.assertEqual(run.call_args.args[0], [str(candidate), "version"])

    def test_version_probe_uses_shared_safe_environment(self):
        unsafe = {"RCLONE_DUMP": "auth", "RCLONE_PASSWORD_COMMAND": "untrusted",
                  "LD_PRELOAD": "/fixture/inject.so", "HTTPS_PROXY": "http://fixture",
                  "SSL_CERT_FILE": "/fixture/ca", "SSLKEYLOGFILE": "/fixture/keylog",
                  "PYTHONPATH": "/fixture/python", "PATH": "/fixture/bin"}
        with mock.patch.dict(os.environ, unsafe), self.system_fixture() as (_, _, run):
            runtime.resolve_rclone()
            self.assertEqual(run.call_args.kwargs["env"], runtime.safe_environment())
        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["PATH"], "/usr/bin:/bin")
        for name in unsafe.keys() - {"PATH"}:
            self.assertNotIn(name, environment)

    def test_user_path_rclone_is_never_selected_or_executed(self):
        commands = self.root / "untrusted-bin"
        commands.mkdir()
        (commands / "rclone").write_bytes(BINARY)
        (commands / "rclone").chmod(0o700)
        # The controlled lookup exposes the impostor only to an ambient PATH
        # lookup. This remains host-independent even if CI has system rclone.
        def lookup(command, path=None):
            self.assertEqual(command, "rclone")
            return str(commands / "rclone") if path is None else None
        with mock.patch.dict(os.environ, {"PATH": str(commands)}), \
             mock.patch.object(runtime.shutil, "which", side_effect=lookup) as which, \
             mock.patch.object(runtime.subprocess, "run") as run:
            with self.assertRaises(runtime.RuntimeUnavailable):
                runtime.resolve_rclone()
            which.assert_called_once_with("rclone", path="/usr/bin:/bin")
            run.assert_not_called()

    def test_system_candidate_must_be_regular_root_owned_and_not_writable_by_others(self):
        for uid, mode in ((1000, stat.S_IFREG | 0o755), (0, stat.S_IFREG | 0o775),
                          (0, stat.S_IFREG | 0o757), (0, stat.S_IFREG | 0o644),
                          (0, stat.S_IFDIR | 0o755), (0, stat.S_IFLNK | 0o755)):
            with self.subTest(uid=uid, mode=oct(mode)), self.system_fixture(uid=uid, mode=mode) as (_, _, run):
                with self.assertRaises(runtime.RuntimeUnavailable):
                    runtime.resolve_rclone()
                run.assert_not_called()

    def test_system_symlink_is_checked_and_executed_by_resolved_target(self):
        with self.system_fixture() as (candidate, which, run):
            link = self.root / "system-link"
            link.symlink_to(candidate)
            which.return_value = str(link)
            self.assertEqual(runtime.resolve_rclone(), str(candidate))
            self.assertEqual(run.call_args.args[0], [str(candidate), "version"])

    def test_prepare_installs_verified_binary_and_license_then_reuses_it(self):
        checksum, binary_hash, downloader = self.prepared_fixture()
        with checksum, binary_hash, downloader as download:
            binary = Path(runtime.prepare_rclone())
            self.assertEqual(binary.read_bytes(), BINARY)
            self.assertEqual(binary.stat().st_mode & 0o777, 0o700)
            self.assertEqual(binary.parent.stat().st_mode & 0o777, 0o700)
            self.assertIn("Permission is hereby granted", (binary.parent / "LICENSE.txt").read_text())
            with mock.patch.object(runtime, "_system_binary", side_effect=AssertionError("private runtime takes priority")):
                self.assertEqual(runtime.resolve_rclone(), str(binary))
                self.assertEqual(runtime.prepare_rclone(), str(binary))
            download.assert_called_once()

    def test_checksum_failure_does_not_install_any_executable(self):
        with mock.patch.object(runtime, "_download", side_effect=lambda path: path.write_bytes(b"corrupted")):
            with self.assertRaisesRegex(runtime.RuntimeUnavailable, "checksum"):
                runtime.prepare_rclone()
        self.assertFalse(runtime._location().exists())
        self.assertFalse(list(self.data.rglob("rclone")))
        self.assertFalse(list(self.data.rglob(".install-*")))

    def test_corrupt_private_binary_is_rejected_before_execution(self):
        checksum, binary_hash, downloader = self.prepared_fixture()
        with checksum, binary_hash, downloader:
            binary = Path(runtime.prepare_rclone())
            binary.write_bytes(b"damaged")
            with mock.patch.object(runtime.subprocess, "run", side_effect=AssertionError("must not execute")), self.assertRaisesRegex(runtime.RuntimeUnavailable, "integrity"):
                runtime.resolve_rclone()

    def test_archive_traversal_is_rejected_even_if_archive_hash_matches(self):
        archive = self.archive(("../outside", b"unsafe"))
        checksum, binary_hash, downloader = self.prepared_fixture(archive)
        with checksum, binary_hash, downloader, self.assertRaisesRegex(runtime.RuntimeUnavailable, "unsafe archive"):
            runtime.prepare_rclone()
        self.assertFalse((self.root / "outside").exists())
        self.assertFalse(runtime._location().exists())

    def test_archive_symlink_is_rejected(self):
        link = zipfile.ZipInfo(runtime.DIRECTORY + "/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive = self.archive((link, b"/etc/passwd"))
        checksum, binary_hash, downloader = self.prepared_fixture(archive)
        with checksum, binary_hash, downloader, self.assertRaisesRegex(runtime.RuntimeUnavailable, "unsafe archive"):
            runtime.prepare_rclone()

    def test_unsafe_install_directory_is_rejected(self):
        target = self.root / "elsewhere"
        target.mkdir(mode=0o700)
        (self.data / "omarchy-cloud-drives").symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(runtime.RuntimeUnavailable, "symlinks"):
            runtime.prepare_rclone()
        self.assertEqual(list(target.iterdir()), [])

    def test_unsupported_architecture_never_downloads_x86_binary(self):
        with mock.patch.object(runtime.platform, "machine", return_value="aarch64"), mock.patch.object(runtime, "_download") as download:
            with self.assertRaisesRegex(runtime.RuntimeUnavailable, "architecture"):
                runtime.prepare_rclone()
            download.assert_not_called()

    def test_resolution_never_prepares_or_creates_directories(self):
        with mock.patch.object(runtime, "_download") as download:
            with self.assertRaises(runtime.RuntimeUnavailable):
                runtime.resolve_rclone()
            download.assert_not_called()
        self.assertEqual(list(self.data.iterdir()), [])

    def test_download_rejects_wrong_checksum(self):
        response = io.BytesIO(b"unverified bytes")
        response.headers = {"Content-Length": "16"}
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(runtime.urllib.request, "build_opener", return_value=opener), self.assertRaisesRegex(runtime.RuntimeUnavailable, "checksum"):
            runtime._download(self.root / "archive.zip")
        self.assertEqual(opener.open.call_args.args[0].full_url, runtime.ASSET_URL)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 20)

    def test_download_is_bounded_before_read(self):
        response = io.BytesIO(b"unused")
        response.headers = {"Content-Length": str(runtime.MAX_DOWNLOAD + 1)}
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(runtime.urllib.request, "build_opener", return_value=opener), self.assertRaisesRegex(runtime.RuntimeUnavailable, "larger"):
            runtime._download(self.root / "archive.zip")

    def test_download_disables_ambient_proxies(self):
        payload = b"verified download fixture"
        response = io.BytesIO(payload)
        response.headers = {"Content-Length": str(len(payload))}
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://untrusted", "https_proxy": "http://untrusted"}), \
             mock.patch.object(runtime, "ASSET_SHA256", hashlib.sha256(payload).hexdigest()), \
             mock.patch.object(runtime.urllib.request, "build_opener", return_value=opener) as build:
            runtime._download(self.root / "archive.zip")
        proxies = [handler for handler in build.call_args.args
                   if isinstance(handler, runtime.urllib.request.ProxyHandler)]
        self.assertEqual(len(proxies), 1)
        self.assertEqual(proxies[0].proxies, {})

    def test_redirects_must_stay_on_official_https_hosts(self):
        redirects = runtime._OfficialRedirects()
        for url in ("http://github.com/release.zip", "https://example.invalid/release.zip"):
            with self.subTest(url=url), self.assertRaises(runtime.RuntimeUnavailable):
                redirects.redirect_request(None, None, 302, "", {}, url)


@unittest.skipUnless(os.environ.get("RCLONE_RELEASE_ARCHIVE"), "set RCLONE_RELEASE_ARCHIVE to verify the pinned official archive locally")
class OfficialArchiveTests(unittest.TestCase):
    def test_pinned_archive_extracts_binary_and_complete_mit_notice(self):
        archive = Path(os.environ["RCLONE_RELEASE_ARCHIVE"]).resolve()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / runtime.DIRECTORY
            runtime._extract_verified(archive, target)
            self.assertEqual(runtime._private_binary(target), str(target / "rclone"))
            notice = (target / "LICENSE.txt").read_text()
            self.assertIn("Copyright (C) 2019 by Nick Craig-Wood", notice)
            self.assertIn("Permission is hereby granted", notice)
            self.assertIn("IN NO EVENT SHALL THE", notice)


if __name__ == "__main__":
    unittest.main()
