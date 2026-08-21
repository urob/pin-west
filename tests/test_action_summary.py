"""The change overview rendered by the GitHub Action into the PR body."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("summary", ROOT / "action" / "summary.py")
assert SPEC is not None and SPEC.loader is not None
summary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(summary)

OLD_SHA = "a" * 40
NEW_SHA = "b" * 40
IMP_SHA = "c" * 40

OLD = f"""manifest:
  remotes:
    - name: zmkfirmware
      url-base: https://github.com/zmkfirmware
    - name: local
      url-base: file:///srv/git
  projects:
    - name: zmk
      remote: zmkfirmware
      revision: {OLD_SHA} # v0.2.1
    - name: zephyr
      remote: local
      revision: {OLD_SHA} # main (2026-01-01)
    - name: unchanged
      remote: zmkfirmware
      revision: {OLD_SHA}
    - name: gone
      remote: zmkfirmware
      revision: {OLD_SHA}
"""

NEW = f"""manifest:
  remotes:
    - name: zmkfirmware
      url-base: https://github.com/zmkfirmware
    - name: local
      url-base: file:///srv/git
  projects:
    - name: zmk
      remote: zmkfirmware
      revision: {NEW_SHA} # v0.3.0
    - name: zephyr
      remote: local
      revision: {NEW_SHA} # main (2026-02-02)
    - name: unchanged
      remote: zmkfirmware
      revision: {OLD_SHA}
    - name: cmsis # via zephyr (west.yml)
      remote: zmkfirmware
      revision: {IMP_SHA}
"""


@pytest.fixture
def rendered(tmp_path: Path) -> str:
    (tmp_path / "old.yml").write_text(OLD)
    (tmp_path / "new.yml").write_text(NEW)
    return summary.summarize(tmp_path / "old.yml", tmp_path / "new.yml")


def test_changed_projects_with_compare_link(rendered: str) -> None:
    assert (
        "- **zmk**: `v0.2.1` → `v0.3.0` "
        f"([`aaaaaaa...bbbbbbb`](https://github.com/zmkfirmware/zmk/compare/{OLD_SHA}...{NEW_SHA}))"
    ) in rendered


def test_non_github_remote_has_no_link(rendered: str) -> None:
    assert "- **zephyr**: `main (2026-01-01)` → `main (2026-02-02)`\n" in rendered


def test_unchanged_omitted_added_and_removed_listed(rendered: str) -> None:
    assert "unchanged" not in rendered
    assert f"- **cmsis**: added at `{IMP_SHA[:12]}` (via zephyr (west.yml))" in rendered
    assert "- **gone**: removed" in rendered


def test_unpinned_old_links_to_commit(tmp_path: Path) -> None:
    (tmp_path / "old.yml").write_text(OLD.replace(f"{OLD_SHA} # v0.2.1", "v0.2.1"))
    (tmp_path / "new.yml").write_text(NEW)
    out = summary.summarize(tmp_path / "old.yml", tmp_path / "new.yml")
    assert (
        "- **zmk**: `v0.2.1` → `v0.3.0` "
        f"([`bbbbbbb`](https://github.com/zmkfirmware/zmk/commit/{NEW_SHA}))"
    ) in out


def test_no_changes_is_empty(tmp_path: Path) -> None:
    (tmp_path / "m.yml").write_text(OLD)
    assert summary.summarize(tmp_path / "m.yml", tmp_path / "m.yml") == ""
