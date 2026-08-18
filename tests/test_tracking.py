"""Unit tests for tracking-state classification and candidate selection."""

from pin_west.resolve import RemoteRefs
from pin_west.tracking import (
    best_tag,
    classify,
    is_floating,
    parse_tag,
    parsed_tags,
    release_candidates,
)

SHA1, SHA2, SHA3, SHA4 = ("1" * 40, "2" * 40, "3" * 40, "4" * 40)

REFS = RemoteRefs(
    branches={"main": SHA4, "dev": SHA4},
    tags={"v0.3": SHA1, "v0.3.0": SHA1, "v0.3.1": SHA2, "v1.0.0": SHA3, "dev": SHA2},
)


class TestClassify:
    def test_unpinned_branch(self):
        track = classify("main", None, REFS)
        assert (track.kind, track.ref, track.via) == ("branch", "main", "revision")

    def test_unpinned_exact_tag(self):
        track = classify("v0.3.1", None, REFS)
        assert (track.kind, track.ref) == ("release", "v0.3.1")
        assert track.version == parse_tag("v0.3.1")

    def test_unpinned_floating_tag(self):
        track = classify("v0.3", None, REFS)
        assert (track.kind, track.ref) == ("floating", "v0.3")

    def test_unpinned_unresolvable(self):
        track = classify("gone", None, REFS)
        assert track.kind == "untracked"
        assert "not found" in track.note

    def test_comment_exact_tag(self):
        track = classify(SHA3, "v1.0.0", REFS)
        assert (track.kind, track.ref, track.via) == ("release", "v1.0.0", "comment")

    def test_comment_branch_with_date(self):
        track = classify(SHA4, "main (2026-08-17)", REFS)
        assert (track.kind, track.ref, track.via) == ("branch", "main", "comment")

    def test_ambiguous_name_prefers_tag(self):
        track = classify(SHA2, "dev", REFS)
        assert track.kind == "other"  # 'dev' is a tag but not version-parseable
        assert "both a branch and a tag" in track.note

    def test_stale_comment_falls_back_to_inference(self):
        track = classify(SHA3, "v9.9.9", REFS)
        assert (track.kind, track.ref, track.via) == ("release", "v1.0.0", "tag-match")
        assert "not found" in track.note

    def test_inference_prefers_specific_tag(self):
        # SHA1 matches both v0.3 (alias) and v0.3.0 (exact): pick the exact one
        track = classify(SHA1, None, REFS)
        assert (track.kind, track.ref, track.via) == ("release", "v0.3.0", "tag-match")

    def test_untracked(self):
        track = classify("f" * 40, None, REFS)
        assert track.kind == "untracked"

    def test_sha_comment_is_ignored(self):
        track = classify(SHA3, "a" * 40, REFS)
        assert (track.kind, track.via) == ("release", "tag-match")


class TestFloating:
    def test_alias_with_refinements(self):
        parsed = parsed_tags(REFS.tags)
        assert is_floating(parse_tag("v0.3"), parsed)

    def test_full_tag_never_floats(self):
        parsed = parsed_tags(REFS.tags)
        assert not is_floating(parse_tag("v0.3.0"), parsed)

    def test_short_tag_without_refinements_is_exact(self):
        parsed = parsed_tags({"v0.3": "x", "v1.0.0": "y"})
        assert not is_floating(parse_tag("v0.3"), parsed)

    def test_major_alias(self):
        parsed = parsed_tags({"v1": "x", "v1.2.0": "y"})
        assert is_floating(parse_tag("v1"), parsed)


class TestCandidates:
    @staticmethod
    def parsed():
        return parsed_tags(
            dict.fromkeys(["v0.9.0", "v0.9.1", "v0.10.0", "v1.0.0", "v1.1.0rc1"])
        )

    def test_unscoped_excludes_prereleases_and_older(self):
        got = release_candidates(parse_tag("v0.9.0"), self.parsed(), None)
        assert set(got) == {"v0.9.1", "v0.10.0", "v1.0.0"}

    def test_patch_scope(self):
        got = release_candidates(parse_tag("v0.9.0"), self.parsed(), "patch")
        assert set(got) == {"v0.9.1"}

    def test_minor_scope(self):
        got = release_candidates(parse_tag("v0.9.0"), self.parsed(), "minor")
        assert set(got) == {"v0.9.1", "v0.10.0"}

    def test_best_tag_prefers_specific_on_tie(self):
        assert best_tag(parsed_tags({"v0.3": "x", "v0.3.0": "x"})) == "v0.3.0"
