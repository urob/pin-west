"""Unit tests for the text-level manifest model (no network, no git)."""

from pathlib import Path
from textwrap import dedent

import pytest
from west.manifest import ImportFlag, Manifest

from pin_west.manifest import (
    ManifestError,
    ManifestFile,
    comment_ref,
    is_pinned,
)

DATA = Path(__file__).parent / "data"


def load(text: str) -> ManifestFile:
    return ManifestFile(Path("west.yml"), dedent(text).splitlines())


class TestIsPinned:
    def test_sha1(self):
        assert is_pinned("0331b7d16e80954b807917f9323e59ffc1e3b626")

    def test_sha256(self):
        assert is_pinned("a" * 64)

    def test_not_pinned(self):
        assert not is_pinned("main")
        assert not is_pinned("0331b7d")  # short sha does not count
        assert not is_pinned("v1.0.0")
        assert not is_pinned(None)
        assert not is_pinned("")


class TestCommentRef:
    def test_branch_with_date(self):
        assert comment_ref("main (2026-08-17)") == "main"

    def test_tag(self):
        assert comment_ref("v0.3") == "v0.3"

    def test_empty(self):
        assert comment_ref(None) is None
        assert comment_ref("") is None


class TestParsing:
    def test_basic_fields(self):
        mf = load("""\
            manifest:
              projects:
                - name: foo
                  revision: main # tracking main
                - name: bar
                  url: https://example.com/bar
        """)
        assert set(mf.blocks) == {"foo", "bar"}
        assert mf.blocks["foo"].revision == "main"
        assert mf.blocks["foo"].comment == "tracking main"
        assert mf.blocks["bar"].revision is None

    def test_quoted_revision(self):
        mf = load("""\
            manifest:
              projects:
                - name: foo
                  revision: "main" # note
        """)
        assert mf.blocks["foo"].revision == "main"
        assert mf.blocks["foo"].comment == "note"

    def test_items_at_same_indent_as_projects_key(self):
        mf = load("""\
            manifest:
              projects:
              - name: foo
                revision: main
              self:
                path: config
        """)
        assert set(mf.blocks) == {"foo"}
        assert mf.blocks["foo"].revision == "main"

    def test_nested_import_list_not_confused_with_projects(self):
        mf = load("""\
            manifest:
              projects:
                - name: zephyr
                  revision: main
                  import:
                    name-allowlist:
                      - cmsis
                      - hal_nordic
                - name: other
        """)
        assert set(mf.blocks) == {"zephyr", "other"}

    def test_name_not_first_key(self):
        mf = load("""\
            manifest:
              projects:
                - revision: main
                  name: foo
        """)
        assert mf.blocks["foo"].revision == "main"

    def test_no_projects_section(self):
        with pytest.raises(ManifestError, match="projects"):
            load("manifest:\n  self:\n    path: config\n")

    def test_duplicate_name(self):
        with pytest.raises(ManifestError, match="duplicate"):
            load("""\
                manifest:
                  projects:
                    - name: foo
                    - name: foo
            """)

    def test_item_without_name(self):
        with pytest.raises(ManifestError, match="no 'name'"):
            load("""\
                manifest:
                  projects:
                    - revision: main
            """)


class TestSetRevision:
    def test_replace_preserves_other_lines(self):
        mf = load("""\
            manifest:
              projects:
                # a comment
                - name: foo
                  path: modules/foo
                  revision: main # old note
                  clone-depth: 1
        """)
        original = mf.text()
        mf.set_revision("foo", "a" * 40, "main (2026-08-17)")
        expected = original.replace(
            "  revision: main # old note",
            f"  revision: {'a' * 40} # main (2026-08-17)",
        )
        assert mf.text() == expected

    def test_replace_without_comment(self):
        mf = load("""\
            manifest:
              projects:
                - name: foo
                  revision: main
        """)
        mf.set_revision("foo", "a" * 40, None)
        assert f"revision: {'a' * 40}\n" in mf.text()

    def test_insert_after_name_line(self):
        mf = load("""\
            manifest:
              projects:
                - name: foo
                  path: modules/foo
                - name: bar
                  revision: main
        """)
        mf.set_revision("foo", "a" * 40, "main (2026-08-17)")
        lines = mf.text().splitlines()
        assert lines[2] == "    - name: foo"
        assert lines[3] == f"      revision: {'a' * 40} # main (2026-08-17)"
        assert lines[4] == "      path: modules/foo"
        # rows of later blocks were shifted; editing them must still work
        mf.set_revision("bar", "b" * 40, None)
        assert f"      revision: {'b' * 40}" in mf.text()

    def test_updates_block_state(self):
        mf = load("""\
            manifest:
              projects:
                - name: foo
        """)
        mf.set_revision("foo", "a" * 40, "v1.0")
        assert mf.blocks["foo"].revision == "a" * 40
        assert mf.blocks["foo"].comment == "v1.0"


class TestRealWorldSample:
    """The zmk-config sample: a realistic manifest mixing defaults, two
    remotes, every revision form, imports, and the comment conventions."""

    @staticmethod
    def _load():
        mf = ManifestFile.load(DATA / "zmk-config.yml")
        return mf, Manifest.from_data(mf.text(), import_flags=ImportFlag.IGNORE)

    def test_round_trip_is_byte_identical(self):
        text = (DATA / "zmk-config.yml").read_text()
        assert ManifestFile.load(DATA / "zmk-config.yml").text() == text

    def test_blocks_match_west_parse(self):
        mf, manifest = self._load()
        names = {p.name for p in manifest.projects} - {"manifest"}
        assert names == set(mf.blocks)
        assert len(names) == 4

    def test_west_resolves_urls_from_remotes_and_defaults(self):
        _, manifest = self._load()
        urls = {p.name: p.url for p in manifest.projects if p.name != "manifest"}
        assert urls["zmk"] == "https://github.com/zmkfirmware/zmk"  # explicit remote
        assert urls["zmk-helpers"] == "https://github.com/urob/zmk-helpers"  # default

    def test_west_resolves_default_revision(self):
        mf, manifest = self._load()
        revs = {p.name: p.revision for p in manifest.projects if p.name != "manifest"}
        assert mf.blocks["zmk"].revision is None  # no revision line in the text ...
        assert revs["zmk"] == "v0.3"  # ... west fills it from defaults
        assert revs["zmk-helpers"] == "main"  # explicit overrides defaults
        assert mf.blocks["zephyr"].comment == "v3.5.0+zmk-fixes"
