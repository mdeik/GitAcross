# GitAcross

**Mirror releases between git hosts.** When a new release appears on one host, GitAcross copies it to another — one clean commit per release, with the option to remove or rewrite files along the way.

```
local repo  ──>  GitHub       (publish local tags as releases)
Gitea       ──>  GitHub       (mirror dev to public)
GitHub      ──>  local repo   (backup)
...any combo                  (gitea, github, local)
```

**Highlights:**

- **Clean history** — every release lands as one commit on top of the last, so the target branch stays linear and readable
- **Safe to re-run** — already-synced releases are skipped, so it works on a schedule or in CI
- **File transforms** — exclude files or rewrite their contents before publishing
- **Flexible endpoints** — Gitea, GitHub, and local repositories, in any combination
- **Config guardrails** — lint and auto-fix your config before it runs

## Contents

- [Quick start](#quick-start)
  - [Install](#install)
  - [Config](#config)
  - [Run](#run)
- [How it works](#how-it-works)
- [Reference](#reference)
  - [Project anatomy](#project-anatomy)
  - [Endpoints — source & target](#endpoints--source--target)
  - [Source mode: release, tag, or commit](#source-mode-release-tag-or-commit)
  - [Excluding files](#excluding-files)
  - [Transforming files](#transforming-files)
  - [Retries](#retries)
- [How to use](#how-to-use)
  - [CLI](#cli)
  - [Python API](#python-api)

## Quick start

### Install

```bash
pip install gitacross
```

For development, install the local checkout:

```bash
pip install -e .
```

### Tests

```bash
pip install -e .[dev]   # pytest + pytest-cov
pytest                  # run the test suite
pytest --cov=gitacross  # run with a coverage floor (90% line + branch)
ruff check src tests    # lint
```

### Config

GitAcross reads a YAML file listing the mirrors you want. Each entry in the `projects` list is a **project**: it has a `source` (where releases come from) and a `target` (where they go).

Start from the fully commented [config.yml.example](config.yml.example) — it covers remote-to-remote mirrors, prebuilt asset sync, local repos, backups, and commit-mode branch syncing:

```bash
cp config.yml.example config.yml
```

Then edit `config.yml` to fill in your own repos and tokens. Tokens like `${GITEA_TOKEN}` are read from environment variables — keep secrets out of the file. See [Project anatomy](#project-anatomy) for everything else a project can have.

### Run

```bash
# Preview what would change (no commits, no pushes)
gitacross --config config.yml --dry-run

# Check the config for errors and redundant settings
gitacross --config config.yml --lint

# Do the sync
gitacross --config config.yml
```

Run it again later — releases that were already synced are skipped, so nothing is duplicated. Use `--project-name my-project` to sync a single project. See [CLI](#cli) for all flags, or the [Python API](#python-api) to drive GitAcross from code.

## How it works

For each project, GitAcross watches the **source** and mirrors new releases to the **target**:

1. Fetch the list of releases from the source
2. Skip releases that were already synced (remembered in a local state file)
3. For each new release, oldest first:
   - Check out the release's file tree
   - Remove excluded files, then apply any file transforms
   - Commit the result on the target's branch — one commit per release
   - Tag the commit and publish the release on the target
4. Save the sync state

On remote targets the commit is pushed; on local targets the working tree is updated instead. Re-running the same command later only syncs new releases — already-synced ones are skipped.

## Reference

### Project anatomy

A config file starts with a `projects` list — each entry is one mirror and needs a `name`, a `source`, and a `target`; everything else is optional.

| Key | What it does | More |
|---|---|---|
| `name` | Unique name for the project | — |
| `source` | Where releases come from | [Endpoints](#endpoints--source--target) |
| `target` | Where releases are mirrored to | [Endpoints](#endpoints--source--target) |
| `enabled` | `false` pauses the project without deleting it | [Endpoints](#endpoints--source--target) |
| `renderer` | File handling: `ignore` (exclude), `operations` (transform), `author` (commit identity) | [Excluding files](#excluding-files) · [Transforming files](#transforming-files) |
| `retry` | Retry settings for API calls | [Retries](#retries) |
| `sync_assets`, `stream_assets` | Mirror prebuilt release files to the target | [Endpoints](#endpoints--source--target) |
| `preserve_description`, `release_description`, `commit_message` | Release notes and commit messages | [Endpoints](#endpoints--source--target) |

### Endpoints — source & target

`source` and `target` each describe one git host:

| Type | `source` fields | `target` fields |
|---|---|---|
| **gitea** / **github** | `repo`, `api`, `token`<br>`mode` (default `release`, or `tag`, or `commit`)<br>`include_prereleases` (default false)<br>`include_drafts` (default false) | `repo`, `api`, `token`<br>`branch` (default main) |
| **local** | `path`, `tag_pattern` (default `*`) | `path`, `branch` (default main) |

Tokens use `${VAR}` syntax — resolved from environment variables.

For local **targets**, the `path` does not have to exist yet: if the directory is
missing or is not already a git repository, GitAcross creates the directory and
runs `git init` there before committing. If a repository already exists at that
path it is opened as-is — existing git metadata is never re-initialised or
overwritten (bare repositories and broken `.git` markers are refused with an
error). Local **sources** must point at an existing git repository.

| Option | Description |
|---|---|
| [`enabled`](#enabled) | Disable a project without deleting it |
| [`preserve_description`](#preserve_description) | Copy source release notes to the target release |
| [`release_description`](#release_description) | Format target release notes from a template |
| [`commit_message`](#commit_message) | Override target commit messages |
| [`sync_assets`](#sync_assets) | Mirror prebuilt release assets to the target |
| [`stream_assets`](#stream_assets) | Stream asset uploads from disk (low memory) |

#### `enabled`

Set to `false` to pause a project without removing it from the config. Default `true`.

#### `preserve_description`

Copy the source release notes/body to the target release. Default `true`. Set at the project or endpoint level; `false` leaves the target release description empty.

#### `release_description`

Format the target release notes from a template. Placeholders: `{body}`, `{description}`, `{tag}`, `{commit_sha}`, `{short_sha}`, `{project_name}`, `{name}`, `{source_date}`.

#### `commit_message`

Custom commit message for the target commits. Default: `"Release {tag}"` or `"Sync commit {short_sha}"`. Placeholders: `{tag}`, `{commit_sha}`, `{short_sha}`, `{project_name}`, `{name}`, `{source_date}`, `{body}`, `{description}`.

#### `sync_assets`

Mirror prebuilt release packages from the source to the target release — so you only need CI on the source platform. Project-level field:

| Value | Behaviour |
|---|---|
| `false` (default) | No assets synced |
| `true` | All assets synced |
| `"*.tar.gz"` | Only assets matching the glob |
| `["*.tar.gz", "*.zip"]` | Only assets matching any listed glob |

```yaml
projects:
  - name: my-project
    sync_assets:            # build on Gitea, upload prebuilts to GitHub
      - "*.tar.gz"
      - "*.zip"
      - "*.deb"
      - "*-checksums.txt"
    stream_assets: true     # stream uploads from disk — avoids buffering in RAM
    source:
      type: gitea
      ...
    target:
      type: github
      ...
```

#### `stream_assets`

Stream each asset upload directly from the temporary download directory on disk (cleaned up after syncing) instead of buffering the whole file in memory. Default `false`; set to `true` when syncing large prebuilt binaries (hundreds of MB) to avoid out-of-memory errors.

### Source mode: release, tag, or commit

Remote sources sync from the host's **API releases** by default. Two alternatives are available: git tags, or the latest commit of a branch. A `sync_from` key on the source sets the starting point — only releases from that tag onward are synced.

Synced state is keyed by **tag name**, so switching a repo between `release` and `tag` modes is safe: already-synced tags are skipped regardless of the current mode (older state files keyed by API release id are migrated automatically).

| Mode | What gets synced | When to use |
|---|---|---|
| [`release`](#release--api-releases-default) (default) | API releases, with prerelease/draft filtering | Normal release workflow |
| [`tag`](#tag--git-tags) | Git tags (no release objects needed) | Tags pushed without releases |
| [`commit`](#commit--sync-latest-head) | Latest commit of the source branch | Keep the target permanently in sync |

#### `release` — API releases (default)

Remote sources sync from the host's **API releases** by default: prerelease/draft filtering applies, and `sync_from` must be an API release.

In `release` mode, a `sync_from` tag that exists only in git (no release object) — or is filtered out as prerelease/draft — produces a warning and syncs nothing. That points you at the right option, `mode: tag` or `include_prereleases`/`include_drafts`, instead of silently treating tags as releases.

#### `tag` — git tags

Set `mode: tag` to treat **git tags** as releases instead — useful when tags were pushed without creating release objects. `sync_from: v2.0.0` starts at that tag, skipping older ones:

```yaml
source:
  type: gitea
  repo: owner/repo
  api: https://gitea.example.com/api/v1
  token: ${GITEA_TOKEN}
  mode: tag
  sync_from: v2.0.0
```

#### `commit` — sync latest HEAD

Set `mode: commit` to sync the **current HEAD of the source branch** each time the script runs, rather than iterating over releases or tags. No tag or release is created on the target — only a plain commit is pushed.

```yaml
source:
  type: gitea
  repo: owner/repo
  api: https://gitea.example.com/api/v1
  token: ${GITEA_TOKEN}
  mode: commit
  branch: main   # optional — which branch to read HEAD from (auto-detected if omitted)
```

| Behaviour | Detail |
|---|---|
| **What gets synced** | Single snapshot of the current branch HEAD |
| **No tag or release** | Only a plain commit is pushed to the target branch |
| **`branch`** | Which source branch to read HEAD from. Auto-detects `origin/HEAD`, then tries `main`/`master`/`trunk` |
| **State key** | Commit SHA (not tag name). Already-synced SHAs are skipped |
| **Idempotent** | Re-running with same HEAD is a no-op (same SHA already in state) |
| **State purged** | Re-commits current HEAD snapshot; git sees no diff if nothing changed → no-op commit |

### Excluding files

Some files shouldn't be mirrored at all. The project's `renderer` block accepts an `ignore` list of glob patterns — matched paths are removed from every release before anything else runs:

```yaml
renderer:
  ignore:
    - node_modules                 # any node_modules/ dir, at any depth
    - "*.secret"                   # only in root (single *, no /)
    - "build/**/*.o"               # any .o file under any build/ dir
    - ToDo.md                      # any file named ToDo.md, at any depth
    - some_folder/node_modules     # node_modules only when inside some_folder/
    - "./some_folder/node_modules"  # root-only variant (anchored to ./)
```

| Wildcard | Meaning |
|---|---|
| `*` | Within a single path segment (does **not** cross `/`) |
| `**` | Across any number of directory levels (recursive) |

Two shortcuts worth knowing:

- `.git/*` matches only direct children like `.git/config` and misses deeper files such as `.git/refs/heads/main` — use `.git/**` to delete everything inside.
- Naming a directory directly (`node_modules`) removes the whole tree in one shot, which is slightly faster than listing `node_modules/**`.

### Transforming files

The project's `renderer` block also accepts an `operations` list of file transformations. They run top-to-bottom in the order listed, both across blocks and within them — a later step can rely on an earlier one (e.g. `rename` a file, then `replace` text inside it).

| Operation | What it does |
|---|---|
| [`remove`](#remove) | Delete files or paths |
| [`rename`](#rename) | Move or rename a file |
| [`replace`](#replace) | Find-and-replace text in files |
| [`add`](#add) | Create new files (parent dirs auto-created) |
| [`validate`](#validate) | Assert file/string conditions, abort on failure |

#### `remove`

| Field | Required | Default | Description |
|---|---|---|---|
| `path` | yes | — | Path or pattern to remove |
| `pattern` | no | `literal` | How to match: `literal`, `glob`, or `regex` |

```yaml
- remove:
    - path: .gitea                        # literal path
    - path: "*.secret"
      pattern: glob                       # glob pattern
    - path: "build\\d+"                   # regex matches path
      pattern: regex
```

#### `rename`

| Field | Required | Default | Description |
|---|---|---|---|
| `from` | yes | — | Source path |
| `to` | yes | — | Destination path |
| `pattern` | no | `literal` | Only `literal` is implemented |

```yaml
- rename:
    - from: .gitea
      to: .github
      pattern: literal
```

#### `replace`

| Field | Required | Default | Description |
|---|---|---|---|
| `search` | yes | — | String (literal) or pattern (regex) to find |
| `replace` | yes | — | Replacement text |
| `pattern` | no | `literal` | `literal` or `regex` |
| `glob` | no | all files | Only modify files matching this glob |
| `path` | no | — | Only modify this exact relative file path (takes precedence over `glob`) |
| `case_sensitive` | no | `true` | `false` matches any casing |
| `match_case` | no | `false` | `true` adapts each replacement to the casing it matched (see below) |

Only UTF-8 text files are scanned. Binary files are skipped.

`case_sensitive: false` makes the search case-insensitive — `search: gitea` also matches `Gitea` and `GITEA` (combines with `pattern: regex` too).

`match_case: true` (handy with `case_sensitive: false`) adjusts each replacement to the casing of the matched text instead of writing it verbatim. With `search: gitea`, `replace: github`:

| Matched text | Replacement |
|---|---|
| `gitea` | `github` |
| `Gitea` | `Github` |
| `GITEA` | `GITHUB` |

In `regex` mode, backreferences (e.g. `\1`) are expanded before the casing adaptation is applied.

```yaml
- replace:
    - search: https://gitea\.example\.com
      replace: https://github.com
      pattern: regex
      glob: "*.md"
    - search: http://old-url.com
      replace: https://new-url.com
      pattern: literal
    - search: gitea
      replace: github
      case_sensitive: false
      match_case: true
```

#### `add`

| Field | Required | Description |
|---|---|---|
| `path` | yes | File path to create (parent dirs auto-created) |
| `content` | yes | File contents |

```yaml
- add:
    - path: .github/FUNDING.yml
      content: |
        github: myuser
    - path: RELEASE_NOTES.md
      content: |
        # Release Notes
        ...
```

#### `validate`

| Field | Required | Description |
|---|---|---|
| `assert` | yes | `file_exists`, `file_absent`, `string_exists`, `string_absent` |
| `path` | yes | File path to check |
| `pattern` | for string checks | Text to search for |
| `case_sensitive` | no (default `true`) | `false` makes `string_exists`/`string_absent` match any casing |

Aborts the entire release if any assertion fails.

```yaml
- validate:
    - assert: file_exists
      path: README.md
    - assert: string_absent
      path: LICENSE
      pattern: "Gitea"
    - assert: string_exists
      path: README.md
      pattern: "mit"
      case_sensitive: false
```

### Retries

Retry settings for API calls, configured in the project's `retry` block.

| Field | Default | Description |
|---|---|---|
| `max_attempts` | 3 | Number of retries before giving up |
| `backoff_seconds` | 2 | Base delay (doubles each attempt) |

## How to use

GitAcross can be driven from the command line or called directly from Python.

### CLI

```
gitacross --config PATH [--project-name NAME] [--workdir PATH] [--dry-run] [--reset] [--clean-cache] [--lint] [--fix] [-v]
```

| Flag | Description |
|---|---|
| `--config PATH` | Config file to use (required) |
| `--project-name NAME` | Sync only the project with this name |
| `--workdir PATH` | Where state and cache live (default: `.gitsync`) |
| `--dry-run` | Preview changes without committing or pushing |
| `--reset` | Clear saved state and cache before running (fresh start) |
| `--clean-cache` | Delete mirror caches no longer referenced by the config (e.g. after changing a repo's host or name in the config) |
| `--lint` | Check the config for YAML errors, invalid settings, and redundant options |
| `--fix` | Fix misplaced keys and remove redundant options in the config |
| `-v, --verbose` | Debug logging |

### Python API

Prefer code over the CLI? GitAcross is importable from Python — handy for CI scripts and webhooks. Expand the use case that fits your situation:

<details>
<summary>Sync everything — one call</summary>

```python
import gitacross

results = gitacross.run("config.yml")

for r in results:
    print(f"{r['project']}: synced={r['synced']} releases={r['releases_synced']}")
    if r["error"]:
        print(f"  error: {r['error']}")
```

</details>

<details>
<summary>Sync one project, preview first</summary>

```python
# Preview only — nothing is committed or pushed
results = gitacross.run(
    "config.yml",
    project_name="my-project",
    dry_run=True,
    work_dir="/data/custom_dir",
)
```

</details>

<details>
<summary>Start fresh — ignore saved state</summary>

```python
# Clears saved state and cache, so every release is treated as new
results = gitacross.run("config.yml", reset=True)
```

</details>

<details>
<summary>Full control — loop over projects yourself</summary>

```python
config = gitacross.Config("config.yml")

for project_config in config.projects:
    if project_config.enabled:
        gitacross.sync_project(project_config, ".gitsync", dry_run=False)
```

</details>

<details>
<summary>No config file — build a project inline</summary>

```python
# ${VAR} tokens still resolve from the environment
project_config = gitacross.ProjectConfig({
    "name": "my-project",
    "source": {
        "type": "gitea",
        "repo": "owner/repo",
        "api": "https://gitea.example.com/api/v1",
        "token": "${GITEA_TOKEN}",
    },
    "target": {
        "type": "github",
        "repo": "owner/repo",
        "api": "https://api.github.com",
        "token": "${GITHUB_TOKEN}",
    },
})
gitacross.sync_project(project_config, ".gitsync")
```

</details>

<details>
<summary>Lint and auto-fix the config from code</summary>

```python
report = gitacross.lint_config("config.yml", print_output=False)
if not report.is_valid:
    print([e.message for e in report.errors])
    gitacross.fix_config("config.yml", write_back=True)
```

</details>

<details>
<summary>Sync from YAML held in a variable — no file needed</summary>

```python
# ${VAR} tokens still resolve from the environment
config = gitacross.Config.from_yaml_string("""
projects:
  - name: my-mirror
    source:
      type: gitea
      repo: owner/repo
      api: https://gitea.example.com/api/v1
      token: ${GITEA_TOKEN}
    target:
      type: github
      repo: owner/repo
      api: https://api.github.com
      token: ${GITHUB_TOKEN}
""")
gitacross.run(config, dry_run=True)
```

</details>

All public symbols are importable directly from `gitacross`:

| Symbol | What it does |
|---|---|
| `run(config, project_name=None, dry_run=False, reset=False, work_dir=".gitsync")` | **Primary entry point.** Sync from a config — a `Config` instance, a path, or an open file object
| `sync_project(project_config, work_dir=".gitsync", dry_run=False)` | Sync one project's new releases (respects `project_config.enabled`); state and cache live in `work_dir`. Returns dicts with `tag`, `source_commit`, `target_commit`, `source_date` |
| `lint_config(config, print_output=True)` | Lint a config (path or open file object) → `LintReport` |
| `fix_config(config, write_back=True, print_output=True)` | Fix misplaced/redundant options (path only — writes back to the file) → `FixReport` |
| `Config(config_source)` | Load a config from a path or open file object; exposes `.projects`. `Config.from_yaml_string(content)` loads a config from raw YAML text (`str` or `bytes`) — no file or stream needed |
| `ProjectConfig(raw)` | Build one mirror project from a raw config dict (see the “No config file” example). Fields: `name`, `enabled`, `source`, `target`, `renderer`, `retry`, `preserve_description`, `sync_assets`, `stream_assets`, `commit_message`, `release_description` |
| `ConfigLinter()` | Collect lint issues programmatically: `lint_file(config)`, `lint_yaml_string(content)`; results accumulate in `.issues` |
| `ConfigFixer()` | Fix a config programmatically: `fix_yaml_string(content)` → `FixReport`; actions recorded in `.fixes` |
| `LintIssue(severity, message, project_name=None, key=None)` | One lint finding |
| `FixIssue(message, project_name=None)` | One applied fix |
| `LintReport(issues)` | Lint results: `.issues`, `.errors`, `.warnings`, `.redundant`, `.is_valid`, `.format_text()` |
| `FixReport(fixes, content, is_valid, error=None)` | Fix results: `.fixes`, `.content`, `.is_valid`, `.error`, `.format_text()` |
| `LintSeverity` | Severity levels used by `LintIssue`: `LintSeverity.ERROR`, `LintSeverity.WARNING`, `LintSeverity.REDUNDANT` |
