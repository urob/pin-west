"""End-to-end tests for pin/bump/check against local git remotes."""

import re
import shutil
import subprocess
from textwrap import dedent

import pytest

from pin_west import resolve as res
from pin_west.cli import main

DATE = r"\d{4}-\d{2}-\d{2}"


def project_manifest(repo, revision: str | None = "main", defaults: str = "") -> str:
    revision_line = f"      revision: {revision}\n" if revision else ""
    return (
        f"manifest:\n{defaults}"
        f"  projects:\n"
        f"    - name: proj\n"
        f"      url: {repo.url}\n"
        f"{revision_line}"
    )


class TestPin:
    def test_pin_branch(self, remote, write_manifest):
        repo = remote()
        head = repo.commit("newer")
        path = write_manifest(project_manifest(repo, "main"))
        assert main(["pin", "-f", str(path)]) == 0
        assert re.search(rf"revision: {head} # main \({DATE}\)", path.read_text())

    def test_pin_tag_comment_has_no_date(self, remote, write_manifest):
        repo = remote()
        repo.tag("v1.0.0", annotated=True)
        path = write_manifest(project_manifest(repo, "v1.0.0"))
        assert main(["pin", "-f", str(path)]) == 0
        assert f"revision: {repo.sha()} # v1.0.0\n" in path.read_text()

    def test_already_pinned_is_untouched(self, remote, write_manifest, capsys):
        repo = remote()
        path = write_manifest(project_manifest(repo, "a" * 40))
        before = path.read_text()
        assert main(["pin", "-f", str(path)]) == 0
        assert path.read_text() == before
        assert "nothing to do" in capsys.readouterr().out

    def test_missing_revision_falls_back_to_default_branch(
        self, remote, write_manifest, capsys
    ):
        # no revision line and no defaults: west implies 'master', which the
        # remote doesn't have; pin should use the default branch instead
        repo = remote()
        path = write_manifest(project_manifest(repo, revision=None))
        assert main(["pin", "-f", str(path)]) == 0
        assert "using default branch 'main'" in capsys.readouterr().out
        lines = path.read_text().splitlines()
        assert lines[2] == "    - name: proj"
        assert re.fullmatch(
            rf"      revision: {repo.sha()} # main \({DATE}\)", lines[3]
        )

    def test_missing_revision_uses_defaults(self, remote, write_manifest):
        repo = remote()
        repo.branch("stable")
        repo.commit("past stable")
        path = write_manifest(
            project_manifest(
                repo, revision=None, defaults="  defaults:\n    revision: stable\n"
            )
        )
        assert main(["pin", "-f", str(path)]) == 0
        assert re.search(
            rf"revision: {repo.sha('stable')} # stable \({DATE}\)", path.read_text()
        )

    def test_dry_run_writes_nothing(self, remote, write_manifest, capsys):
        repo = remote()
        path = write_manifest(project_manifest(repo, "main"))
        before = path.read_text()
        assert main(["pin", "--dry-run", "-f", str(path)]) == 0
        assert path.read_text() == before
        out = capsys.readouterr().out
        assert f"+      revision: {repo.sha()}" in out

    def test_unresolvable_ref_errors(self, remote, write_manifest, capsys):
        path = write_manifest(project_manifest(remote(), "no-such-ref"))
        before = path.read_text()
        assert main(["pin", "-f", str(path)]) == 1
        assert path.read_text() == before
        assert "ref 'no-such-ref' not found" in capsys.readouterr().err


class TestCheck:
    def test_unpinned_fails(self, remote, write_manifest, capsys):
        path = write_manifest(project_manifest(remote(), "main"))
        assert main(["check", "--gh-token", "dummy", "-f", str(path)]) == 1
        assert "FAIL proj: not pinned" in capsys.readouterr().out

    def test_all_checks_pass(self, remote, write_manifest, capsys):
        repo = remote()
        path = write_manifest(
            project_manifest(repo, f"{repo.sha()} # main (2026-08-17)")
        )
        assert main(["check", "--gh-token", "dummy", "-f", str(path)]) == 0
        out = capsys.readouterr().out
        # local remotes aren't GitHub, so the ancestry check reports unverifiable
        assert f"ok   proj: {repo.sha()[:12]} ('main' not verifiable)" in out

    def test_pinned_only_is_offline(self, remote, write_manifest):
        # a bogus sha passes --pinned (no network) but fails the default checks
        path = write_manifest(project_manifest(remote(), "a" * 40))
        assert main(["check", "--pinned", "-f", str(path)]) == 0
        assert main(["check", "--gh-token", "dummy", "-f", str(path)]) == 1

    def test_missing_commit_fails(self, remote, write_manifest, capsys):
        path = write_manifest(project_manifest(remote(), "deadbeef" * 5))
        assert main(["check", "--gh-token", "dummy", "-f", str(path)]) == 1
        assert "not found on" in capsys.readouterr().out

    def test_comment_ancestry(self, remote, write_manifest, capsys, monkeypatch):
        repo = remote()
        path = write_manifest(
            project_manifest(repo, f"{repo.sha()} # main (2026-08-17)")
        )
        monkeypatch.setattr(res, "sha_on_ref", lambda url, sha, ref, token: True)
        assert (
            main(["check", "--comments", "--gh-token", "dummy", "-f", str(path)]) == 0
        )
        assert "(on 'main')" in capsys.readouterr().out
        monkeypatch.setattr(res, "sha_on_ref", lambda url, sha, ref, token: False)
        assert (
            main(["check", "--comments", "--gh-token", "dummy", "-f", str(path)]) == 1
        )
        assert "not in the history of 'main'" in capsys.readouterr().out

    def test_unpinned_skipped_when_pinned_not_selected(
        self, remote, write_manifest, capsys
    ):
        path = write_manifest(project_manifest(remote(), "main"))
        assert (
            main(["check", "--comments", "--gh-token", "dummy", "-f", str(path)]) == 0
        )
        assert "skip proj: not pinned" in capsys.readouterr().out

    def test_comment_exact_tag_identity(self, remote, write_manifest, capsys):
        # works offline for any git host: identity against the peeled tag sha
        repo = remote()
        pinned = repo.sha()
        repo.tag("v1.0.0", annotated=True)
        path = write_manifest(project_manifest(repo, f"{pinned} # v1.0.0"))
        assert (
            main(["check", "--comments", "--gh-token", "dummy", "-f", str(path)]) == 0
        )
        assert "(at tag 'v1.0.0')" in capsys.readouterr().out
        repo.commit("rewritten")
        repo.git("tag", "-f", "v1.0.0")
        assert (
            main(["check", "--comments", "--gh-token", "dummy", "-f", str(path)]) == 1
        )
        assert "tag 'v1.0.0' points at" in capsys.readouterr().out

    def test_comment_floating_series_membership(self, remote, write_manifest, capsys):
        repo = remote()
        old = repo.sha()
        repo.tag("v0.3")
        repo.tag("v0.3.0")
        repo.commit("newer in series")
        repo.tag("v0.3.1")
        repo.git("tag", "-f", "v0.3")
        # pinned at an older series member while the alias moved on: still ok
        path = write_manifest(project_manifest(repo, f"{old} # v0.3"))
        assert (
            main(["check", "--comments", "--gh-token", "dummy", "-f", str(path)]) == 0
        )
        assert "(in the 'v0.3' series)" in capsys.readouterr().out
        # a commit foreign to the series fails
        foreign = repo.commit("untagged")
        path = write_manifest(project_manifest(repo, f"{foreign} # v0.3"))
        assert (
            main(["check", "--comments", "--gh-token", "dummy", "-f", str(path)]) == 1
        )
        assert "is not in the 'v0.3' series" in capsys.readouterr().out

    def test_comment_ref_missing_fails(self, remote, write_manifest, capsys):
        repo = remote()
        path = write_manifest(project_manifest(repo, f"{repo.sha()} # gone-ref"))
        assert (
            main(["check", "--comments", "--gh-token", "dummy", "-f", str(path)]) == 1
        )
        assert "comment ref 'gone-ref' not found" in capsys.readouterr().out


class TestBump:
    def test_explicit_ref(self, remote, write_manifest):
        repo = remote()
        repo.tag("v1.0.0")
        path = write_manifest(project_manifest(repo, "b" * 40))
        assert main(["bump", "--ref", "v1.0.0", "-f", str(path)]) == 0
        assert f"revision: {repo.sha()} # v1.0.0\n" in path.read_text()

    def test_idempotent(self, remote, write_manifest, capsys):
        repo = remote()
        repo.tag("v1.0.0")
        path = write_manifest(project_manifest(repo, repo.sha()))
        assert main(["bump", "--ref", "v1.0.0", "-f", str(path)]) == 0
        assert "already at v1.0.0" in capsys.readouterr().out

    def test_default_picks_highest_stable_tag(self, remote, write_manifest):
        repo = remote()
        v9 = repo.sha()
        repo.tag("v0.9.0")
        v10 = repo.commit("v0.10")
        repo.tag("v0.10.0")
        repo.commit("rc")
        repo.tag("v0.11.0rc1")
        path = write_manifest(project_manifest(repo, f"{v9} # v0.9.0"))
        assert main(["bump", "-f", str(path)]) == 0
        assert f"revision: {v10} # v0.10.0\n" in path.read_text()

    def test_default_without_tags_uses_default_branch(self, remote, write_manifest):
        repo = remote()
        path = write_manifest(project_manifest(repo, "b" * 40))
        assert main(["bump", "-f", str(path)]) == 0
        assert re.search(rf"revision: {repo.sha()} # main \({DATE}\)", path.read_text())

    def test_github_release_path(self, remote, write_manifest, monkeypatch):
        # untracked pin: the fallback chain consults the GitHub release marking
        repo = remote()
        repo.tag("v1.0.0")
        released = repo.sha()
        repo.commit("past release")
        repo.tag("v2.0.0")  # higher tag exists, but the release marking wins
        monkeypatch.setattr(res, "github_repo", lambda url: ("owner", "repo"))
        monkeypatch.setattr(res, "latest_release", lambda owner, repo_, token: "v1.0.0")
        path = write_manifest(project_manifest(repo, "b" * 40))
        assert main(["bump", "--gh-token", "dummy", "-f", str(path)]) == 0
        assert f"revision: {released} # v1.0.0\n" in path.read_text()

    def test_unknown_project(self, remote, write_manifest, capsys):
        path = write_manifest(project_manifest(remote(), "main"))
        assert main(["bump", "nope", "-f", str(path)]) == 1
        assert "unknown project" in capsys.readouterr().err

    def test_ref_conflicts_with_scope_flags(self, remote, write_manifest, capsys):
        path = write_manifest(project_manifest(remote(), "main"))
        assert main(["bump", "--ref", "v1", "--patch", "-f", str(path)]) == 1
        assert "--ref cannot be combined" in capsys.readouterr().err

    def test_patch_scope(self, remote, write_manifest):
        repo = remote()
        v9 = repo.sha()
        repo.tag("v0.9.0")
        v91 = repo.commit("patch")
        repo.tag("v0.9.1")
        repo.commit("minor")
        repo.tag("v0.10.0")
        path = write_manifest(project_manifest(repo, f"{v9} # v0.9.0"))
        assert main(["bump", "--patch", "-f", str(path)]) == 0
        assert f"revision: {v91} # v0.9.1\n" in path.read_text()

    def test_minor_scope(self, remote, write_manifest):
        repo = remote()
        v9 = repo.sha()
        repo.tag("v0.9.0")
        v10 = repo.commit("minor")
        repo.tag("v0.10.0")
        repo.commit("major")
        repo.tag("v1.0.0")
        path = write_manifest(project_manifest(repo, f"{v9} # v0.9.0"))
        assert main(["bump", "--minor", "-f", str(path)]) == 0
        assert f"revision: {v10} # v0.10.0\n" in path.read_text()

    def test_release_up_to_date(self, remote, write_manifest, capsys):
        repo = remote()
        repo.tag("v1.0.0")
        path = write_manifest(project_manifest(repo, f"{repo.sha()} # v1.0.0"))
        assert main(["bump", "-f", str(path)]) == 0
        assert "up to date (v1.0.0)" in capsys.readouterr().out

    def test_branch_tracked_follows_branch_not_releases(self, remote, write_manifest):
        # declared branch intent wins over available release tags
        repo = remote()
        old = repo.sha()
        repo.tag("v1.0.0")
        head = repo.commit("past release")
        path = write_manifest(project_manifest(repo, f"{old} # main (2026-01-01)"))
        assert main(["bump", "-f", str(path)]) == 0
        assert re.search(rf"revision: {head} # main \({DATE}\)", path.read_text())

    def test_branch_tracked_scope_warns_and_follows(
        self, remote, write_manifest, capsys
    ):
        repo = remote()
        old = repo.sha()
        head = repo.commit("newer")
        path = write_manifest(project_manifest(repo, f"{old} # main (2026-01-01)"))
        assert main(["bump", "--patch", "-f", str(path)]) == 0
        out = capsys.readouterr().out
        assert "--patch ignored for branch-tracked 'proj'" in out
        assert "--releases-only" in out
        assert head in path.read_text()

    def test_releases_only_skips_branch_tracked(self, remote, write_manifest, capsys):
        repo = remote()
        old = repo.sha()
        repo.commit("newer")
        path = write_manifest(project_manifest(repo, f"{old} # main (2026-01-01)"))
        before = path.read_text()
        assert main(["bump", "--releases-only", "-f", str(path)]) == 0
        assert "skip proj: not release-tracked (branch)" in capsys.readouterr().out
        assert path.read_text() == before

    def test_floating_alias_follows_moved_tag(self, remote, write_manifest, capsys):
        repo = remote()
        old = repo.sha()
        repo.tag("v0.3")
        repo.tag("v0.3.0")
        new = repo.commit("v0.3.1")
        repo.tag("v0.3.1")
        repo.git("tag", "-f", "v0.3")  # alias moved along the series: normal
        path = write_manifest(project_manifest(repo, f"{old} # v0.3"))
        assert main(["bump", "-f", str(path)]) == 0
        out = capsys.readouterr().out
        assert "no longer points" not in out  # moved-tag guard must not fire
        assert f"revision: {new} # v0.3\n" in path.read_text()  # comment kept

    def test_moved_exact_tag_warns_and_holds(self, remote, write_manifest, capsys):
        repo = remote()
        old = repo.sha()
        repo.tag("v0.3.0")
        repo.commit("rewritten")
        repo.git("tag", "-f", "v0.3.0")  # exact release tag moved: suspicious
        path = write_manifest(project_manifest(repo, f"{old} # v0.3.0"))
        before = path.read_text()
        assert main(["bump", "-f", str(path)]) == 0
        out = capsys.readouterr().out
        assert "tag 'v0.3.0' no longer points at the pinned commit" in out
        assert "holding pinned commit" in out
        assert path.read_text() == before

    def test_downgrade_guard(self, remote, write_manifest, capsys, monkeypatch):
        repo = remote()
        repo.tag("v1.0.0")
        cur = repo.commit("v2")
        repo.tag("v2.0.0")
        monkeypatch.setattr(res, "github_repo", lambda url: ("owner", "repo"))
        monkeypatch.setattr(res, "latest_release", lambda owner, repo_, token: "v1.0.0")
        path = write_manifest(project_manifest(repo, f"{cur} # v2.0.0"))
        before = path.read_text()
        assert main(["bump", "--gh-token", "dummy", "-f", str(path)]) == 0
        assert "older than the pinned v2.0.0" in capsys.readouterr().out
        assert path.read_text() == before

    def test_defaults_revision_declares_tracking(self, remote, write_manifest):
        # a floating alias inherited from defaults: follow it, keep the comment
        repo = remote()
        repo.tag("v0.3.0")
        new = repo.commit("newer in series")
        repo.tag("v0.3.1")
        repo.git("tag", "-f", "v0.3")
        path = write_manifest(
            project_manifest(
                repo, revision=None, defaults="  defaults:\n    revision: v0.3\n"
            )
        )
        assert main(["bump", "-f", str(path)]) == 0
        assert f"revision: {new} # v0.3\n" in path.read_text()

    def test_sha_inference_recovers_release_tracking(self, remote, write_manifest):
        # no comment at all: the pinned sha matches a release tag
        repo = remote()
        v9 = repo.sha()
        repo.tag("v0.9.0")
        v10 = repo.commit("next")
        repo.tag("v0.10.0")
        path = write_manifest(project_manifest(repo, v9))
        assert main(["bump", "--patch", "-f", str(path)]) == 0
        # patch scope from inferred v0.9.0: no v0.9.x candidates -> up to date
        assert v9 in path.read_text()
        assert main(["bump", "--minor", "-f", str(path)]) == 0
        assert f"revision: {v10} # v0.10.0\n" in path.read_text()


class TestManifestDetection:
    def test_finds_west_yml_in_cwd(self, remote, write_manifest, monkeypatch):
        repo = remote()
        path = write_manifest(project_manifest(repo, "a" * 40))
        monkeypatch.chdir(path.parent)
        assert main(["check", "--pinned"]) == 0

    def test_no_manifest_anywhere(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        assert main(["check", "--pinned"]) == 1
        assert "no west.yml" in capsys.readouterr().err


@pytest.mark.skipif(shutil.which("west") is None, reason="west CLI not on PATH")
class TestLocalWorkspace:
    def test_pin_local_uses_workspace_state(
        self, remote, tmp_path, monkeypatch, capsys
    ):
        repo = remote()
        updated = repo.commit("state at west update")
        ws = tmp_path / "ws"
        (ws / "config").mkdir(parents=True)
        (ws / "config" / "west.yml").write_text(
            dedent(f"""\
                manifest:
                  projects:
                    - name: proj
                      url: {repo.url}
                      revision: main
                  self:
                    path: config
            """)
        )
        for cmd in (["west", "init", "-l", "config"], ["west", "update"]):
            subprocess.run(cmd, cwd=ws, check=True, capture_output=True, text=True)
        newer = repo.commit("after west update")

        # no -f: exercises workspace manifest detection too
        monkeypatch.chdir(ws)
        assert main(["pin", "--local"]) == 0
        out = capsys.readouterr().out
        assert "using workspace manifest" in out
        text = (ws / "config" / "west.yml").read_text()
        assert updated in text  # pinned to the workspace's manifest-rev ...
        assert newer not in text  # ... not the remote's current head
