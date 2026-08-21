"""End-to-end tests for --include-imports: materializing and pinning
imported (indirect) projects into the managed section."""

import shutil
from textwrap import dedent

import pytest
from conftest import GitRemote

from pin_west.cli import main
from pin_west.manifest import GENERATED_BEGIN, GENERATED_END, ManifestFile
from pin_west.resolve import fetch_blob, list_dir


def sub_manifest(child, shadowed=None) -> str:
    text = dedent(f"""\
        manifest:
          projects:
            - name: child
              url: {child.url}
              revision: main
              path: modules/child
              groups: [extras]
    """)
    if shadowed is not None:
        text += (
            f"    - name: shadowed\n      url: {shadowed.url}\n      revision: v9.9\n"
        )
    return text


def dep_manifest(repo, name, revision="main") -> str:
    return dedent(f"""\
        manifest:
          projects:
            - name: {name}
              url: {repo.url}
              revision: {revision}
    """)


def two_parents(parent_a, parent_b) -> str:
    return dedent(f"""\
        manifest:
          projects:
            - name: parentA
              url: {parent_a.url}
              revision: main
              import: a/west.yml
            - name: parentB
              url: {parent_b.url}
              revision: main
              import: b/west.yml
    """)


def top_manifest(parent, shadowed) -> str:
    return dedent(f"""\
        manifest:
          projects:
            - name: parent
              url: {parent.url}
              revision: main
              import: sub/west.yml
            - name: shadowed
              url: {shadowed.url}
              revision: {"c" * 40} # main (2026-01-01)
          self:
            path: config
    """)


class TestFetchBlob:
    def test_fetch_at_sha_and_branch(self, remote):
        repo = remote()
        sha = repo.commit_file("a/b.yml", "hello: 1\n")
        assert fetch_blob(repo.url, sha, "a/b.yml") == "hello: 1\n"
        assert fetch_blob(repo.url, "main", "a/b.yml") == "hello: 1\n"

    def test_missing_file(self, remote):
        repo = remote()
        assert fetch_blob(repo.url, repo.sha(), "nope.yml") is None

    def test_list_dir(self, remote):
        repo = remote()
        repo.commit_file("d/b.yml", "b: 1\n")
        sha = repo.commit_file("d/a.yml", "a: 1\n")
        assert sorted(list_dir(repo.url, sha, "d") or []) == ["a.yml", "b.yml"]
        assert list_dir(repo.url, sha, "d/a.yml") is None  # a file
        assert list_dir(repo.url, sha, "nope") is None


class TestIncludeImports:
    def test_pin_materializes_and_pins_imports(self, remote, write_manifest, capsys):
        parent, child, shadowed = remote("parent"), remote("child"), remote("shadowed")
        child_head = child.commit("child head")
        parent.commit_file("sub/west.yml", sub_manifest(child, shadowed))
        path = write_manifest(top_manifest(parent, shadowed))

        assert main(["pin", "--include-imports", "-f", str(path)]) == 0
        out = capsys.readouterr().out
        text = path.read_text()

        # direct dep pinned as usual; child materialized, pinned, self-contained
        assert f"revision: {parent.sha()} # main" in text
        assert GENERATED_BEGIN in text and GENERATED_END in text
        assert "- name: child # via parent (sub/west.yml)" in text
        assert f"revision: {child_head}\n" in text  # no tracking comment
        assert "path: modules/child" in text
        assert "groups: [extras]" in text
        # the user's own 'shadowed' entry is not duplicated into the section
        section = text.split(GENERATED_BEGIN)[1]
        assert "shadowed" not in section
        # ... but the conflicting declaration is warned about
        assert "declares shadowed at 'v9.9'" in out
        assert "1 added, 0 removed, 0 updated" in out
        # the result is still a valid, parseable manifest
        mf = ManifestFile.load(path)
        assert mf.generated == {"child"}

    def test_section_is_maintained_without_flag(self, remote, write_manifest, capsys):
        parent, child, shadowed = remote("parent"), remote("child"), remote("shadowed")
        child.commit("child head")
        parent.commit_file("sub/west.yml", sub_manifest(child))
        path = write_manifest(top_manifest(parent, shadowed))
        assert main(["pin", "--include-imports", "-f", str(path)]) == 0
        capsys.readouterr()

        # plain pin re-runs regeneration; nothing changed -> no rewrite
        assert main(["pin", "-f", str(path)]) == 0
        assert "nothing to do" in capsys.readouterr().out

        # child moves; plain pin refreshes the lock
        new_head = child.commit("child moved")
        assert main(["pin", "-f", str(path)]) == 0
        assert "0 added, 0 removed, 1 updated" in capsys.readouterr().out
        assert f"revision: {new_head}\n" in path.read_text()

    def test_bump_regenerates_after_direct_bump(self, remote, write_manifest, capsys):
        parent, child, child2 = remote("parent"), remote("child"), remote("child2")
        shadowed = remote("shadowed")
        parent.commit_file("sub/west.yml", sub_manifest(child))
        path = write_manifest(top_manifest(parent, shadowed))
        assert main(["pin", "--include-imports", "-f", str(path)]) == 0
        capsys.readouterr()

        # upstream: parent drops child, adds child2
        parent.commit_file("sub/west.yml", dep_manifest(child2, "child2"), "swap deps")
        assert main(["bump", "parent", "-f", str(path)]) == 0
        out = capsys.readouterr().out
        text = path.read_text()
        assert "bump parent:" in out
        assert "1 added, 1 removed, 0 updated" in out
        assert "child2" in text
        assert f"url: {child.url}\n" not in text  # stale entry removed

    def test_bumping_managed_project_is_refused(self, remote, write_manifest, capsys):
        parent, child, shadowed = remote("parent"), remote("child"), remote("shadowed")
        parent.commit_file("sub/west.yml", sub_manifest(child))
        path = write_manifest(top_manifest(parent, shadowed))
        assert main(["pin", "--include-imports", "-f", str(path)]) == 0

        assert main(["bump", "child", "-f", str(path)]) == 1
        assert "managed by pin-west" in capsys.readouterr().err

    def test_managed_entries_not_bumped_to_their_own_releases(
        self, remote, write_manifest
    ):
        # the child repo has a newer release tag, but its pinned revision is
        # whatever the importing project declares (main), not the tag
        parent, child, shadowed = remote("parent"), remote("child"), remote("shadowed")
        child.tag("v9.0.0")
        head = child.commit("past release")
        parent.commit_file("sub/west.yml", sub_manifest(child))
        path = write_manifest(top_manifest(parent, shadowed))
        assert main(["pin", "--include-imports", "-f", str(path)]) == 0
        assert main(["bump", "-f", str(path)]) == 0
        assert f"revision: {head}\n" in path.read_text()

    def test_scoped_bump_holds_other_trees(self, remote, write_manifest, capsys):
        parent_a, parent_b = remote("parentA"), remote("parentB")
        child_a, child_b = remote("childA"), remote("childB")
        child_a.commit("childA base")  # distinct histories per repo
        b1 = child_b.commit("childB base")
        parent_a.commit_file("a/west.yml", dep_manifest(child_a, "depA"))
        parent_b.commit_file("b/west.yml", dep_manifest(child_b, "depB"))
        path = write_manifest(two_parents(parent_a, parent_b))
        assert main(["pin", "--include-imports", "-f", str(path)]) == 0
        a2 = child_a.commit("childA advances")
        b2 = child_b.commit("childB advances")
        capsys.readouterr()

        # scoped bump: only parentA's tree is re-resolved, depB holds
        assert main(["bump", "parentA", "-f", str(path)]) == 0
        assert "0 added, 0 removed, 1 updated" in capsys.readouterr().out
        text = path.read_text()
        assert f"revision: {a2}\n" in text
        assert f"revision: {b1}\n" in text
        assert b2 not in text

        # a full bump refreshes the held entry too
        assert main(["bump", "-f", str(path)]) == 0
        assert f"revision: {b2}\n" in path.read_text()

    def test_winner_flip_rescues_held_entry(self, remote, write_manifest, capsys):
        # parentA's declaration of 'shared' wins initially; when parentA drops
        # it, parentB's (different-branch) declaration becomes the winner —
        # the held pin would be invalid, so the flip forces a re-resolve
        parent_a, parent_b, shared = remote("parentA"), remote("parentB"), remote("sh")
        alt_head = shared.commit("shared base")
        shared.branch("alt")
        main_head = shared.commit("main ahead of alt")
        parent_a.commit_file("a/west.yml", dep_manifest(shared, "shared", "main"))
        parent_b.commit_file("b/west.yml", dep_manifest(shared, "shared", "alt"))
        path = write_manifest(two_parents(parent_a, parent_b))
        assert main(["pin", "--include-imports", "-f", str(path)]) == 0
        text = path.read_text()
        assert "- name: shared # via parentA (a/west.yml)" in text
        assert f"revision: {main_head}\n" in text

        parent_a.commit_file("a/west.yml", "manifest:\n  projects: []\n", "drop dep")
        capsys.readouterr()
        assert main(["bump", "parentA", "-f", str(path)]) == 0
        text = path.read_text()
        assert "- name: shared # via parentB (b/west.yml)" in text
        assert f"revision: {alt_head}\n" in text  # re-resolved despite scope
        assert main_head not in text

    def test_pin_and_content_use_same_snapshot_under_concurrent_push(
        self, remote, write_manifest, monkeypatch
    ):
        # child's manifest content and child's pin must come from the same
        # commit, even if the declared branch moves mid-regeneration
        from pin_west import resolve as res

        parent, child, shadowed = remote("parent"), remote("child"), remote("shadowed")
        grandchild = remote("grandchild")
        child.commit_file(
            "sub2/west.yml", dep_manifest(grandchild, "grandchild", grandchild.sha())
        )
        head_before_push = child.sha()
        parent.commit_file(
            "sub/west.yml",
            dedent(f"""\
                manifest:
                  projects:
                    - name: child
                      url: {child.url}
                      revision: main
                      import: sub2/west.yml
            """),
        )
        path = write_manifest(top_manifest(parent, shadowed))

        real_fetch = res.fetch_blob

        def racy_fetch(url, revision, blob_path):
            content = real_fetch(url, revision, blob_path)
            if url == child.url:  # a push lands right after the content read
                child.commit("pushed mid-run")
            return content

        monkeypatch.setattr(res, "fetch_blob", racy_fetch)
        assert main(["pin", "--include-imports", "-f", str(path)]) == 0
        # pinned at the head whose manifest content was read, not the new one
        assert f"revision: {head_before_push}\n" in path.read_text()

    def test_imported_manifest_self_import_resolves_from_remote(
        self, remote, write_manifest, capsys
    ):
        # zephyr-style: the *imported* manifest self-imports a directory of
        # submanifests; they are fetched at the pin and locked like the rest
        parent, child, shadowed = remote("parent"), remote("child"), remote("shadowed")
        child_head = child.commit("child head")
        dep_a, dep_b, dep_c = remote("dep_a"), remote("dep_b"), remote("dep_c")
        heads = {
            n: r.commit(f"{n} head")
            for n, r in [("dep_a", dep_a), ("dep_b", dep_b), ("dep_c", dep_c)]
        }
        # b.yml sorts before c.yml: its 'dup' declaration must win
        parent.commit_file("extras/c.yml", dep_manifest(dep_c, "dup"))
        parent.commit_file("extras/b.yml", dep_manifest(dep_b, "dup"))
        parent.commit_file("extras/a.yml", dep_manifest(dep_a, "dep_a"))
        parent.commit_file("extras/README.md", "not a manifest\n")
        parent.commit_file(
            "sub/west.yml",
            sub_manifest(child) + "  self:\n    path: parent\n    import: extras\n",
        )
        path = write_manifest(top_manifest(parent, shadowed))
        assert main(["pin", "--include-imports", "-f", str(path)]) == 0
        out = capsys.readouterr().out
        assert "cannot be resolved" not in out
        text = path.read_text()
        section = text.split(GENERATED_BEGIN)[1]
        assert f"revision: {child_head}\n" in section
        assert "- name: dep_a # via parent (extras/a.yml)" in section
        assert f"revision: {heads['dep_a']}\n" in section
        assert "- name: dup # via parent (extras/b.yml)" in section
        assert f"revision: {heads['dep_b']}\n" in section
        assert heads["dep_c"] not in section
        assert "3 added" in out

    def test_nested_self_import_and_ordering(self, remote, write_manifest):
        # the imported manifest's own projects beat its self-imports, and a
        # self-imported file may self-import again
        parent, child, shadowed = remote("parent"), remote("child"), remote("shadowed")
        child.commit("child head")
        other, deep = remote("other"), remote("deep")
        deep_head = deep.commit("deep head")
        parent.commit_file(
            "extras/more.yml",
            dep_manifest(other, "child")  # loses against sub/west.yml's child
            + "  self:\n    import: extras/deep.yml\n",
        )
        parent.commit_file("extras/deep.yml", dep_manifest(deep, "deep"))
        parent.commit_file(
            "sub/west.yml",
            sub_manifest(child) + "  self:\n    import: [extras/more.yml]\n",
        )
        path = write_manifest(top_manifest(parent, shadowed))
        assert main(["pin", "--include-imports", "-f", str(path)]) == 0
        section = path.read_text().split(GENERATED_BEGIN)[1]
        assert f"url: {child.url}\n" in section
        assert f"url: {other.url}\n" not in section
        assert "- name: deep # via parent (extras/deep.yml)" in section
        assert f"revision: {deep_head}\n" in section

    def test_map_form_self_import_is_skipped_with_warning(
        self, remote, write_manifest, capsys
    ):
        parent, child, shadowed = remote("parent"), remote("child"), remote("shadowed")
        child_head = child.commit("child head")
        parent.commit_file("extras/more.yml", dep_manifest(remote("dep"), "dep"))
        parent.commit_file(
            "sub/west.yml",
            sub_manifest(child) + "  self:\n    import:\n      file: extras/more.yml\n"
            "      name-allowlist: [dep]\n",
        )
        path = write_manifest(top_manifest(parent, shadowed))
        assert main(["pin", "--include-imports", "-f", str(path)]) == 0
        assert "uses the map form" in capsys.readouterr().out
        text = path.read_text()
        assert f"revision: {child_head}\n" in text
        assert "- name: dep" not in text

    def test_missing_self_import_fails(self, remote, write_manifest, capsys):
        parent, child, shadowed = remote("parent"), remote("child"), remote("shadowed")
        child.commit("child head")
        parent.commit_file(
            "sub/west.yml",
            sub_manifest(child) + "  self:\n    import: extras/nope.yml\n",
        )
        path = write_manifest(top_manifest(parent, shadowed))
        assert main(["pin", "--include-imports", "-f", str(path)]) == 1
        assert "self-imported 'extras/nope.yml' not found" in capsys.readouterr().err

    def test_top_level_self_import_without_workspace(
        self, remote, write_manifest, tmp_path
    ):
        # read from disk next to the manifest; no workspace needed
        parent, shadowed, dep = remote("parent"), remote("shadowed"), remote("dep")
        parent.commit_file("sub/west.yml", "manifest:\n  projects: []\n")
        head = dep.commit("dep head")
        (tmp_path / "extra.yml").write_text(dep_manifest(dep, "dep"))
        path = write_manifest(
            top_manifest(parent, shadowed).replace(
                "    path: config\n", "    path: config\n    import: extra.yml\n"
            )
        )
        assert main(["pin", "--include-imports", "-f", str(path)]) == 0
        text = path.read_text()
        assert "import: extra.yml" in text  # the user's manifest is untouched
        section = text.split(GENERATED_BEGIN)[1]
        assert "- name: dep # via self (extra.yml)" in section
        assert f"revision: {head}\n" in section
        assert "pin-west-self" not in text

    def test_top_level_self_import_is_relative_to_repo_root(self, remote, tmp_path):
        # like west: paths resolve against the manifest repository, not the
        # manifest file's directory
        repo = GitRemote(tmp_path / "app")
        dep = remote("dep")
        head = dep.commit("dep head")
        (repo.path / "submanifests").mkdir()
        (repo.path / "submanifests" / "x.yml").write_text(dep_manifest(dep, "dep"))
        (repo.path / "config").mkdir()
        path = repo.path / "config" / "west.yml"
        path.write_text(
            dep_manifest(remote("direct"), "direct")
            + "  self:\n    path: config\n    import: submanifests\n"
        )
        assert main(["pin", "--include-imports", "-f", str(path)]) == 0
        section = path.read_text().split(GENERATED_BEGIN)[1]
        assert "- name: dep # via self (submanifests/x.yml)" in section
        assert f"revision: {head}\n" in section


@pytest.mark.skipif(shutil.which("west") is None, reason="west CLI not on PATH")
class TestWorkspaceSelfImports:
    def test_top_level_self_import(self, remote, west_workspace, monkeypatch):
        child, direct = remote("child"), remote("direct")
        head = child.commit("child head")
        ws = west_workspace(
            f"""\
                manifest:
                  projects:
                    - name: direct
                      url: {direct.url}
                      revision: main
                  self:
                    path: config
                    import: extra.yml
            """,
            extra_files=[("extra.yml", dep_manifest(child, "dep"))],
        )
        monkeypatch.chdir(ws)
        assert main(["pin", "--include-imports"]) == 0
        text = (ws / "config" / "west.yml").read_text()
        assert "- name: dep" in text.split(GENERATED_BEGIN)[1]
        assert f"revision: {head}\n" in text

    def test_imported_manifest_self_import_resolves_from_clone(
        self, remote, west_workspace, monkeypatch, capsys
    ):
        # zephyr-style: parent's manifest self-imports another file from the
        # parent repo; with parent cloned, west reads it and dep2 gets pinned
        parent, child2 = remote("parent"), remote("child2")
        head2 = child2.commit("child2 head")
        parent.commit_file(
            "extras/more.yml", dep_manifest(child2, "dep2"), "add extras"
        )
        parent.commit_file(
            "sub/west.yml",
            "manifest:\n  projects: []\n  self:\n    path: parent\n"
            "    import: extras/more.yml\n",
            "add manifest",
        )
        ws = west_workspace(
            f"""\
            manifest:
              projects:
                - name: parent
                  url: {parent.url}
                  revision: main
                  import: sub/west.yml
              self:
                path: config
        """,
            update=True,
        )
        monkeypatch.chdir(ws)
        assert main(["pin", "--include-imports"]) == 0
        out = capsys.readouterr().out
        assert "cannot be resolved" not in out  # no strip warning
        text = (ws / "config" / "west.yml").read_text()
        assert "- name: dep2" in text.split(GENERATED_BEGIN)[1]
        assert f"revision: {head2}\n" in text

    def test_bump_past_clone_follows_remote(
        self, remote, west_workspace, monkeypatch, capsys
    ):
        # bump moves parent beyond the clone's checkout: its self-imported
        # projects are re-read from the remote at the new pin, never from
        # the (stale) clone
        parent, child2, child3 = remote("parent"), remote("child2"), remote("child3")
        child2.commit("child2 head")
        head3 = child3.commit("child3 head")
        parent.commit_file("extras/more.yml", dep_manifest(child2, "dep2"))
        parent.commit_file(
            "sub/west.yml",
            "manifest:\n  projects: []\n  self:\n    path: parent\n"
            "    import: extras/more.yml\n",
        )
        ws = west_workspace(
            f"""\
            manifest:
              projects:
                - name: parent
                  url: {parent.url}
                  revision: main
                  import: sub/west.yml
              self:
                path: config
        """,
            update=True,
        )
        monkeypatch.chdir(ws)
        assert main(["pin", "--include-imports"]) == 0
        assert "- name: dep2" in (ws / "config" / "west.yml").read_text()

        # upstream: parent swaps its self-imported dep, bump moves past clone
        parent.commit_file("extras/more.yml", dep_manifest(child3, "dep3"), "swap")
        capsys.readouterr()
        assert main(["bump", "parent"]) == 0
        assert "1 added, 1 removed" in capsys.readouterr().out
        text = (ws / "config" / "west.yml").read_text()
        section = text.split(GENERATED_BEGIN)[1]
        assert "dep2" not in section
        assert "- name: dep3 # via parent (extras/more.yml)" in section
        assert f"revision: {head3}\n" in text
