"""Shared fixtures: local git remotes so tests run offline and deterministically.

`git ls-remote` (and west itself) accept plain filesystem paths as remote
urls, so a bare-bones local repository stands in for GitHub in end-to-end
tests. Expected shas are read back with `git rev-parse` — no golden hashes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent

import pytest


class GitRemote:
    def __init__(self, path: Path):
        self.path = path
        path.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.com")
        # let `git fetch <url> <sha>` work (GitHub enables this; plain git doesn't)
        self.git("config", "uploadpack.allowAnySHA1InWant", "true")
        self.commit("initial")

    @property
    def url(self) -> str:
        return str(self.path)

    def git(self, *args: str) -> str:
        p = subprocess.run(
            ["git", "-C", str(self.path), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return p.stdout.strip()

    def commit(self, message: str = "commit") -> str:
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.sha()

    def commit_file(self, relpath: str, content: str, message: str = "add file") -> str:
        target = self.path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.git("add", relpath)
        self.git("commit", "-q", "-m", message)
        return self.sha()

    def branch(self, name: str) -> None:
        self.git("branch", name)

    def tag(self, name: str, annotated: bool = False) -> None:
        if annotated:
            self.git("tag", "-a", "-m", name, name)
        else:
            self.git("tag", name)

    def sha(self, rev: str = "HEAD") -> str:
        return self.git("rev-parse", f"{rev}^{{commit}}")


@pytest.fixture
def remote(tmp_path):
    """Factory for local git remotes, keyed by name under one directory."""

    def make(name: str = "proj") -> GitRemote:
        return GitRemote(tmp_path / "remotes" / name)

    make.base = tmp_path / "remotes"
    return make


@pytest.fixture
def write_manifest(tmp_path):
    """Write a (dedented) manifest to a fresh west.yml and return its path."""

    def write(text: str) -> Path:
        path = tmp_path / "west.yml"
        path.write_text(dedent(text))
        return path

    return write
