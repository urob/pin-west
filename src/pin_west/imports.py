"""Materialize and pin imported (indirect) projects.

The manifest's full import tree is resolved without a workspace or clones:
west's own import machinery runs on an importer callback that fetches each
imported manifest file straight from its project's remote at the pinned
revision, including the files it ``self: import``s (see
``_expand_self_imports``).
The resolved indirect projects are written to a marker-delimited section at
the end of ``projects:`` — a lockfile embedded in the manifest. Entries are
self-contained (explicit url/path/groups) and carry no tracking comments:
their revisions are whatever the importing project declares, re-resolved on
every regeneration, so bump's tracking logic never applies to them.

Because top-level projects take precedence over imported ones in west,
materializing an imported project *is* pinning it. Projects the user lists
themselves are never added here; they shadow the import (a warning is
printed when the import declares a different revision than the user pins).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from west.configuration import Configuration, MalformedConfig
from west.manifest import (
    MalformedManifest,
    Manifest,
    ManifestImportFailed,
    ManifestProject,
)
from west.util import WestNotFound, west_topdir

from . import resolve as res
from .manifest import ManifestError, ManifestFile, comment_ref, is_pinned


@dataclass
class _ImportInfo:
    provenance: dict[str, str] = field(default_factory=dict)
    declared: dict[str, str | None] = field(default_factory=dict)
    resolved: dict[tuple[str, str], str | None] = field(default_factory=dict)

    def resolve_once(self, url: str, revision: str) -> str | None:
        """Resolve a branch/tag to a sha exactly once per (url, ref) and cache
        it, so the imported-manifest fetch and the pin describe the same
        snapshot even if the ref moves mid-run."""
        key = (url, revision)
        if key not in self.resolved:
            resolved = res.resolve_ref(url, revision)
            self.resolved[key] = resolved.sha if resolved else None
        return self.resolved[key]


# The '# via <importer> (<path>)' comment on managed entries is load-bearing:
# it is parsed back to scope selective bumps. Keep every encode/decode here.
def _provenance(importer_name: str, path: str) -> str:
    return f"{importer_name} ({path})"


def _provenance_importer(provenance: str) -> str:
    return provenance.split(" (", 1)[0]


def _via_comment(provenance: str) -> str:
    return f"via {provenance}"


def manifest_data(text: str) -> dict:
    """The 'manifest:' mapping of a YAML document; {} when absent or invalid."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    manifest = data.get("manifest") if isinstance(data, dict) else None
    return manifest if isinstance(manifest, dict) else {}


def regenerate(
    mf: ManifestFile,
    errors: list[str],
    scope: set[str] | None = None,
    token: str | None = None,
) -> ManifestFile:
    """Re-resolve the import tree and rebuild the managed section. Returns a
    new ManifestFile; `mf` is left untouched.

    With `scope` (a set of direct project names, from a selective bump), only
    entries whose winning declaration roots in one of those projects are
    re-resolved; other existing entries hold their pinned revision — unless
    their winning declarer changed, detected via the recorded '# via'
    comment, or they are new. Without scope, everything is re-resolved."""
    base = mf.without_generated()
    projects, info = _resolve_full(base.text(), mf.path, token)
    indent = " " * base.item_indent
    old = {
        name: (mf.blocks[name].revision, mf.blocks[name].name_comment)
        for name in mf.generated
    }
    direct = set(base.blocks)

    body: list[str] = []
    indirect = sorted(
        (p for p in projects if p.name not in direct), key=lambda p: p.name
    )
    for p in indirect:
        provenance = info.provenance.get(p.name)
        revision = p.revision
        if not is_pinned(revision):
            held = _held_revision(p.name, provenance, old, scope, info, direct)
            if held is not None:
                revision = held
            else:
                sha = info.resolve_once(p.url, revision)
                if sha is None:
                    errors.append(
                        f"{p.name}: imported ref '{revision}' not found on {p.url}"
                    )
                    continue
                revision = sha
        body.extend(_render_entry(p, revision, provenance, indent))

    _warn_shadowed_conflicts(base, info)
    new = mf.with_generated(body)
    _report(mf, new)
    return new


def _held_revision(
    name: str,
    provenance: str | None,
    old: dict[str, tuple[str | None, str | None]],
    scope: set[str] | None,
    info: _ImportInfo,
    direct: set[str],
) -> str | None:
    """Out-of-scope hold: on a scoped run, keep an existing entry's pinned
    revision when its winning declarer's tree wasn't selected. None means
    re-resolve."""
    if scope is None or name not in old:
        return None
    old_revision, old_name_comment = old[name]
    if provenance is None or old_name_comment != _via_comment(provenance):
        return None  # winning declarer changed (or unrecorded): re-resolve
    if not is_pinned(old_revision):
        return None
    root = _root_importer(name, info, direct)
    if root is None or root in scope:
        return None
    return old_revision


def _root_importer(name: str, info: _ImportInfo, direct: set[str]) -> str | None:
    """Walk the winning-declaration chain up to the direct project it roots
    in (e.g. cmsis -> zephyr -> zmk), or _SELF_IMPORTER for entries declared
    by the top-level manifest's own self-imports (never part of a bump
    selection, so scoped runs hold them). None if the chain can't be traced."""
    seen = {name}
    while True:
        provenance = info.provenance.get(name)
        if provenance is None:
            return None
        importer = _provenance_importer(provenance)
        if importer in direct or importer == _SELF_IMPORTER:
            return importer
        if importer in seen:
            return None
        seen.add(importer)
        name = importer


# Synthetic project wrapping the top-level manifest when it has a
# 'self: import:' of its own (see _resolve_full); never rendered.
_SELF = "pin-west-self"
# Provenance importer name for projects declared in the top-level manifest's
# own self-imported files ('# via self (<file>)').
_SELF_IMPORTER = "self"
_SELF_WRAPPER = f"""\
manifest:
  projects:
    - name: {_SELF}
      url: {_SELF}
      revision: {_SELF}
      import: west.yml
"""


def _resolve_full(
    text: str, manifest_path: Path, token: str | None
) -> tuple[list, _ImportInfo]:
    """Resolve the full import tree of manifest `text` through west, with
    every imported file fetched at its pinned revision (no workspace, no
    clones). Self-imports are expanded by us (see _expand_self_imports):
    those of imported manifests from their remote, the top-level's from the
    manifest repository on disk."""
    info = _ImportInfo()

    def importer(project, path):
        if project.name == _SELF:
            reader = _LocalReader(_manifest_repo(manifest_path))
            return _expand_self_imports(
                text, _SELF_IMPORTER, path, reader, info, record=False
            )
        declared = str(project.revision)
        revision = declared
        if not is_pinned(revision):
            sha = info.resolve_once(project.url, revision)
            if sha is None:
                raise res.ResolveError(f"cannot resolve '{revision}' on {project.url}")
            revision = sha
        reader = res.RemoteTree(project.url, revision, token)
        try:
            content = reader.read(path)
        except res.ResolveError:
            if revision == declared:
                raise
            # some hosts refuse fetching arbitrary shas; fall back to the
            # declared ref (reopens the snapshot race, but stays functional)
            reader = res.RemoteTree(project.url, declared, token)
            content = reader.read(path)
        if content is None:
            raise res.ResolveError(
                f"cannot fetch '{path}' from {project.url} at {revision}"
            )
        return _expand_self_imports(content, project.name, path, reader, info)

    data = text
    if (manifest_data(text).get("self") or {}).get("import") is not None:
        # west can only read a top-level self-import from a workspace; route
        # the manifest through the importer instead, where we expand it
        data = _SELF_WRAPPER
    try:
        manifest = Manifest.from_data(data, importer=importer)
    except (ManifestImportFailed, MalformedManifest) as e:
        raise ManifestError(f"import resolution failed: {e}")
    except RecursionError:
        raise ManifestError("import resolution failed: self-imports form a cycle")
    except (OSError, UnicodeDecodeError) as e:
        raise ManifestError(f"import resolution failed: cannot read self-import: {e}")
    projects = [
        p
        for p in manifest.projects
        if not isinstance(p, ManifestProject) and p.name != _SELF
    ]
    return projects, info


def configured_manifest(topdir: str) -> Path | None:
    """Path of the workspace's configured manifest, None if unconfigured.
    Raises MalformedConfig on a broken configuration."""
    config = Configuration(topdir=topdir)
    path = config.get("manifest.path")
    if path is None:
        return None
    return Path(topdir, path, config.get("manifest.file") or "west.yml")


def _manifest_repo(manifest_path: Path) -> Path:
    """The manifest repository root, which top-level self-import paths are
    relative to (west: topdir / manifest.path). Taken from the workspace
    configuration when manifest_path is its configured manifest; otherwise
    the manifest's directory, which is what `west init -l <dir>` configures."""
    manifest_path = manifest_path.resolve()
    try:
        topdir = west_topdir(start=manifest_path.parent)
        config = Configuration(topdir=topdir)
        path = config.get("manifest.path")
        file = config.get("manifest.file") or "west.yml"
        if path is not None and (Path(topdir, path, file)).resolve() == manifest_path:
            return Path(topdir, path).resolve()
    except (WestNotFound, MalformedConfig):
        pass
    return manifest_path.parent


class _LocalReader:
    """Files of the manifest repository on disk."""

    def __init__(self, root: Path):
        self.root = root

    def read(self, path: str) -> str | None:
        target = self.root / path
        return target.read_text() if target.is_file() else None

    def list_dir(self, path: str) -> list[str] | None:
        target = self.root / path
        return [p.name for p in target.iterdir()] if target.is_dir() else None


def _expand_self_imports(
    content: str,
    importer_name: str,
    path: str,
    reader: res.RemoteTree | _LocalReader,
    info: _ImportInfo,
    record: bool = True,
) -> list[str]:
    """The manifest `content` (read from `path` via `reader`) as the list of
    manifest documents west should import for it: the self-imported files
    in west's order (a directory imports its .yml files sorted by name),
    each expanded recursively, followed by the content itself with its
    'self: import:' removed. West reads a manifest's self-imports from the
    file system only, and loads them *before* the manifest's own projects
    (first definition wins); handing them back as leading sibling documents
    yields the same projects at the same precedence."""
    manifest = manifest_data(content)
    self_section = manifest.get("self") or {}
    imp = self_section.get("import")
    if imp is None:
        if record:
            _record(info, importer_name, path, manifest)
        return [content]

    paths = imp if isinstance(imp, list) else [imp]
    docs: list[str] = []
    for entry in paths:
        if not isinstance(entry, str):
            # the map form carries filters (name-allowlist, path-prefix, ...)
            # that don't carry over to sibling documents; not emulated
            print(
                f"warning: {importer_name} ({path}): 'self: import: {entry}' "
                "uses the map form, which pin-west cannot resolve; any projects "
                "it defines are left unpinned"
            )
            continue
        sub = reader.read(entry)
        if sub is not None:
            docs.extend(_expand_self_imports(sub, importer_name, entry, reader, info))
            continue
        # a directory imports its .yml files in name order
        names = reader.list_dir(entry)
        if names is None:
            raise res.ResolveError(
                f"{importer_name} ({path}): self-imported '{entry}' not found"
            )
        for name in sorted(names):
            if not name.endswith((".yml", ".yaml")):
                continue
            file = f"{entry}/{name}"
            sub = reader.read(file)
            if sub is None:
                raise res.ResolveError(
                    f"{importer_name} ({path}): self-imported '{file}' not found"
                )
            docs.extend(_expand_self_imports(sub, importer_name, file, reader, info))

    # recorded after the self-imports, in west's load order: provenance
    # goes to the first declaration
    if record:
        _record(info, importer_name, path, manifest)
    # rebuild rather than mutate: `manifest` is the shared parse _record read
    self_section = {k: v for k, v in self_section.items() if k != "import"}
    manifest = {k: v for k, v in manifest.items() if k != "self"}
    if self_section:
        manifest["self"] = self_section
    docs.append(yaml.safe_dump({"manifest": manifest}))
    return docs


def _record(info: _ImportInfo, importer_name: str, path: str, manifest: dict) -> None:
    default_rev = (manifest.get("defaults") or {}).get("revision")
    for proj in manifest.get("projects") or []:
        if isinstance(proj, dict) and (name := proj.get("name")):
            info.provenance.setdefault(name, _provenance(importer_name, path))
            info.declared.setdefault(name, proj.get("revision", default_rev))


def _render_entry(p, revision: str, provenance: str | None, indent: str) -> list[str]:
    key = indent + "  "
    via = f" # {_via_comment(provenance)}" if provenance else ""
    lines = [f"{indent}- name: {p.name}{via}"]
    lines.append(f"{key}url: {p.url}")
    lines.append(f"{key}revision: {revision}")
    lines.append(f"{key}path: {p.path}")
    if p.clone_depth:
        lines.append(f"{key}clone-depth: {p.clone_depth}")
    if p.groups:
        lines.append(f"{key}groups: [{', '.join(p.groups)}]")
    if p.west_commands:
        if len(p.west_commands) == 1:
            lines.append(f"{key}west-commands: {p.west_commands[0]}")
        else:
            lines.append(f"{key}west-commands: [{', '.join(p.west_commands)}]")
    if p.submodules is True:
        lines.append(f"{key}submodules: true")
    elif p.submodules:
        print(
            f"warning: {p.name}: per-path submodules are not supported in the "
            "managed section yet; 'submodules' was dropped"
        )
    if p.userdata:
        lines.append(
            f"{key}userdata: "
            + yaml.safe_dump(p.userdata, default_flow_style=True).strip()
        )
    return lines


def _warn_shadowed_conflicts(base: ManifestFile, info: _ImportInfo) -> None:
    """Warn when an import declares a project the user pins differently."""
    for name, block in base.blocks.items():
        declared = info.declared.get(name)
        if declared is None:
            continue
        if declared not in (block.revision, comment_ref(block.comment)):
            print(
                f"warning: {info.provenance[name]} declares {name} at "
                f"'{declared}', but the manifest pins it differently"
            )


def _report(old: ManifestFile, new: ManifestFile) -> None:
    before = {n: old.blocks[n].revision for n in old.generated}
    after = {n: new.blocks[n].revision for n in new.generated}
    added = after.keys() - before.keys()
    removed = before.keys() - after.keys()
    updated = {n for n in before.keys() & after.keys() if before[n] != after[n]}
    print(
        f"imports: {len(after)} project(s) pinned "
        f"({len(added)} added, {len(removed)} removed, {len(updated)} updated)"
    )
