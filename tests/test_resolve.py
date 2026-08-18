"""Tests for ref resolution: pure helpers, plus ls-remote against local repos."""

import pytest

from pin_west import resolve as res


class TestHighestVersionTag:
    def test_orders_versions(self):
        tags = {"v0.9.0": "x", "v0.10.0": "x", "v0.2.0": "x"}
        assert res.highest_version_tag(tags) == "v0.10.0"

    def test_prefers_stable_over_prerelease(self):
        tags = {"v1.0.0": "x", "v2.0.0rc1": "x"}
        assert res.highest_version_tag(tags) == "v1.0.0"

    def test_all_prereleases(self):
        tags = {"v1.0.0rc1": "x", "v1.0.0rc2": "x"}
        assert res.highest_version_tag(tags) == "v1.0.0rc2"

    def test_ignores_unparseable(self):
        tags = {"nightly": "x", "v1.0": "x"}
        assert res.highest_version_tag(tags) == "v1.0"

    def test_no_version_tags(self):
        assert res.highest_version_tag({"nightly": "x"}) is None
        assert res.highest_version_tag({}) is None


class TestGithubRepo:
    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/urob/zmk-helpers",
            "https://github.com/urob/zmk-helpers.git",
            "https://github.com/urob/zmk-helpers/",
            "git@github.com:urob/zmk-helpers.git",
            "ssh://git@github.com/urob/zmk-helpers",
        ],
    )
    def test_matches(self, url):
        assert res.github_repo(url) == ("urob", "zmk-helpers")

    @pytest.mark.parametrize(
        "url",
        ["https://gitlab.com/urob/zmk-helpers", "/local/path/repo", ""],
    )
    def test_non_github(self, url):
        assert res.github_repo(url) is None


class TestLsRemote:
    def test_resolve_branch(self, remote):
        repo = remote()
        repo.branch("dev")
        head = repo.commit("on main")
        resolved = res.resolve_ref(repo.url, "main")
        assert resolved == res.Resolved(head, "branch", False)
        dev = res.resolve_ref(repo.url, "dev")
        assert dev is not None and dev.sha != head

    def test_resolve_lightweight_tag(self, remote):
        repo = remote()
        repo.tag("v1.0.0")
        resolved = res.resolve_ref(repo.url, "v1.0.0")
        assert resolved == res.Resolved(repo.sha(), "tag", False)

    def test_resolve_annotated_tag_is_peeled(self, remote):
        repo = remote()
        repo.tag("v1.0.0", annotated=True)
        resolved = res.resolve_ref(repo.url, "v1.0.0")
        assert resolved is not None
        assert resolved.sha == repo.sha()  # commit sha, not the tag object's
        assert resolved.kind == "tag"

    def test_ambiguous_prefers_tag(self, remote):
        repo = remote()
        repo.tag("dev")
        tagged = repo.sha()
        repo.commit("newer")
        repo.branch("dev")
        resolved = res.resolve_ref(repo.url, "dev")
        assert resolved == res.Resolved(tagged, "tag", ambiguous=True)

    def test_missing_ref(self, remote):
        assert res.resolve_ref(remote().url, "nope") is None

    def test_bad_remote(self, tmp_path):
        with pytest.raises(res.ResolveError):
            res.resolve_ref(str(tmp_path / "nonexistent"), "main")

    def test_default_branch(self, remote):
        repo = remote()
        assert res.default_branch(repo.url) == "main"

    def test_remote_tags_peels(self, remote):
        repo = remote()
        first = repo.sha()
        repo.tag("light")
        repo.commit("second")
        repo.tag("annot", annotated=True)
        tags = res.remote_tags(repo.url)
        assert tags == {"light": first, "annot": repo.sha()}


class TestCommitExists:
    def test_existing_and_missing(self, remote):
        repo = remote()
        assert res.commit_exists(repo.url, repo.sha(), token=None)
        assert not res.commit_exists(repo.url, "deadbeef" * 5, token=None)
