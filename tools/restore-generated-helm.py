#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Restore generated Helm data without letting the archive replace trusted code."""
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile


HELM_ROOT = pathlib.PurePosixPath("operations/helm")
GIT_CONTROL_FILES = frozenset({".gitattributes", ".gitignore", ".gitmodules", ".lfsconfig"})


class RestoreError(ValueError):
    pass


def git(workspace, *args):
    return subprocess.check_output(["git", "-C", str(workspace), *args])


def trusted_entries(workspace):
    entries = {}
    for record in git(workspace, "ls-tree", "-r", "-z", "HEAD", "--", str(HELM_ROOT)).split(b"\0"):
        if not record:
            continue
        header, raw_path = record.split(b"\t", 1)
        mode, _, oid = header.split()
        path = pathlib.PurePosixPath(os.fsdecode(raw_path))
        if mode == b"120000" or path.name.casefold() in GIT_CONTROL_FILES:
            entries[path] = (mode.decode(), git(workspace, "cat-file", "blob", oid.decode()))
    return entries


def git_mode(member):
    if member.issym():
        return "120000"
    if member.isfile():
        return "100755" if member.mode & stat.S_IXUSR else "100644"
    return "040000"


def validate_members(archive, trusted):
    members = {}
    for member in archive.getmembers():
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or path.parts[:2] != HELM_ROOT.parts:
            raise RestoreError("Archive path outside Helm tree: " + repr(member.name))
        if any(part.casefold() == ".git" for part in path.parts):
            raise RestoreError("Git metadata in archive: " + repr(member.name))
        if path in members:
            raise RestoreError("Duplicate archive path: " + repr(member.name))
        if member.type not in (
            tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE, tarfile.SYMTYPE
        ):
            raise RestoreError("Unsupported archive entry: " + repr(member.name))
        if member.issym() and trusted.get(path) != (
            "120000", os.fsencode(member.linkname)
        ):
            raise RestoreError("New or modified symlink: " + repr(member.name))
        if path.name.casefold() in GIT_CONTROL_FILES:
            if path not in trusted or trusted[path][0] != git_mode(member):
                raise RestoreError("New or modified Git control file: " + repr(member.name))
        members[path] = member

    if HELM_ROOT not in members or not members[HELM_ROOT].isdir():
        raise RestoreError("Archive must contain the Helm root directory")
    if not any(member.isfile() or member.issym() for member in members.values()):
        raise RestoreError("Archive must contain a nonempty Helm tree")
    for path in members:
        for parent in path.parents:
            if parent in members and not members[parent].isdir():
                raise RestoreError("Archive entry beneath a non-directory: " + repr(str(path)))
    controls = {path for path in members if path.name.casefold() in GIT_CONTROL_FILES}
    expected_controls = {path for path in trusted if path.name.casefold() in GIT_CONTROL_FILES}
    if controls != expected_controls:
        raise RestoreError("Archive must preserve tracked Git control files")
    return members


def stage_tree(archive, members, trusted, destination):
    destination.mkdir(mode=0o755)
    for path, member in members.items():
        target = destination.joinpath(*path.relative_to(HELM_ROOT).parts)
        if member.isdir():
            target.mkdir(mode=0o755, parents=True, exist_ok=True)
        elif member.isfile():
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            with archive.extractfile(member) as source, target.open("xb") as output:
                if path.name.casefold() in GIT_CONTROL_FILES:
                    expected = trusted[path][1]
                    if member.size != len(expected) or source.read(len(expected) + 1) != expected:
                        raise RestoreError("Modified Git control file: " + repr(str(path)))
                    output.write(expected)
                else:
                    shutil.copyfileobj(source, output)
            target.chmod(0o755 if member.mode & stat.S_IXUSR else 0o644)

    # Existing links can point outside Helm. Never extract or write through them.
    for path, member in members.items():
        if member.issym():
            target = destination.joinpath(*path.relative_to(HELM_ROOT).parts)
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            target.symlink_to(os.fsdecode(trusted[path][1]))


def restore(archive_path, workspace):
    workspace = pathlib.Path(workspace).resolve(strict=True)
    git_root = pathlib.Path(
        os.fsdecode(git(workspace, "rev-parse", "--show-toplevel")).rstrip("\n")
    )
    if git_root.resolve() != workspace:
        raise RestoreError("Run from the checkout root")
    helm = workspace.joinpath(*HELM_ROOT.parts)
    if not all(stat.S_ISDIR(path.lstat().st_mode) for path in (helm.parent, helm)):
        raise RestoreError("The existing Helm tree and its parent must be real directories")
    trusted = trusted_entries(workspace)
    archive_path = pathlib.Path(archive_path)
    if not stat.S_ISREG(archive_path.lstat().st_mode):
        raise RestoreError("The artifact must be a regular archive file")

    staging = None
    installed = False
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(archive_path, flags), "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise RestoreError("The artifact must be a regular archive file")
            with tarfile.open(fileobj=source, mode="r:*") as archive:
                members = validate_members(archive, trusted)
                staging = pathlib.Path(
                    tempfile.mkdtemp(prefix=".helm-restore-", dir=helm.parent)
                )
                replacement = staging / "new"
                stage_tree(archive, members, trusted, replacement)

        backup = staging / "original"
        os.replace(helm, backup)
        try:
            os.replace(replacement, helm)
        except BaseException:
            try:
                os.replace(backup, helm)
            except OSError as error:
                raise RestoreError(
                    "Rollback failed; original Helm tree retained at " + repr(str(backup))
                ) from error
            raise
        installed = True
    finally:
        # An interrupted install must not delete the only remaining original tree.
        if staging is not None and (installed or not (staging / "original").exists()):
            shutil.rmtree(staging)


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise RestoreError("Usage: restore-generated-helm.py ARCHIVE")
        restore(sys.argv[1], pathlib.Path.cwd())
    except (OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as error:
        raise SystemExit("Helm artifact restoration failed: " + str(error))
