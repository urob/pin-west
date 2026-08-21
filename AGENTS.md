# pin-west — agent notes

Pins revisions in west manifests to exact commit SHAs (pinact, but for
Zephyr/ZMK's west). Python 3.13, uv-managed. README.md documents the CLI.

## Commands

- `uv run pytest -q` — full test suite (offline, ~10s)
- `uvx ruff check`, `uvx ruff format`, `uv run ty check` — all must be clean
  before finishing
- `uv run pin-west <pin|bump|check> ...` — run the tool
- `nix build .#pin-west -L` — flake build; runs the same suite in the sandbox
  (`flake.nix` + `package.nix`; nixpkgs ships an older `uv_build` than
  `pyproject.toml` pins, so `package.nix` relaxes that bound in `postPatch`)

## Architecture (src/pin_west/)

- `manifest.py` — text-level model: locates project blocks, rewrites/inserts
  `revision:` lines in place. Never touches any other byte.
- `resolve.py` — remote resolution: `git ls-remote` (branches/tags/peeling,
  works on filesystem paths) + GitHub REST API (latest release, sha checks).
- `tracking.py` — pure bump decision logic: classify what a pin follows
  (release tag / floating series alias / branch / untracked), scoped
  candidate selection. Unit-tested without network.
- `imports.py` — indirect-deps lockfile: resolves the full `import:` tree
  workspace-free (west's `importer=` callback + `fetch_blob`, one file GET
  per importing project) and regenerates the marker-delimited managed
  section. Managed entries carry only an inline `# via <declarer>` comment
  (parsed back for scoping — it is load-bearing there) and are excluded from
  bump's tracking logic; their revisions are the importing project's call.
  `pin`/unselective `bump` refresh everything (floating declarations inside
  imported manifests follow their ref — intended); `bump PROJECT`
  re-resolves only entries rooting in the selection (winner-flip detection
  via the `# via` comment forces re-resolution). Non-sha declarations are
  resolved once per (url, ref) and cached so content fetch and pin share a
  snapshot. `self: import:` is expanded by us, never by west (west reads
  self-imports from the file system only): the importer strips it and
  returns the self-imported files (fetched at the pinned sha; directories
  via `list_dir`, `.yml` sorted) as *leading* sibling documents — west's
  importer accepts a list, imported in order, and west itself loads
  self-imports before a manifest's own `projects:` (first definition
  wins), so this reproduces its native precedence. The top-level
  manifest's self-import goes through the same path via a synthetic
  wrapper manifest (`_SELF`) whose importer reads the manifest repository
  on disk — `topdir/manifest.path` when a workspace configures this
  manifest, else the manifest's directory (what `west init -l <dir>`
  gives); never the git toplevel. Entries it yields are `# via self
  (<file>)` and root in no direct project: scoped bumps hold them. Never
  use `Manifest.from_file`/workspace state here; clones are never
  consulted. Remote files are read through `res.RemoteTree`, one shallow
  fetch per (url, revision) for non-GitHub hosts. Map-form self-imports
  (filters) are
  warned about and dropped. An empty managed section keeps its markers:
  the section's presence is the regeneration opt-in.
- `action/summary.py` (outside the package) — renders the PR-body change
  overview for `action.yml` from the pre-/post-bump manifest; runs via
  `uv run --with pin-west` against the published package, so it must only
  use stable `pin_west` API. Tested in `tests/test_action_summary.py`.
- `cli.py` — argparse subcommands `pin`, `bump`, `check`; manifest discovery;
  `--local` workspace mode.

## Design decisions (settled — don't relitigate)

- Use the `west` package as a **library only**, never its CLI. Parse with
  `Manifest.from_data(..., ImportFlag.IGNORE)` — never `from_file` (breaks if
  a stray `.west` dir sits near the manifest).
- Edits are surgical text replacements, never a YAML round-trip: user
  formatting and comments must survive byte-for-byte.
- Direct deps always; indirect deps via `pin --include-imports` (opt-in by
  marker section, then auto-maintained). A dependabot-style GitHub Action is
  still a future goal.
- Trailing comments: `# v0.3.0` for tags, `# main (YYYY-MM-DD)` (UTC) for
  branches. They declare tracking intent and are verified against the remote
  before use; tag pins also self-identify via the sha↔tag map, so a lost
  comment degrades gracefully rather than breaking anything.
- Bump follows the tracking state (see README "Tracking" and `tracking.py`):
  release pins → latest release (scoped by `--patch`/`--minor`), floating
  aliases (`v0.3` with `v0.3.x` siblings) and branches → follow the ref.
  Guards: no downgrades, and moved *exact* release tags warn + hold (moved
  aliases are normal). Tag-vs-branch name ties resolve to the tag — matches
  git/west; verified against west's fetch path, don't change.
- Bare `bump` = latest; flags only ever narrow (`--patch`, `--minor`,
  `--releases-only`). This direction was chosen deliberately.
- GitHub token precedence: `--gh-token` > `GITHUB_TOKEN` > `GH_TOKEN` >
  `gh auth token`.

## Testing conventions

- Everything runs offline: `tests/conftest.py` provides `GitRemote`, real
  local git repos standing in for GitHub (ls-remote/west accept plain paths;
  expected shas come from `git rev-parse`, never hardcoded).
- Parsing patterns get inline-snippet unit tests in `test_manifest.py`;
  behavior gets end-to-end tests in `test_cli.py` via `main([...])`.
- `tests/data/zmk-config.yml` is a golden real-world sample: it must
  round-trip byte-identically, and tests assert its structure — update the
  assertions in `TestRealWorldSample` if you change it. Keep it realistic;
  don't turn it into a pattern zoo (patterns belong in snippets).
