"""Render a markdown overview of what changed between two manifest versions.

Used by action.yml for the pull request body. Usage:

    summary.py OLD_MANIFEST NEW_MANIFEST

Writes markdown to stdout: one line per changed project with the tracked
ref before/after and, for GitHub remotes, a link to the commit range.
"""

from __future__ import annotations

import sys
from pathlib import Path

from west.manifest import ImportFlag, Manifest, ManifestProject

from pin_west.manifest import ManifestFile, is_pinned
from pin_west.resolve import github_repo


def _urls(mf: ManifestFile) -> dict[str, str]:
    """Each project's resolved url."""
    manifest = Manifest.from_data(mf.text(), import_flags=ImportFlag.IGNORE)
    return {
        p.name: p.url for p in manifest.projects if not isinstance(p, ManifestProject)
    }


def _label(revision: str | None, comment: str | None) -> str:
    """What a pin reads as: its comment (the tracked ref), else the sha."""
    if comment:
        return f"`{comment}`"
    if revision is None:
        return "_(default)_"
    return f"`{revision[:12] if is_pinned(revision) else revision}`"


def _range_link(url: str, old: str | None, new: str | None) -> str | None:
    """A web link to the old..new range; None for non-GitHub remotes."""
    gh = github_repo(url)
    if gh is None or not is_pinned(new):
        return None
    base = f"https://github.com/{gh[0]}/{gh[1]}"
    if is_pinned(old):
        return f"[`{old[:7]}...{new[:7]}`]({base}/compare/{old}...{new})"
    return f"[`{new[:7]}`]({base}/commit/{new})"


def summarize(old_path: Path, new_path: Path) -> str:
    old_mf = ManifestFile.load(old_path)
    new_mf = ManifestFile.load(new_path)
    urls = _urls(new_mf)
    lines: list[str] = []

    for name in new_mf.blocks:
        new = new_mf.blocks[name]
        old = old_mf.blocks.get(name)
        if old is None:
            # managed entries carry their provenance on the name line
            via = f" ({new.name_comment})" if new.name_comment else ""
            label = _label(new.revision, new.comment)
            lines.append(f"- **{name}**: added at {label}{via}")
            continue
        if old.revision == new.revision:
            continue
        change = (
            f"{_label(old.revision, old.comment)} → {_label(new.revision, new.comment)}"
        )
        url = urls.get(name)
        link = _range_link(url, old.revision, new.revision) if url else None
        lines.append(f"- **{name}**: {change}" + (f" ({link})" if link else ""))

    for name in old_mf.blocks:
        if name not in new_mf.blocks:
            lines.append(f"- **{name}**: removed")

    return "\n".join(lines) + "\n" if lines else ""


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    sys.stdout.write(summarize(Path(argv[1]), Path(argv[2])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
