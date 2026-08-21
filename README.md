# GitAcross

[![CI](https://github.com/mdeik/GitAcross/actions/workflows/test.yml/badge.svg)](https://github.com/mdeik/GitAcross/actions)

Mirror releases between git hosts (Gitea, GitHub, local). One commit per release, linear history, with file transforms.

```
local repo  ──→  GitHub       (publish local tags as releases)
Gitea       ──→  GitHub       (mirror dev to public)
GitHub      ──→  local repo   (backup)
...any combo                    (gitea, github, local)
```

## Quick start

### Install

Install from PyPI:

```bash
pip install gitacross
````

For development, install the local checkout in editable mode:

```bash
pip install -e .
```


### Run

```bash
# Lint configuration for errors, invalid keys, and redundant defaults
gitacross --config config.yml --lint

# Automatically fix misplaced keys and remove redundant default options
gitacross --config config.yml --fix

# Preview changes without modifying targets
gitacross --config config.yml --dry-run

# Run full sync
gitacross --config config.yml

# Sync only a specific project
gitacross --config config.yml --project my-project

# Or run directly from repository root
python main.py --config config.yml --lint
python main.py --config config.yml --fix
python main.py --config config.yml

# Or run as a Python module
python -m gitacross --config config.yml --lint
python -m gitacross --config config.yml --fix
python -m gitacross --config config.yml
```

## Reference

### Endpoints

| Type | `source` fields | `target` fields |
|---|---|---|
| **gitea** / **github** | `repo`, `api`, `token`<br>`mode` (default `release`, or `tag`, or `commit`)<br>`include_prereleases` (default false)<br>`include_drafts` (default false) | `repo`, `api`, `token`<br>`branch` (default main) |
| **local** | `path`, `tag_pattern` (default `*`) | `path`, `branch` (default main) |

Tokens use `${VAR}` syntax — resolved from environment.

`enabled` (default `true`) can be set to `false` on any project to temporarily disable or skip it without removing it from your configuration file.

`preserve_description` (default `true`, alias `preserve_release_description`) can be set at the project or endpoint level to preserve the source release notes/body on the target release, or set to `false` to leave the target release description empty.

`sync_assets` (alias `preserve_assets`, `include_assets`) is a **project-level** field that mirrors prebuilt release packages from the source to the target release — so you only need CI on the source platform:

| Value | Behaviour |
|---|---|
| `false` (default) | No assets synced |
| `true` | All assets synced |
| `"*.tar.gz"` | Only assets matching the glob |
| `["*.tar.gz", "*.zip"]` | Only assets matching any listed glob |

`stream_assets: true` pairs with `sync_assets` to stream each asset upload from disk rather than buffering the full file in RAM. Default is `false`. Set to `true` when syncing large prebuilt binaries (hundreds of MB) to avoid out-of-memory errors.

```yaml
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


### `source.mode` — release vs tag vs commit

Remote sources sync from the host's **API releases** by default (`mode: release`): prerelease/draft filtering applies, and `sync_from` must be an API release. Set `mode: tag` to treat **git tags** as releases instead — useful when tags were pushed without creating release objects:

```yaml
source:
  type: gitea
  repo: owner/repo
  api: https://gitea.example.com/api/v1
  token: ${GITEA_TOKEN}
  mode: tag
  sync_from: v2.0.0
```

In `release` mode, a `sync_from` tag that exists only in git (no release object) or is filtered out as prerelease/draft produces a warning and syncs nothing, pointing you at the right option — `mode: tag` or `include_prereleases`/`include_drafts` — instead of silently treating tags as releases.

Synced state is keyed by **tag name**, so switching a repo between `release` and `tag` modes is safe: already-synced tags are skipped regardless of the current mode (older state files keyed by API release id are migrated automatically).

#### `mode: commit` — sync latest HEAD instead of releases

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

### `renderer.ignore`

A list of glob patterns. Matched paths are removed before any operations run.

| Wildcard | Meaning |
|---|---|
| `*` | Matches within a single path segment (does **not** cross `/`) |
| `**` | Matches across any number of directory levels (recursive) |

So `.git/*` would only match direct children like `.git/config`, but miss `.git/refs/heads/main`. Use `.git/**` to delete everything inside. Also, matching the directory name directly (`node_modules`) will remove the whole tree in one shot via `shutil.rmtree`, which is slightly faster than matching each file individually with `node_modules/**`.

```yaml
ignore:
  - node_modules                 # any node_modules/ dir, at any depth
  - "*.secret"                  # only in root (single *, no /)
  - "build/**/*.o"              # any .o file under any build/ dir
  - ToDo.md                      # any file named ToDo.md, at any depth
  - some_folder/node_modules     # node_modules only when inside some_folder/
  - "./some_folder/node_modules"  # root-only variant (anchored to ./)
```

### `renderer.operations`

All operations run top-to-bottom in the order they're listed. This applies at every level:

- **Operation blocks** run in order (e.g. `remove` before `replace` before `add`)
- **Items inside each block** also run in order (e.g. second `replace` item runs after the first)

This matters when later steps depend on earlier ones — for example, a `rename` moving a file, then a `replace` modifying the renamed target.

#### `remove`

| Field | Required | Default | Description |
|---|---|---|---|
| `path` | yes | — | Path or pattern to remove |
| `pattern` | no | `literal` | `literal`, `glob`, or `regex` |

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

Only UTF-8 text files are scanned. Binary files are skipped.

```yaml
- replace:
    - search: https://gitea\.example\.com
      replace: https://github.com
      pattern: regex
      glob: "*.md"
    - search: http://old-url.com
      replace: https://new-url.com
      pattern: literal
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
      pattern: "MIT"
```

### `retry`

| Field | Default | Description |
|---|---|---|
| `max_attempts` | 3 | Number of retries before giving up |
| `backoff_seconds` | 2 | Base delay (doubles each attempt) |

### CLI

```
python -m sync.main --config PATH [--project NAME] [--dry-run] [-v]
```

| Flag | Description |
|---|---|
| `--config` | Path to config file (required) |
| `--project` | Sync only one project (by name) |
| `--dry-run` | Preview changes without committing or pushing |
| `-v` | Debug logging |

## How it works

1. Fetch releases from source (paginated API or local tags)
2. Filter out already-synced releases (tracked in `.gitsync/state.yml`)
3. For each new release (oldest first):
   - Export tag's file tree via `git archive`
   - Apply `ignore` patterns, then `operations` in order
   - Commit on top of target branch (linear history)
   - Create annotated tag
   - Push (remote targets) or populate working tree (local targets)
   - Create release via API (remote targets)
4. Persist state atomically

Re-running is idempotent — already-synced releases are skipped.
