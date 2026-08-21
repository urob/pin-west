# pin-west

[![PyPI](https://img.shields.io/pypi/v/pin-west.svg)](https://pypi.org/project/pin-west/)

Pin and update revisions in [west
manifests](https://docs.zephyrproject.org/latest/develop/west/manifest.html) to
exact commit SHAs.

```diff
manifest:
   projects:
     - name: zephyr
-      revision: 628a0d85e36938dddb6f0dfc6dc902de7359711c # v3.5.0+zmk-fixes (2025-07-10)
+      revision: dacab4875df72109b96cc8977547a0dc04875bcd # v3.5.0+zmk-fixes (2026-08-18)
     - name: zmk
-      revision: v0.3
+      revision: edf5c0814fd3ea202e43aad2d68fd32e882a518c # v0.3
     - name: zmk-helpers
-      revision: main
+      revision: bc114546392b4615ac90a99140eaf21dde31209d # main (2026-08-18)
```

With a few noted exceptions, `pin-west` needs neither an
initialized west workspace nor any clones. It ever only edits `revision` fields, 
keeping all other formatting and comments byte-for-byte.

## Setup

**uv/pip**

With [uv](https://docs.astral.sh/uv/), run it without installing:

```sh
uvx pin-west check
```

Or install it persistently:

```sh
uv tool install pin-west   # or: pipx install pin-west
```

**nix**

With [Nix](https://nixos.org), run it without installing:

```sh
nix run github:urob/pin-west -- check
```

The flake also exposes `packages` and `overlays` defaults providing `pin-west`, and a `devShell`
with Python and `uv` for development.

## Usage

By default, pin-west runs on `./west.yml` if it exists, or otherwise
— when run inside an initialized west workspace — the workspace's
configured manifest. Use `-f/--manifest` to explicitly specify the
manifest.

```sh
# Pin every unpinned revision to the current head of its branch/tag:
pin-west pin [-f west.yml] [--dry-run]

# Inside an initialized west workspace: pin to the locally resolved
# revisions instead:
pin-west pin --local

# Also materialize and pin imported (indirect) projects into a managed
# section at the end of `projects:` (see "Imported projects" below):
pin-west pin --include-imports

# Bump projects (default: all) along what they track (see below): release
# pins to the latest release, branch/series pins to the tracked ref's head:
pin-west bump [PROJECT ...] [--dry-run]

# Constrain release-tracked bumps to the same major.minor / major series:
pin-west bump --patch  # or: --minor

# Skip everything that doesn't track a release tag or series:
pin-west bump --releases-only

# Bump to an explicit branch or tag (switches what the pin tracks):
pin-west bump zephyr --ref v4.1.0+zmk-fixes

# Check that every revision is pinned to a full sha, that pinned shas exist
# on their remote, and that pins are consistent with their trailing comment
# (when one exists). Exit non-zero on any failure:
pin-west check

# Flags select a subset of the checks; e.g. only the pin check:
pin-west check --pinned  # or: --shas, --comments
```

Commands that talk to the GitHub API (`bump` and `check`) find a token via
`--gh-token`, then `$GITHUB_TOKEN` / `$GH_TOKEN`, then `gh auth token`; without
one, unauthenticated requests are rate-limited to 60/hour. Non-GitHub remotes
work throughout — they just skip the release lookup and the ancestry check.

## Tracking

Pinned revisions get a trailing comment recording what they track. If a
tracking comment exists, `bump` uses it to infer the declared update scope
as follows:

- **Exact release** (`# v0.3.0`): bump to the latest release (limited by
  `--patch`/`--minor` if specified). Targets *older* than the pin are
  skipped, and an exact release tag that no longer points at the pinned commit
  triggers a warning and holds the pin — release tags shouldn't move; use
  `--ref` to force the move.
- **Floating release** (`# v0.3`, when refined tags like `v0.3.x` exist):
  follow the alias tag wherever it moves; the comment keeps the alias.
- **Branch** (`# main (date)`): follow the branch head. Scope flags don't
  apply and are ignored with a warning.

Pinned projects **without a tracking comment** are interpreted as exact releases
if a matching release tag exists (see above), and otherwise fall back to
  latest-release → highest version tag → default branch.

**Unpinned projects** infer the declared intent from the manifest's `revision`
field. Names that are both a branch and a tag resolve to the tag. 

**Explicit scopes:** `bump --ref GITREF` overwrites the inferred update scope
with `GITREF` (e.g., `main` or `v0.3`).

**Auditing tracking comments:** `check --comments` audits the tracking comments:
exact-release pins must match the tag's commit, floating-release pins must pin a
member of the release series, and branch pins must be ancestors of the branch's
head (GitHub only).

## Pinning indirect dependencies

By default, only projects explicitly specified in the manifest are pinned or
updated. Use `pin --include-imports` to resolve and lock the entire manifest
instead.

This will embed a dependency section into the manifest, turning it into a
self-contained lockfile. Any future `pin-west` runs will continue locking
dependencies, re-resolving them when their parent project is re-pinned or
updated (a selective `bump PROJECT` only refreshes dependencies in the
tree of that project). 

Deleting the dependency section in the manifest turns off dependency locking
(until re-enabled with `--include-imports`).

## GitHub Action

The repository includes a Dependabot-like action for automated manifest
maintenance. It runs `pin-west bump` and opens a pull request when anything
changed. To set up a scheduled workflow to the manifest repository, add
`.github/workflows/bump-west.yml` with the following content:

```yaml
name: West updates

on:
  schedule:
    - cron: "0 5 * * 1" # Mondays 05:00 UTC
  workflow_dispatch:

permissions:
  contents: write # push the bump branch
  pull-requests: write # open the pull request

jobs:
  bump:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: urob/pin-west@main
        with:
          scope: minor # optional: latest (default), minor, or patch
```

The action uses the repository's `west.yml` if there is exactly one; otherwise
it fails and asks for `manifest: path/to/manifest.yml`, which overrides the
detection. Use `scope` to narrow the update scope, `token` to overwrite the
default workflow token, and `pr-branch` and `pr-title` to control the pull
request (`pr-branch` is force-pushed, so repeated runs update a single open PR).

Note that pull requests created with the default workflow token don't trigger
other workflows; pass a PAT or a GitHub App token as `token` if CI should run
on the bump PRs.

