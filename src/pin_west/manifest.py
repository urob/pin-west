"""Text-level model of a west manifest.

The west library is used elsewhere to *interpret* the manifest (resolving
defaults, remotes and urls). This module only locates project blocks in the
raw text and rewrites ``revision:`` lines in place, so that all other
formatting and comments are preserved verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_KEY_LINE = re.compile(r"^(\s*)(- )?([A-Za-z0-9_-]+):(.*)$")
_PROJECTS = re.compile(r"^(\s*)projects:\s*(?:#.*)?$")
_COMMENT_REF = re.compile(r"^(\S+)(?:\s+\(.*\))?$")

GENERATED_BEGIN = "# --- imported projects pinned by pin-west; do not edit ---"
GENERATED_END = "# --- end of imported projects ---"


class ManifestError(Exception):
    pass


def is_pinned(revision: str | None) -> TypeGuard[str]:
    """True if revision is a full commit sha (SHA-1 or SHA-256)."""
    return bool(revision and _FULL_SHA.match(revision))


def comment_ref(comment: str | None) -> str | None:
    """Extract the ref from a trailing comment like 'main (2026-08-17)' or 'v0.3'."""
    if not comment:
        return None
    m = _COMMENT_REF.match(comment.strip())
    return m.group(1) if m else None


def _split_comment(raw: str) -> tuple[str, str | None]:
    """Split a raw scalar into (value, trailing comment text)."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        if end > 0:
            rest = raw[end + 1 :].strip()
            comment = rest[1:].strip() if rest.startswith("#") else None
            return raw[1:end], comment
    if raw.startswith("#"):
        return "", raw[1:].strip()
    value, sep, comment = raw.partition(" #")
    return value.strip(), comment.strip() if sep else None


@dataclass
class ProjectBlock:
    name: str
    name_row: int
    key_indent: str
    revision_row: int | None = None
    revision: str | None = None
    comment: str | None = None
    name_comment: str | None = None  # trailing comment on the 'name:' line


class ManifestFile:
    def __init__(self, path: Path, lines: list[str]):
        self.path = path
        self.lines = lines
        self.blocks, self.section_end, self.item_indent, self._gen_range = (
            _parse_blocks(lines)
        )
        self.generated: set[str] = set()
        if self._gen_range:
            lo, hi = self._gen_range
            self.generated = {
                n for n, b in self.blocks.items() if lo <= b.name_row <= hi
            }

    @classmethod
    def load(cls, path: Path | str) -> ManifestFile:
        path = Path(path)
        try:
            text = path.read_text()
        except OSError as e:
            raise ManifestError(f"cannot read {path}: {e}")
        return cls(path, text.splitlines())

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"

    def set_revision(self, name: str, revision: str, comment: str | None) -> None:
        block = self.blocks[name]
        suffix = f" # {comment}" if comment else ""
        if block.revision_row is None:
            row = block.name_row + 1
            self.lines.insert(row, f"{block.key_indent}revision: {revision}{suffix}")
            for b in self.blocks.values():
                if b.name_row >= row:
                    b.name_row += 1
                if b.revision_row is not None and b.revision_row >= row:
                    b.revision_row += 1
            block.revision_row = row
        else:
            row = block.revision_row
            prefix = self.lines[row][: self.lines[row].index("revision:")]
            self.lines[row] = f"{prefix}revision: {revision}{suffix}"
        block.revision, block.comment = revision, comment

    def without_generated(self) -> ManifestFile:
        """A copy with the pin-west managed imports section removed."""
        if not self._gen_range:
            return ManifestFile(self.path, list(self.lines))
        lo, hi = self._gen_range
        lines = self.lines[:lo] + self.lines[hi + 1 :]
        if lo > 0 and not lines[lo - 1].strip():
            del lines[lo - 1]  # drop the blank line the section was set off with
        return ManifestFile(self.path, lines)

    @property
    def has_generated_section(self) -> bool:
        return self._gen_range is not None

    def with_generated(self, body: list[str]) -> ManifestFile:
        """A copy with a fresh managed-imports section (replacing any existing
        one) at the end of the projects list. An empty body keeps the markers:
        the section's presence is the opt-in for future regeneration."""
        base = self.without_generated()
        insert = base.section_end
        while insert > 0 and not base.lines[insert - 1].strip():
            insert -= 1
        indent = " " * base.item_indent
        section = ["", f"{indent}{GENERATED_BEGIN}", *body, f"{indent}{GENERATED_END}"]
        return ManifestFile(
            self.path, base.lines[:insert] + section + base.lines[insert:]
        )


def _parse_blocks(
    lines: list[str],
) -> tuple[dict[str, ProjectBlock], int, int, tuple[int, int] | None]:
    proj_row = proj_indent = None
    for i, line in enumerate(lines):
        if m := _PROJECTS.match(line):
            proj_row, proj_indent = i, len(m.group(1))
            break
    if proj_row is None or proj_indent is None:
        raise ManifestError("no 'projects:' section found")

    # Collect the start row of each list item directly under 'projects:'.
    item_rows: list[int] = []
    item_indent = None
    end_row = len(lines)
    for i in range(proj_row + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(lines[i]) - len(lines[i].lstrip())
        if indent < proj_indent or (
            indent == proj_indent and not stripped.startswith("-")
        ):
            end_row = i
            break
        if stripped.startswith("-"):
            if item_indent is None:
                item_indent = indent
            if indent == item_indent:
                item_rows.append(i)

    blocks: dict[str, ProjectBlock] = {}
    for idx, start in enumerate(item_rows):
        stop = item_rows[idx + 1] if idx + 1 < len(item_rows) else end_row
        block = _parse_item(lines, start, stop)
        if block.name in blocks:
            raise ManifestError(f"duplicate project name: {block.name}")
        blocks[block.name] = block

    begin = end = None
    for i, line in enumerate(lines):
        if line.strip() == GENERATED_BEGIN:
            begin = i
        elif line.strip() == GENERATED_END:
            end = i
    if begin is None and end is None:
        gen_range = None
    elif begin is None or end is None or end < begin:
        raise ManifestError("malformed pin-west managed-imports section markers")
    else:
        gen_range = (begin, end)
    if item_indent is None:
        item_indent = proj_indent + 2
    return blocks, end_row, item_indent, gen_range


def _parse_item(lines: list[str], start: int, stop: int) -> ProjectBlock:
    key_col = None
    name = name_row = name_comment = None
    revision_row = revision = comment = None
    for i in range(start, stop):
        m = _KEY_LINE.match(lines[i])
        if not m:
            continue
        indent, dash, key, rest = m.groups()
        col = len(indent) + (2 if dash else 0)
        if key_col is None:
            key_col = col
        if col != key_col:
            continue  # nested mapping (e.g. inside 'import:')
        value, cmt = _split_comment(rest)
        if key == "name":
            name, name_row, name_comment = value, i, cmt
        elif key == "revision":
            revision_row, revision, comment = i, value, cmt
    if name is None or name_row is None or key_col is None:
        raise ManifestError(f"project item at line {start + 1} has no 'name' key")
    return ProjectBlock(
        name=name,
        name_row=name_row,
        key_indent=" " * key_col,
        revision_row=revision_row,
        revision=revision,
        comment=comment,
        name_comment=name_comment,
    )
