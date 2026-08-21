"""pin-west command line interface."""

from __future__ import annotations

import argparse
import difflib
import importlib.metadata
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeGuard

from west.configuration import MalformedConfig
from west.manifest import (
    QUAL_MANIFEST_REV_BRANCH,
    ImportFlag,
    MalformedManifest,
    Manifest,
    ManifestProject,
)
from west.util import WestNotFound, west_topdir

from . import imports, tracking
from . import resolve as res
from .manifest import ManifestError, ManifestFile, comment_ref, is_pinned

try:
    __version__ = importlib.metadata.version("pin-west")
except importlib.metadata.PackageNotFoundError:  # source tree, not installed
    __version__ = "0.0.0+unknown"


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ManifestError, res.ResolveError, MalformedManifest) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pin-west",
        description="Pin revisions in west manifests to exact commit SHAs.",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(
        name: str, func, help: str, *, dry_run: bool = False, gh_token: bool = False
    ) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help)
        sp.add_argument(
            "-f",
            "--manifest",
            type=Path,
            default=None,
            help="manifest file to operate on (default: ./west.yml, else the "
            "enclosing west workspace's configured manifest)",
        )
        if gh_token:
            sp.add_argument(
                "--gh-token",
                help="GitHub token (default: $GITHUB_TOKEN, $GH_TOKEN, gh auth)",
            )
        if dry_run:
            sp.add_argument(
                "-n", "--dry-run", action="store_true", help="print a diff, don't write"
            )
        sp.set_defaults(func=func, dry_run=False)
        return sp

    sp = add(
        "pin",
        cmd_pin,
        "pin unpinned revisions to the current head of their ref",
        dry_run=True,
    )
    sp.add_argument(
        "--local",
        action="store_true",
        help="pin to the revisions resolved in the enclosing west workspace "
        "(west manifest --freeze) instead of querying the remotes",
    )
    sp.add_argument(
        "--include-imports",
        action="store_true",
        help="also materialize and pin imported (indirect) projects into a "
        "managed section; once present, the section is kept up to date by "
        "every pin/bump run",
    )

    sp = add(
        "bump",
        cmd_bump,
        "bump projects along what they track: release-pinned to the latest "
        "release, branch/series-pinned to the tracked ref's head",
        dry_run=True,
        gh_token=True,
    )
    sp.add_argument("projects", nargs="*", help="projects to bump (default: all)")
    sp.add_argument("--ref", help="bump to this branch or tag explicitly")
    scope = sp.add_mutually_exclusive_group()
    scope.add_argument(
        "--patch",
        action="store_true",
        help="release-tracked: only updates within the same major.minor",
    )
    scope.add_argument(
        "--minor",
        action="store_true",
        help="release-tracked: only updates within the same major",
    )
    sp.add_argument(
        "--releases-only",
        action="store_true",
        help="skip projects that don't track a release tag or series",
    )

    sp = add(
        "check",
        cmd_check,
        "check that revisions are pinned, shas exist, and comments are "
        "consistent (all three by default; flags select a subset)",
        gh_token=True,
    )
    sp.add_argument(
        "--pinned",
        action="store_true",
        help="check that every revision is a full sha (offline)",
    )
    sp.add_argument(
        "--shas",
        action="store_true",
        help="check that pinned shas exist on their remote",
    )
    sp.add_argument(
        "--comments",
        action="store_true",
        help="check that pinned shas are in the history of their commented ref",
    )

    return ap


def _find_manifest(arg: Path | None) -> Path:
    """Locate the manifest: -f wins, then ./west.yml, then the enclosing
    west workspace's configured manifest."""
    if arg is not None:
        return arg
    cwd_manifest = Path("west.yml")
    if cwd_manifest.exists():
        return cwd_manifest
    try:
        topdir = west_topdir()
    except WestNotFound:
        raise ManifestError(
            "no west.yml in the current directory and not inside a west "
            "workspace; use -f/--manifest"
        )
    try:
        manifest = imports.configured_manifest(topdir)
    except MalformedConfig as e:
        raise ManifestError(
            f"west workspace at {topdir} has a broken configuration ({e}); "
            "use -f/--manifest"
        )
    if manifest is None:
        raise ManifestError(
            f"west workspace at {topdir} has no manifest.path configured; "
            "use -f/--manifest"
        )
    print(f"using workspace manifest: {manifest}")
    return manifest


def _load(args) -> tuple[ManifestFile, list]:
    mf = ManifestFile.load(_find_manifest(args.manifest))
    manifest = Manifest.from_data(mf.text(), import_flags=ImportFlag.IGNORE)
    projects = [p for p in manifest.projects if not isinstance(p, ManifestProject)]
    for p in projects:
        if p.name not in mf.blocks:
            raise ManifestError(f"could not locate project '{p.name}' in {mf.path}")
    return mf, projects


def _has_default_revision(mf: ManifestFile) -> bool:
    return "revision" in (imports.manifest_data(mf.text()).get("defaults") or {})


def _finish(mf: ManifestFile, original: str, args, errors: list[str]) -> int:
    for err in errors:
        print(f"error: {err}", file=sys.stderr)
    new = mf.text()
    if new == original:
        print("nothing to do")
    elif args.dry_run:
        sys.stdout.writelines(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=str(mf.path),
                tofile=str(mf.path),
            )
        )
    else:
        mf.path.write_text(new)
        print(f"updated {mf.path}")
    return 1 if errors else 0


def _comment_for(ref: str, kind: str) -> str:
    today = datetime.now(UTC).date().isoformat()
    return ref if kind == "tag" else f"{ref} ({today})"


def _resolution_ok(
    resolved: res.Resolved | None, name: str, url: str, ref: str, errors: list[str]
) -> TypeGuard[res.Resolved]:
    """Report an unresolved or ambiguous ref; True when resolved is usable."""
    if resolved is None:
        errors.append(f"{name}: ref '{ref}' not found on {url}")
        return False
    if resolved.ambiguous:
        print(f"warning: {name}: '{ref}' is both a branch and a tag; using the tag")
    return True


def _regenerate_imports(
    mf: ManifestFile,
    errors: list[str],
    token: str | None,
    scope: set[str] | None = None,
) -> ManifestFile:
    try:
        return imports.regenerate(mf, errors, scope=scope, token=token)
    except (ManifestError, res.ResolveError) as e:
        errors.append(str(e))
        return mf


def cmd_pin(args) -> int:
    mf, projects = _load(args)
    original = mf.text()
    frozen = _frozen_revisions(mf.path) if args.local else None
    has_default = _has_default_revision(mf)
    errors: list[str] = []

    for p in projects:
        if is_pinned(p.revision):
            continue
        ref = p.revision
        if frozen is not None:
            sha = frozen.get(p.name)
            if not is_pinned(sha):
                errors.append(
                    f"{p.name}: not resolved in the workspace (missing from its "
                    f"manifest, or not cloned — run `west update {p.name}`)"
                )
                continue
            mf.set_revision(p.name, sha, _comment_for(ref, "branch"))
            print(f"pin {p.name}: {ref} -> {sha[:12]} (workspace)")
            continue
        resolved = res.resolve_ref(p.url, ref)
        # west's implicit default; fall back to the remote's default branch
        if (
            resolved is None
            and ref == "master"
            and not has_default
            and (default := res.default_branch(p.url))
        ):
            branch = default[0]
            print(
                f"note: {p.name}: no revision given and no 'master' branch; "
                f"using default branch '{branch}'"
            )
            # resolve by name: a same-named tag must win, matching git/west
            ref, resolved = branch, res.resolve_ref(p.url, branch)
        if not _resolution_ok(resolved, p.name, p.url, ref, errors):
            continue
        mf.set_revision(p.name, resolved.sha, _comment_for(ref, resolved.kind))
        print(f"pin {p.name}: {ref} -> {resolved.sha[:12]} ({resolved.kind})")

    if args.include_imports or mf.has_generated_section:
        mf = _regenerate_imports(mf, errors, res.find_token(None))
    return _finish(mf, original, args, errors)


def _frozen_revisions(manifest_path: Path) -> dict[str, str | None]:
    """Revisions as resolved in the enclosing workspace: the sha of each
    project's manifest-rev ref, maintained by `west update` (the same source
    `west manifest --freeze` uses). None for projects not cloned/updated."""
    try:
        topdir = west_topdir(start=manifest_path.resolve().parent)
    except WestNotFound:
        raise ManifestError(
            f"--local: no initialized west workspace found above {manifest_path}"
        )
    try:
        manifest = Manifest.from_topdir(topdir=topdir, import_flags=ImportFlag.IGNORE)
    except (MalformedConfig, MalformedManifest) as e:
        raise ManifestError(
            f"--local: workspace at {topdir} is not usable ({e}); "
            "is it initialized and updated?"
        )
    frozen: dict[str, str | None] = {}
    for p in manifest.projects:
        if isinstance(p, ManifestProject):
            continue
        try:
            frozen[p.name] = p.sha(QUAL_MANIFEST_REV_BRANCH)
        except (subprocess.CalledProcessError, OSError):
            frozen[p.name] = None
    return frozen


def cmd_bump(args) -> int:
    if args.ref and (args.patch or args.minor or args.releases_only):
        raise ManifestError(
            "--ref cannot be combined with --patch/--minor/--releases-only"
        )
    scope = "patch" if args.patch else "minor" if args.minor else None
    mf, projects = _load(args)
    original = mf.text()
    selected = set(args.projects)
    known = {p.name for p in projects}
    if unknown := selected - known:
        raise ManifestError(f"unknown project(s): {', '.join(sorted(unknown))}")
    if managed := selected & mf.generated:
        raise ManifestError(
            f"{', '.join(sorted(managed))}: imported project(s) managed by "
            "pin-west; bump the importing project instead"
        )
    targets = [
        p
        for p in projects
        if (not selected or p.name in selected) and p.name not in mf.generated
    ]

    # only GitHub-remote targets without --ref can reach the GitHub API;
    # don't spend a `gh auth token` run otherwise
    has_github = any(res.github_repo(p.url) for p in targets)
    token = res.find_token(args.gh_token) if has_github and not args.ref else None
    if token is None and args.ref is None and scope is None and has_github:
        print(
            "warning: no GitHub token found (--gh-token, $GITHUB_TOKEN, or `gh auth login`); "
            "unauthenticated API requests are rate-limited to 60/hour",
            file=sys.stderr,
        )

    has_default = _has_default_revision(mf)
    errors: list[str] = []
    for p in targets:
        block = mf.blocks[p.name]
        # Classify on the effective revision (defaults included); ignore
        # west's implicit 'master' when nothing was actually declared.
        revision = p.revision
        if block.revision is None and not has_default:
            revision = None
        try:
            if args.ref:
                resolved = res.resolve_ref(p.url, args.ref)
                if _resolution_ok(resolved, p.name, p.url, args.ref, errors):
                    _apply(mf, p.name, args.ref, resolved.sha, resolved.kind)
                continue
            refs = res.remote_refs(p.url)
        except res.ResolveError as e:
            errors.append(str(e))
            continue

        track = tracking.classify(revision, block.comment, refs)
        if track.note:
            print(f"warning: {p.name}: {track.note}")

        if track.kind in ("branch", "other", "untracked") and args.releases_only:
            print(f"skip {p.name}: not release-tracked ({track.kind})")
        elif track.kind == "branch":
            assert track.ref is not None  # classification invariant
            if scope:
                print(
                    f"warning: --{scope} ignored for branch-tracked '{p.name}'; "
                    f"following branch '{track.ref}' (use --releases-only to "
                    "restrict the run to release-tracked projects)"
                )
            _apply(mf, p.name, track.ref, refs.branches[track.ref], "branch")
        elif track.kind == "floating":
            assert track.ref is not None and track.version is not None
            if scope == "patch" and len(track.version.release) < 2:
                print(
                    f"warning: {p.name}: '{track.ref}' floats at the major level; "
                    "--patch cannot constrain it — following the tag"
                )
            _apply(mf, p.name, track.ref, refs.tags[track.ref], "tag")
        elif track.kind == "release":
            _bump_release(mf, p, track, refs, scope, token)
        else:  # "other" / "untracked": today's fallback chain
            if scope:
                print(
                    f"warning: {p.name}: no base version to scope against; "
                    "bumping to latest"
                )
            try:
                ref, resolved = _latest_target(p.url, token, refs)
            except res.ResolveError as e:
                errors.append(str(e))
                continue
            if not _resolution_ok(resolved, p.name, p.url, ref, errors):
                continue
            _apply(mf, p.name, ref, resolved.sha, resolved.kind)

    if mf.has_generated_section:
        mf = _regenerate_imports(mf, errors, token, scope=selected or None)
    return _finish(mf, original, args, errors)


def _apply(mf: ManifestFile, name: str, ref: str, sha: str, kind: str) -> None:
    block = mf.blocks[name]
    if sha == block.revision:
        print(f"ok  {name}: already at {ref} ({sha[:12]})")
        return
    old = (block.revision or "?")[:12]
    mf.set_revision(name, sha, _comment_for(ref, kind))
    print(f"bump {name}: {old} -> {sha[:12]} ({kind} {ref})")


def _bump_release(mf, p, track, refs, scope, token) -> None:
    block = mf.blocks[p.name]
    # Moved-tag guard: an exact release tag declared by the comment should
    # still point at the pinned commit — release tags aren't supposed to move.
    moved = (
        track.via == "comment"
        and is_pinned(block.revision)
        and refs.tags.get(track.ref) not in (None, block.revision)
    )
    if moved:
        print(
            f"warning: {p.name}: tag '{track.ref}' no longer points at the pinned "
            f"commit ({block.revision[:12]} -> {refs.tags[track.ref][:12]}); "
            f"re-pin explicitly with --ref {track.ref} if this is expected"
        )

    release = version = None
    if scope is None and (gh := res.github_repo(p.url)):
        release = res.latest_release(*gh, token)
        version = tracking.parse_tag(release)
    if version is not None:  # the GitHub release marking wins over a tag scan
        target = None
        if version < track.version:
            print(
                f"skip {p.name}: latest release '{release}' is older than the "
                f"pinned {track.ref}; use --ref to downgrade"
            )
            return
        if version > track.version:
            target = release
        # equal: up to date per the release marking
    else:
        target = tracking.best_tag(
            tracking.release_candidates(
                track.version, tracking.parsed_tags(refs.tags), scope
            )
        )

    if target is not None:
        sha = refs.tags.get(target)
        if sha is None:  # release tag missing from the tag map (unusual)
            resolved = res.resolve_ref(p.url, target)
            sha = resolved.sha if resolved else None
        if sha:
            _apply(mf, p.name, target, sha, "tag")
            return
    if moved:
        print(f"skip {p.name}: holding pinned commit; tag '{track.ref}' moved")
    else:
        print(f"ok  {p.name}: up to date ({track.ref})")


def _latest_target(
    url: str, token: str | None, refs: res.RemoteRefs
) -> tuple[str, res.Resolved | None]:
    """Fallback for untracked projects: latest GitHub release, else highest
    version tag, else the remote's default branch."""
    if (gh := res.github_repo(url)) and (tag := res.latest_release(*gh, token)):
        if tag in refs.tags:
            return tag, res.Resolved(refs.tags[tag], "tag")
        return tag, res.resolve_ref(url, tag)
    if tag := tracking.highest_version_tag(refs.tags):
        return tag, res.Resolved(refs.tags[tag], "tag")
    default = res.default_branch(url)
    if default is None:
        raise res.ResolveError(
            f"{url}: no releases, version tags, or default branch found"
        )
    branch, sha = default
    return branch, res.Resolved(sha, "branch")


def cmd_check(args) -> int:
    flags = {"pinned": args.pinned, "shas": args.shas, "comments": args.comments}
    selected = {name for name, on in flags.items() if on}
    checks = selected or set(flags)
    mf, projects = _load(args)
    needs_network = checks & {"shas", "comments"}
    token = (
        res.find_token(args.gh_token)
        if needs_network and any(res.github_repo(p.url) for p in projects)
        else None
    )

    failures = 0
    for p in projects:
        if not is_pinned(p.revision):
            if "pinned" in checks:
                print(f"FAIL {p.name}: not pinned (revision: {p.revision})")
                failures += 1
            else:
                print(f"skip {p.name}: not pinned (revision: {p.revision})")
            continue

        ref = refs = None
        if "comments" in checks:
            ref = comment_ref(mf.blocks[p.name].comment)
            if ref and is_pinned(ref):
                ref = None  # comment repeats a sha; nothing to cross-check
            if ref:
                try:
                    refs = res.remote_refs(p.url)
                except res.ResolveError:
                    refs = None  # reported as unverifiable by _check_comment
        problem = note = None
        if "shas" in checks and not res.commit_exists(p.url, p.revision, token, refs):
            problem = f"commit {p.revision[:12]} not found on {p.url}"
        elif ref:
            problem, note = _check_comment(p, ref, refs, token)

        if problem:
            print(f"FAIL {p.name}: {problem}")
            failures += 1
        else:
            detail = f" ({note})" if note else ""
            print(f"ok   {p.name}: {p.revision[:12]}{detail}")

    if failures:
        print(f"{failures} of {len(projects)} project(s) failed checks")
        return 1
    print(f"all {len(projects)} project(s) passed checks")
    return 0


def _check_comment(
    p, ref: str, refs: res.RemoteRefs | None, token: str | None
) -> tuple[str | None, str | None]:
    """Verify a pinned project against its commented ref. The claim depends
    on the ref's type: exact tag -> identity, floating series alias ->
    membership in the series, branch -> ancestry (GitHub only, best-effort).
    Returns (problem, note); at most one is set."""
    if refs is None:
        return None, f"'{ref}' not verifiable"
    if ref in refs.tags:  # tag wins over a same-named branch, matching git/west
        ver = tracking.parse_tag(ref)
        parsed = tracking.parsed_tags(refs.tags)
        if ver is not None and tracking.is_floating(ver, parsed):
            series = {
                refs.tags[t]
                for t, v in parsed.items()
                if tracking.same_series(v, ver.release)
            }
            if p.revision in series:
                return None, f"in the '{ref}' series"
            return f"{p.revision[:12]} is not in the '{ref}' series", None
        if refs.tags[ref] == p.revision:
            return None, f"at tag '{ref}'"
        return (
            f"tag '{ref}' points at {refs.tags[ref][:12]}, not the pinned commit",
            None,
        )
    if ref in refs.branches:
        if refs.branches[ref] == p.revision:
            return None, f"on '{ref}'"
        on_ref = res.sha_on_ref(p.url, p.revision, ref, token)
        if on_ref is False:
            return f"{p.revision[:12]} is not in the history of '{ref}'", None
        if on_ref is None:
            return None, f"'{ref}' not verifiable"
        return None, f"on '{ref}'"
    return f"comment ref '{ref}' not found on the remote", None
