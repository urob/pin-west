"""Materialize and pin imported (indirect) projects.

The manifest's full import tree is resolved without a workspace: west's own
import machinery runs on an importer callback that fetches each imported
manifest file straight from its project's remote at the pinned revision.
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

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from west.configuration import Configuration, MalformedConfig
from west.manifest import (
    ImportFlag,
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
    mf: ManifestFile, errors: list[str], scope: set[str] | None = None
) -> ManifestFile:
    """Re-resolve the import tree and rebuild the managed section. Returns a
    new ManifestFile; `mf` is left untouched.

    With `scope` (a set of direct project names, from a selective bump), only
    entries whose winning declaration roots in one of those projects are
    re-resolved; other existing entries hold their pinned revision — unless
    their winning declarer changed, detected via the recorded '# via'
    comment, or they are new. Without scope, everything is re-resolved."""
    base = mf.without_generated()
    projects, info = _resolve_full(base.text(), mf.path)
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
    in (e.g. cmsis -> zephyr -> zmk). None if the chain can't be traced."""
    seen = {name}
    while True:
        provenance = info.provenance.get(name)
        if provenance is None:
            return None
        importer = _provenance_importer(provenance)
        if importer in direct:
            return importer
        if importer in seen:
            return None
        seen.add(importer)
        name = importer


def _resolve_full(text: str, manifest_path: Path) -> tuple[list, _ImportInfo]:
    topdir = _workspace_topdir(manifest_path)
    if (manifest_data(text).get("self") or {}).get("import") and not topdir:
        raise ManifestError(
            "top-level 'self: import:' requires an initialized west workspace; "
            "run inside one to resolve imports"
        )

    info = _ImportInfo()

    def importer(project, path):
        declared = str(project.revision)
        revision = declared
        if not is_pinned(revision):
            sha = info.resolve_once(project.url, revision)
            if sha is None:
                raise res.ResolveError(f"cannot resolve '{revision}' on {project.url}")
            revision = sha
        try:
            content = res.fetch_blob(project.url, revision, path)
        except res.ResolveError:
            if revision == declared:
                raise
            # some hosts refuse fetching arbitrary shas; fall back to the
            # declared ref (reopens the snapshot race, but stays functional)
            content = res.fetch_blob(project.url, declared, path)
        if content is None:
            raise res.ResolveError(
                f"cannot fetch '{path}' from {project.url} at {revision}"
            )
        manifest = manifest_data(content)
        _record(info, project.name, path, manifest)
        if topdir and project.is_cloned() and _checkout_matches(project, revision):
            # west resolves this manifest's own 'self: import:' (if any) from
            # the project's working tree; only allow that when the checkout
            # is at the revision we're importing at, so the lock can never
            # mix content from two revisions of the same project
            return content
        return _strip_self_import(content, manifest, project.name, path)

    try:
        if topdir:
            manifest = _from_workspace(text, manifest_path, importer)
        else:
            manifest = Manifest.from_data(text, importer=importer)
    except (ManifestImportFailed, MalformedManifest) as e:
        raise ManifestError(f"import resolution failed: {e}")
    projects = [p for p in manifest.projects if not isinstance(p, ManifestProject)]
    return projects, info


def _checkout_matches(project, revision: str) -> bool:
    """True when the project's working tree is checked out at revision, so
    west's filesystem reads of its self-imports match the pinned content."""
    p = res.git("rev-parse", "HEAD^{commit}", cwd=project.abspath)
    return p.returncode == 0 and p.stdout.strip() == revision


def configured_manifest(topdir: str) -> Path | None:
    """Path of the workspace's configured manifest, None if unconfigured.
    Raises MalformedConfig on a broken configuration."""
    config = Configuration(topdir=topdir)
    path = config.get("manifest.path")
    if path is None:
        return None
    return Path(topdir, path, config.get("manifest.file") or "west.yml")


def _workspace_topdir(manifest_path: Path) -> str | None:
    """Topdir of the workspace whose configured manifest is manifest_path,
    or None (also when the manifest is some other file)."""
    try:
        topdir = west_topdir(start=manifest_path.resolve().parent)
        configured = configured_manifest(topdir)
    except (WestNotFound, MalformedConfig):
        return None
    if configured is None:
        return None
    return topdir if configured.resolve() == manifest_path.resolve() else None


def _from_workspace(text: str, manifest_path: Path, importer) -> Manifest:
    """Resolve in-memory manifest text with workspace context: self-imports
    (top-level and of cloned imported projects) come from the filesystem,
    while project-import *content* still goes through our importer
    (FORCE_PROJECTS) so it is read at the pinned revisions."""
    tmp = manifest_path.resolve().parent / ".pin-west-resolve.yml"
    tmp.write_text(text)
    try:
        return Manifest.from_file(
            tmp, importer=importer, import_flags=ImportFlag.FORCE_PROJECTS
        )
    except (subprocess.CalledProcessError, ValueError) as e:
        raise ManifestError(
            f"workspace manifest resolution failed (is {manifest_path.parent} "
            f"a git repository?): {e}"
        )
    finally:
        tmp.unlink(missing_ok=True)


def _strip_self_import(
    content: str, manifest: dict, project_name: str, path: str
) -> str:
    """West can only resolve an imported manifest's own 'self: import:' (e.g.
    zephyr's 'import: submanifests') from a clone of the project. Without
    one, dropping it is safe: our section only adds pinned overrides, so any
    projects it would define simply stay resolved by west at update time —
    unpinned, as before."""
    self_section = manifest.get("self") or {}
    if "import" not in self_section:
        return content
    print(
        f"warning: {project_name} ({path}): 'self: import: "
        f"{self_section['import']}' cannot be resolved without a clone checked "
        "out at the pinned revision; any projects it defines are left "
        "unpinned (run `west update`, then re-run pin)"
    )
    # rebuild rather than mutate: `manifest` is the shared parse _record read
    self_section = {k: v for k, v in self_section.items() if k != "import"}
    manifest = {k: v for k, v in manifest.items() if k != "self"}
    if self_section:
        manifest["self"] = self_section
    return yaml.safe_dump({"manifest": manifest})


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
