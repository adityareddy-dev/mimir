# SPDX-License-Identifier: AGPL-3.0-only
import copy
import importlib.util
import io
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).with_name("restore-generated-helm.py").resolve()
MODE_CHECKER = SCRIPT.with_name("check-api-file-modes.py")
spec = importlib.util.spec_from_file_location("restore_helm", SCRIPT)
restorer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restorer)


def tree_snapshot(root):
    if not root.exists():
        return None
    entries = {}
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = pathlib.Path(directory) / name
            relative = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                entries[relative] = ("link", os.readlink(path))
            elif stat.S_ISDIR(mode):
                entries[relative] = ("directory",)
            else:
                entries[relative] = ("file", bool(mode & stat.S_IXUSR), path.read_bytes())
    return entries


def member(name, content=b"", kind=tarfile.REGTYPE, target="", mode=0o644):
    entry = tarfile.TarInfo(name)
    entry.type = kind
    entry.linkname = target
    entry.mode = mode
    entry.size = len(content) if kind in (tarfile.REGTYPE, tarfile.AREGTYPE) else 0
    return entry, content


class RestoreGeneratedHelmTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="helm-restore-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.helm = self.workspace / "operations" / "helm"
        self.helm.mkdir(parents=True)
        (self.workspace / "tools").mkdir()
        self.checker = self.workspace / "tools" / "checker.py"
        self.checker.write_bytes(b"trusted checker\n")
        (self.workspace / "outside").mkdir()
        self.sentinel = self.workspace / "outside" / "sentinel"
        self.sentinel.write_bytes(b"outside the Helm tree\n")
        (self.helm / ".gitignore").write_bytes(b"ignored/\n")
        (self.helm / "existing.yaml").write_bytes(b"old\n")
        (self.helm / "delete.yaml").write_bytes(b"delete me\n")
        (self.helm / "script.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
        (self.helm / "script.sh").chmod(0o755)
        (self.helm / "internal-link").symlink_to("existing.yaml")
        (self.helm / "external-link").symlink_to("../../outside/sentinel")
        (self.helm / "external-directory").symlink_to("../../outside", target_is_directory=True)
        self.git("init")
        for key, value in {
            "user.name": "Example", "user.email": "example@example.com",
            "commit.gpgsign": "false", "core.filemode": "true", "core.autocrlf": "false",
            "core.hooksPath": "/dev/null",
        }.items():
            self.git("config", key, value)
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.original = tree_snapshot(self.helm)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.workspace), *args], stderr=subprocess.PIPE)

    def archive(self, replacements=None, omitted=(), extra=(), reverse=False, entries=None):
        if entries is None:
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w", dereference=False) as archive:
                archive.add(self.helm, arcname="operations/helm")
            buffer.seek(0)
            entries = []
            with tarfile.open(fileobj=buffer, mode="r:") as archive:
                for entry in archive:
                    data = archive.extractfile(entry).read() if entry.isfile() else b""
                    if entry.name not in omitted:
                        entries.append((replacements or {}).get(entry.name, (copy.copy(entry), data)))
            entries.extend(extra)
        path = self.root / "generated.tar"
        with tarfile.open(path, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for entry, data in reversed(entries) if reverse else entries:
                archive.addfile(entry, io.BytesIO(data) if entry.isfile() else None)
        return path

    def assert_original_intact(self):
        self.assertEqual(self.original, tree_snapshot(self.helm))
        self.assertEqual(b"trusted checker\n", self.checker.read_bytes())
        self.assertEqual(b"outside the Helm tree\n", self.sentinel.read_bytes())

    def test_supported_changes_round_trip_and_pass_publication_mode_check(self):
        archive = self.archive(
            replacements={"operations/helm/existing.yaml": member("operations/helm/existing.yaml", b"new\n")},
            omitted={"operations/helm/delete.yaml", "operations/helm/internal-link"},
            extra=[member("operations/helm/new directory/binary file", b" \t\x00binary\n\n", mode=0o4644)],
            reverse=True,
        )
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), str(archive)], cwd=self.workspace, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(b"new\n", (self.helm / "existing.yaml").read_bytes())
        self.assertEqual(b" \t\x00binary\n\n", (self.helm / "new directory/binary file").read_bytes())
        self.assertEqual(0o644, stat.S_IMODE((self.helm / "new directory/binary file").stat().st_mode))
        self.assertEqual(0o755, stat.S_IMODE((self.helm / "script.sh").stat().st_mode))
        self.assertFalse((self.helm / "delete.yaml").exists())
        self.assertFalse((self.helm / "internal-link").is_symlink())
        self.assertEqual("../../outside/sentinel", os.readlink(self.helm / "external-link"))
        self.assertEqual(b"outside the Helm tree\n", self.sentinel.read_bytes())
        self.git("add", "-A", "--", "operations/helm/")
        check = subprocess.run([sys.executable, str(MODE_CHECKER), "operations/helm/"], cwd=self.workspace, capture_output=True)
        self.assertEqual(0, check.returncode, check.stderr)
        self.assertFalse(list(self.helm.parent.glob(".helm-restore-*")))

    def test_unrepresentable_executable_change_still_fails_mode_check(self):
        archive = self.archive(extra=[member("operations/helm/new-script", b"example\n", mode=0o755)])
        restorer.restore(archive, self.workspace)
        self.git("add", "-A", "--", "operations/helm/")
        result = subprocess.run([sys.executable, str(MODE_CHECKER), "operations/helm/"], cwd=self.workspace, capture_output=True)
        self.assertNotEqual(0, result.returncode)

    def test_outside_traversal_and_git_metadata_paths_are_rejected(self):
        for name in ["tools/checker.py", "/absolute", "operations/helm/../escape", "operations/helmish/file",
                     "operations/helm/.git/config", "operations/helm/nested/.GIT/hooks/example"]:
            with self.subTest(path=name):
                with self.assertRaises(restorer.RestoreError):
                    restorer.restore(self.archive(extra=[member(name, b"untrusted marker\n")]), self.workspace)
                self.assert_original_intact()

    def test_effective_pax_paths_and_link_targets_are_validated(self):
        path_entry = member("operations/helm/looks-safe", b"untrusted\n")
        path_entry[0].pax_headers = {"path": "tools/checker.py"}
        link_entry = member("operations/helm/external-link", kind=tarfile.SYMTYPE, target="../../outside/sentinel")
        link_entry[0].pax_headers = {"linkpath": "../../tools/checker.py"}
        for entry in [path_entry, link_entry]:
            with self.subTest(entry=entry[0].name):
                kwargs = {"extra": [entry]} if entry is path_entry else {"replacements": {entry[0].name: entry}}
                with self.assertRaises(restorer.RestoreError):
                    restorer.restore(self.archive(**kwargs), self.workspace)
                self.assert_original_intact()

    def test_links_and_special_entries_are_rejected(self):
        cases = [
            member("operations/helm/new-link", kind=tarfile.SYMTYPE, target="../../tools/checker.py"),
            member("operations/helm/new-hardlink", kind=tarfile.LNKTYPE, target="tools/checker.py"),
            member("operations/helm/fifo", kind=tarfile.FIFOTYPE),
            member("operations/helm/device", kind=tarfile.CHRTYPE),
        ]
        for entry in cases:
            with self.subTest(kind=entry[0].type):
                with self.assertRaises(restorer.RestoreError):
                    restorer.restore(self.archive(extra=[entry]), self.workspace)
                self.assert_original_intact()

    def test_non_directory_ancestors_are_rejected_in_either_order(self):
        for parent in ["external-directory", "existing.yaml"]:
            for reverse in [False, True]:
                with self.subTest(parent=parent, reverse=reverse):
                    archive = self.archive(extra=[member("operations/helm/" + parent + "/child", b"untrusted\n")], reverse=reverse)
                    with self.assertRaises(restorer.RestoreError):
                        restorer.restore(archive, self.workspace)
                    self.assert_original_intact()

    def test_duplicate_normalized_paths_are_rejected(self):
        for name in ["operations/helm/existing.yaml", "operations/helm/./existing.yaml", "operations//helm/existing.yaml"]:
            with self.subTest(path=name):
                with self.assertRaises(restorer.RestoreError):
                    restorer.restore(self.archive(extra=[member(name, b"duplicate\n")]), self.workspace)
                self.assert_original_intact()

    def test_empty_missing_and_wrong_root_archives_are_rejected(self):
        root = member("operations/helm", kind=tarfile.DIRTYPE)
        archives = [[], [root], [member("operations/helm/file", b"data")], [member("operations/helm", b"not a directory")]]
        for entries in archives:
            with self.subTest(entries=len(entries)):
                with self.assertRaises(restorer.RestoreError):
                    restorer.restore(self.archive(entries=entries), self.workspace)
                self.assert_original_intact()

    def test_git_control_files_are_immutable(self):
        changes = [
            {"extra": [member("operations/helm/" + name, b"untrusted\n")]}
            for name in [".gitattributes", ".gitmodules", ".lfsconfig", "nested/.gitignore", ".GITATTRIBUTES"]
        ]
        changes.extend([
            {"omitted": {"operations/helm/.gitignore"}},
            {"replacements": {"operations/helm/.gitignore": member("operations/helm/.gitignore", b"new ignores\n")}},
            {"replacements": {"operations/helm/.gitignore": member("operations/helm/.gitignore", b"ignored/\n", mode=0o755)}},
        ])
        for change in changes:
            with self.subTest(change=change):
                with self.assertRaises(restorer.RestoreError):
                    restorer.restore(self.archive(**change), self.workspace)
                self.assert_original_intact()

    def test_rejection_stops_before_git_filters_and_publication(self):
        filter_marker = self.root / "filter-ran"
        publish_marker = self.root / "publication-ran"
        driver = self.workspace / "tools" / "filter.py"
        driver.write_text("import pathlib, sys\npathlib.Path(sys.argv[1]).touch()\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n")
        self.git("config", "filter.example.clean", sys.executable + " " + str(driver) + " " + str(filter_marker))
        archive = self.archive(extra=[member("operations/helm/.gitattributes", b"*.yaml filter=example\n")])
        result = subprocess.run(
            ["bash", "-e", "-c", '"$1" -B "$2" "$3"; git add -A -- operations/helm/; touch "$4"',
             "--", sys.executable, str(SCRIPT), str(archive), str(publish_marker)],
            cwd=self.workspace, capture_output=True,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(filter_marker.exists())
        self.assertFalse(publish_marker.exists())
        self.assert_original_intact()

    def test_malformed_and_truncated_archives_preserve_original_tree(self):
        archive = self.root / "broken.tar"
        archive.write_bytes(b"not a tar file")
        with self.assertRaises(tarfile.TarError):
            restorer.restore(archive, self.workspace)
        self.assert_original_intact()
        archive = self.archive(extra=[member("operations/helm/large.bin", b"x" * 8192)])
        with tarfile.open(archive) as contents:
            offset = contents.getmember("operations/helm/large.bin").offset_data
        archive.write_bytes(archive.read_bytes()[:offset + 200])
        with self.assertRaises((OSError, tarfile.TarError)):
            restorer.restore(archive, self.workspace)
        self.assert_original_intact()

    def test_archive_itself_must_be_a_regular_file(self):
        archive = self.archive()
        link = self.root / "archive-link"
        link.symlink_to(archive)
        directory = self.root / "directory"
        directory.mkdir()
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        for path in [link, directory, fifo]:
            with self.subTest(path=path.name):
                with self.assertRaises(restorer.RestoreError):
                    restorer.restore(path, self.workspace)
                self.assert_original_intact()

    def test_staging_and_installation_failures_preserve_original_tree(self):
        archive = self.archive()
        with mock.patch.object(restorer, "stage_tree", side_effect=OSError("staging failure")):
            with self.assertRaisesRegex(OSError, "staging failure"):
                restorer.restore(archive, self.workspace)
        self.assert_original_intact()
        real_replace = os.replace
        for failed_source in ["helm", "new"]:
            def replace(source, destination):
                if pathlib.Path(source).name == failed_source:
                    raise OSError("installation failure")
                return real_replace(source, destination)
            with self.subTest(source=failed_source), mock.patch.object(restorer.os, "replace", side_effect=replace):
                with self.assertRaisesRegex(OSError, "installation failure"):
                    restorer.restore(archive, self.workspace)
            self.assert_original_intact()
            self.assertFalse(list(self.helm.parent.glob(".helm-restore-*")))

    def test_failed_rollback_retains_and_reports_original_backup(self):
        archive = self.archive()
        real_replace = os.replace
        def replace(source, destination):
            if pathlib.Path(source).name in ["new", "original"]:
                raise OSError("injected rename failure")
            return real_replace(source, destination)
        with mock.patch.object(restorer.os, "replace", side_effect=replace):
            with self.assertRaisesRegex(restorer.RestoreError, "original Helm tree retained at") as failure:
                restorer.restore(archive, self.workspace)
        backups = list(self.helm.parent.glob(".helm-restore-*/original"))
        self.assertEqual(1, len(backups))
        self.assertIn(str(backups[0]), str(failure.exception))
        self.assertEqual(self.original, tree_snapshot(backups[0]))
        self.assertEqual(b"outside the Helm tree\n", self.sentinel.read_bytes())

    def test_interrupted_install_restores_original_tree(self):
        archive = self.archive()
        real_replace = os.replace
        def replace(source, destination):
            if pathlib.Path(source).name == "new":
                raise KeyboardInterrupt()
            return real_replace(source, destination)
        with mock.patch.object(restorer.os, "replace", side_effect=replace):
            with self.assertRaises(KeyboardInterrupt):
                restorer.restore(archive, self.workspace)
        self.assert_original_intact()
        self.assertFalse(list(self.helm.parent.glob(".helm-restore-*")))

    def test_repository_producer_archive_round_trips(self):
        shutil.rmtree(self.helm)
        shutil.copytree(SCRIPT.parents[1] / "operations" / "helm", self.helm, symlinks=True)
        self.git("add", "-A", "--", "operations/helm/")
        self.git("commit", "-m", "repository Helm fixture")
        original = tree_snapshot(self.helm)
        archive = self.root / "repository.tar"
        env = os.environ.copy()
        env["COPYFILE_DISABLE"] = "1"
        subprocess.run(["tar", "-cf", str(archive), "operations/helm"], cwd=self.workspace, env=env, check=True)
        restorer.restore(archive, self.workspace)
        self.assertEqual(original, tree_snapshot(self.helm))
        self.assertEqual(b"outside the Helm tree\n", self.sentinel.read_bytes())
        self.assertEqual(b"", self.git("status", "--porcelain", "--", "operations/helm/"))


if __name__ == "__main__":
    unittest.main()
