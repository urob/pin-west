"""Tracking-state classification: what does a project's pin follow?

Implements the bump decision spec. A pin tracks one of:

- an exact release tag ("release": v0.3.0 — immutable by convention),
- a floating series alias ("floating": v0.3 moving along v0.3.x),
- a branch ("branch"),
- a version-unparseable tag ("other"), or
- nothing we can name ("untracked").

Declared intent wins: the manifest revision field for unpinned projects,
else the trailing comment. Sha<->tag inference is the recovery path when
neither declares anything. Names that are both a branch and a tag resolve
to the tag, matching how git (and therefore `west update`) disambiguates.
"""

from __future__ import annotations

from dataclasses import dataclass

from packaging.version import InvalidVersion, Version

from .manifest import comment_ref, is_pinned
from .resolve import RemoteRefs


@dataclass
class Track:
    kind: str  # "release" | "floating" | "branch" | "other" | "untracked"
    ref: str | None = None
    version: Version | None = None
    via: str = ""  # "revision" | "comment" | "tag-match" | ""
    note: str | None = None


def parse_tag(tag: str | None) -> Version | None:
    if tag is None:
        return None
    try:
        return Version(tag.lstrip("vV"))
    except InvalidVersion:
        return None


def parsed_tags(tags: dict[str, str]) -> dict[str, Version]:
    return {t: v for t in tags if (v := parse_tag(t)) is not None}


def is_floating(ver: Version, parsed: dict[str, Version]) -> bool:
    """A short tag (v0.3, v1) is a floating series alias iff more specific
    tags exist in its series (any v0.3.x)."""
    n = len(ver.release)
    if n >= 3:
        return False
    return any(
        len(v.release) > n and v.release[:n] == ver.release for v in parsed.values()
    )


def best_tag(candidates: dict[str, Version]) -> str | None:
    """Highest version; ties broken by specificity (v0.3.0 over v0.3), then
    tag name."""
    if not candidates:
        return None
    return max(candidates.items(), key=lambda kv: (kv[1], len(kv[1].release), kv[0]))[0]


def release_candidates(
    current: Version, parsed: dict[str, Version], scope: str | None
) -> dict[str, Version]:
    """Stable tags newer than current; with scope, only within the same
    major.minor ("patch") or major ("minor") series."""
    n = {"patch": 2, "minor": 1}.get(scope)
    prefix = current.release[:n] if n else None
    return {
        t: v
        for t, v in parsed.items()
        if not v.is_prerelease
        and v > current
        and (prefix is None or v.release[: len(prefix)] == prefix)
    }


def classify(revision: str | None, comment: str | None, refs: RemoteRefs) -> Track:
    if revision and not is_pinned(revision):
        track = _track_for_name(revision, refs, "revision")
        if track is not None:
            return track
        return Track(
            "untracked",
            via="revision",
            note=f"revision '{revision}' not found on the remote",
        )
    ref = comment_ref(comment)
    if ref and not is_pinned(ref):
        track = _track_for_name(ref, refs, "comment")
        if track is not None:
            return track
        track = _infer(revision, refs)
        track.note = f"comment ref '{ref}' not found on the remote; ignoring it"
        return track
    return _infer(revision, refs)


def _track_for_name(name: str, refs: RemoteRefs, via: str) -> Track | None:
    if name in refs.tags:
        ver = parse_tag(name)
        if ver is None:
            track = Track("other", name, via=via)
        else:
            kind = "floating" if is_floating(ver, parsed_tags(refs.tags)) else "release"
            track = Track(kind, name, ver, via)
        if name in refs.branches:
            track.note = (
                f"'{name}' is both a branch and a tag; using the tag "
                "(matching git/west resolution)"
            )
        return track
    if name in refs.branches:
        return Track("branch", name, via=via)
    return None


def _infer(revision: str | None, refs: RemoteRefs) -> Track:
    if revision:
        matching = {
            t: v for t, v in parsed_tags(refs.tags).items() if refs.tags[t] == revision
        }
        best = best_tag(matching)
        if best is not None and (track := _track_for_name(best, refs, "tag-match")):
            return track
        for tag, sha in refs.tags.items():
            if sha == revision:  # matches only version-unparseable tags
                return Track("other", tag, via="tag-match")
    return Track("untracked")
