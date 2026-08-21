"""Remote ref resolution via `git ls-remote` and the GitHub API.

Everything here works without cloning: branch/tag heads and tag lists come
from single ls-remote round-trips, and release/commit lookups use the GitHub
REST API where the remote is a github.com repo.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from typing import NamedTuple

_GITHUB_URL = re.compile(
    r"^(?:https://|http://|git@|ssh://git@)github\.com[:/]"
    r"(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)


class ResolveError(Exception):
    pass


class Resolved(NamedTuple):
    sha: str
    kind: str  # "branch" or "tag"
    ambiguous: bool = False


class RemoteRefs(NamedTuple):
    branches: dict[str, str]
    tags: dict[str, str]  # peeled: annotated tags map to their commit sha


def git(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=cwd, check=False
    )


def _shallow_fetch(tmp: str, url: str, revision: str) -> bool:
    """Fetch a single revision into a throwaway repo at tmp; True on success."""
    git("init", "-q", cwd=tmp)
    return git("fetch", "-q", "--depth=1", "--", url, revision, cwd=tmp).returncode == 0


@contextmanager
def _fetched(url: str, revision: str) -> Iterator[str]:
    """A throwaway repo with `revision` of `url` at FETCH_HEAD."""
    with tempfile.TemporaryDirectory(prefix="pin-west-") as tmp:
        if not _shallow_fetch(tmp, url, revision):
            raise ResolveError(f"cannot fetch '{revision}' from {url}")
        yield tmp


def ls_remote(url: str, *patterns: str) -> dict[str, str]:
    p = git("ls-remote", "--", url, *patterns)
    if p.returncode:
        raise ResolveError(f"git ls-remote {url}: {p.stderr.strip()}")
    refs = {}
    for line in p.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        refs[ref] = sha
    return refs


def resolve_ref(url: str, ref: str) -> Resolved | None:
    """Resolve a branch or tag name to (commit sha, kind) on the remote."""
    refs = ls_remote(
        url, f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}"
    )
    tag = refs.get(f"refs/tags/{ref}^{{}}") or refs.get(f"refs/tags/{ref}")
    branch = refs.get(f"refs/heads/{ref}")
    if tag:
        return Resolved(tag, "tag", ambiguous=branch is not None)
    if branch:
        return Resolved(branch, "branch")
    return None


def default_branch(url: str) -> tuple[str, str] | None:
    """The remote's default branch as (name, head sha), in one round-trip."""
    p = git("ls-remote", "--symref", "--", url, "HEAD")
    if p.returncode:
        raise ResolveError(f"git ls-remote {url}: {p.stderr.strip()}")
    name = re.search(r"^ref:\s+refs/heads/(\S+)\s+HEAD$", p.stdout, re.MULTILINE)
    sha = re.search(r"^([0-9a-fA-F]+)\s+HEAD$", p.stdout, re.MULTILINE)
    return (name.group(1), sha.group(1)) if name and sha else None


def remote_refs(url: str) -> RemoteRefs:
    """All branches and (peeled) tags on the remote, one round-trip."""
    branches: dict[str, str] = {}
    base: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for ref, sha in ls_remote(url, "refs/heads/*", "refs/tags/*").items():
        if ref.startswith("refs/heads/"):
            branches[ref.removeprefix("refs/heads/")] = sha
        elif ref.startswith("refs/tags/"):
            tag = ref.removeprefix("refs/tags/")
            if tag.endswith("^{}"):
                peeled[tag.removesuffix("^{}")] = sha
            else:
                base[tag] = sha
    return RemoteRefs(
        branches, {tag: peeled.get(tag, sha) for tag, sha in base.items()}
    )


def fetch_blob(url: str, revision: str, path: str) -> str | None:
    """File content at a revision of a remote repo, without cloning it.

    GitHub remotes are served straight from raw.githubusercontent.com (one
    HTTPS GET, no API rate limit); anything else falls back to a shallow
    fetch into a throwaway repo. Returns None if the file doesn't exist."""
    if gh := github_repo(url):
        raw = (
            f"https://raw.githubusercontent.com/{gh[0]}/{gh[1]}/"
            f"{urllib.parse.quote(revision)}/{urllib.parse.quote(path)}"
        )
        req = urllib.request.Request(raw, headers={"User-Agent": "pin-west"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.read().decode()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise ResolveError(f"{raw}: HTTP {e.code} {e.reason}")
        except urllib.error.URLError as e:
            raise ResolveError(f"{raw}: {e.reason}")
    with _fetched(url, revision) as tmp:
        p = git("show", f"FETCH_HEAD:{path}", cwd=tmp)
        return p.stdout if p.returncode == 0 else None


def list_dir(
    url: str, revision: str, path: str, token: str | None = None
) -> list[str] | None:
    """Entry names of a directory at a revision of a remote repo, without
    cloning it. None if path is not a directory there.

    GitHub remotes use the contents API (one call); anything else falls
    back to a shallow fetch into a throwaway repo."""
    if gh := github_repo(url):
        data = github_api(
            f"/repos/{gh[0]}/{gh[1]}/contents/{urllib.parse.quote(path)}"
            f"?ref={urllib.parse.quote(revision)}",
            token,
        )
        if not isinstance(data, list):  # a file (dict) or 404
            return None
        return [entry["name"] for entry in data if isinstance(entry, dict)]
    with _fetched(url, revision) as tmp:
        # ls-tree fails on a blob ("not a tree object") and on a missing path
        p = git("ls-tree", "--name-only", f"FETCH_HEAD:{path}", cwd=tmp)
        return p.stdout.split() if p.returncode == 0 else None


# --- GitHub API ---------------------------------------------------------


def github_repo(url: str) -> tuple[str, str] | None:
    m = _GITHUB_URL.match(url)
    return (m["owner"], m["repo"]) if m else None


def find_token(cli_token: str | None) -> str | None:
    if cli_token:
        return cli_token
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if token := os.environ.get(var):
            return token
    try:
        p = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=False
        )
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    except FileNotFoundError:
        pass
    return None


def github_api(path: str, token: str | None) -> dict | list | None:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pin-west",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request("https://api.github.com" + path, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (404, 422):
            return None
        raise ResolveError(f"GitHub API {path}: HTTP {e.code} {e.reason}")
    except urllib.error.URLError as e:
        raise ResolveError(f"GitHub API {path}: {e.reason}")


def latest_release(owner: str, repo: str, token: str | None) -> str | None:
    data = github_api(f"/repos/{owner}/{repo}/releases/latest", token)
    return data.get("tag_name") if isinstance(data, dict) else None


def commit_exists(
    url: str, sha: str, token: str | None, refs: RemoteRefs | None = None
) -> bool:
    """Whether sha exists on the remote. A RemoteRefs snapshot, if given,
    proves existence without a round-trip when sha sits at an advertised
    branch or (peeled) tag tip."""
    if refs is not None and (
        sha in refs.tags.values() or sha in refs.branches.values()
    ):
        return True
    if gh := github_repo(url):
        return github_api(f"/repos/{gh[0]}/{gh[1]}/commits/{sha}", token) is not None
    with tempfile.TemporaryDirectory(prefix="pin-west-") as tmp:
        return _shallow_fetch(tmp, url, sha)


def sha_on_ref(url: str, sha: str, ref: str, token: str | None) -> bool | None:
    """Whether sha is in the history of ref. None if undeterminable cheaply."""
    gh = github_repo(url)
    if not gh:
        return None
    cmp = github_api(
        f"/repos/{gh[0]}/{gh[1]}/compare/{urllib.parse.quote(ref)}...{sha}", token
    )
    if not isinstance(cmp, dict):
        return None
    return cmp.get("status") in ("identical", "behind")
