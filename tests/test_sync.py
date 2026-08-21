#!/usr/bin/env python3
"""Self-check for the sync tool. No test framework — just asserts.

Run: python3 test_sync.py
"""

import email.message
import io
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_file(root, path, content="hello"):
    full = root / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    return full


def _make_git_repo(path):
    """Init a bare or non-bare git repo at *path* and return it."""
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    return path


def _git_commit(path, msg="init"):
    subprocess.run(
        ["git", "-C", str(path), "add", "-A"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", msg, "--allow-empty"],
        check=True,
        capture_output=True,
    )


def _git_tag(path, tag):
    subprocess.run(
        ["git", "-C", str(path), "tag", tag], check=True, capture_output=True
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_source_target():
    from gitacross.config import Config

    yaml_text = """
projects:
  - name: myapp
    source:
      type: gitea
      repo: owner/repo
      api: https://gitea.example.com/api/v1
      token: ${GITEA_TOKEN}
      include_prereleases: true
    target:
      type: github
      repo: owner/repo
      api: https://api.github.com
      token: ${GITHUB_TOKEN}
      branch: develop
    retry:
      max_attempts: 5
      backoff_seconds: 3
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(yaml_text)
        config_path = f.name

    try:
        cfg = Config(config_path)
        assert len(cfg.projects) == 1
        proj = cfg.projects[0]
        assert proj.name == "myapp"

        # Source
        assert proj.source.type == "gitea"
        assert proj.source.repo == "owner/repo"
        assert proj.source.is_remote is True
        assert proj.source.include_prereleases is True
        assert proj.source.include_drafts is False

        # Target
        assert proj.target.type == "github"
        assert proj.target.repo == "owner/repo"
        assert proj.target.branch == "develop"

        # Retry
        assert proj.retry.max_attempts == 5
        assert proj.retry.backoff_seconds == 3

        # Token resolution
        os.environ["GITEA_TOKEN"] = "tkn"
        cfg2 = Config(config_path)
        assert cfg2.projects[0].source.token == "tkn"
        del os.environ["GITEA_TOKEN"]

        print("  ✓ config: source/target")
    finally:
        os.unlink(config_path)


def test_config_local():
    from gitacross.config import Config

    yaml_text = """
projects:
  - name: local-pub
    source:
      type: local
      path: /tmp/myrepo
      tag_pattern: "v*"
    target:
      type: local
      path: /tmp/mirror
      branch: releases
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(yaml_text)
        config_path = f.name

    try:
        cfg = Config(config_path)
        proj = cfg.projects[0]
        assert proj.source.type == "local"
        assert proj.source.path == "/tmp/myrepo"
        assert proj.source.tag_pattern == "v*"
        assert proj.source.is_remote is False

        assert proj.target.type == "local"
        assert proj.target.path == "/tmp/mirror"
        assert proj.target.branch == "releases"
        assert proj.target.is_remote is False

        print("  ✓ config: local")
    finally:
        os.unlink(config_path)


def test_config_token_resolution():
    from gitacross.config import Config

    yaml_text = """
projects:
  - name: test
    source:
      type: gitea
      repo: a/b
      api: https://gitea.example.com
      token: ${MY_VAR}
    target:
      type: github
      repo: a/b
      api: https://api.github.com
      token: inline-token
      branch: main
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(yaml_text)
        config_path = f.name

    try:
        cfg = Config(config_path)
        proj = cfg.projects[0]
        assert proj.target.token == "inline-token"
        assert proj.source.token == ""

        os.environ["MY_VAR"] = "resolved-token"
        cfg2 = Config(config_path)
        assert cfg2.projects[0].source.token == "resolved-token"
        del os.environ["MY_VAR"]

        print("  ✓ config: token resolution")
    finally:
        os.unlink(config_path)


def test_config_enabled():
    from gitacross.config import Config

    yaml_text = """
projects:
  - name: active-app
    source:
      type: local
      path: /tmp/a
    target:
      type: local
      path: /tmp/b
  - name: disabled-app
    enabled: false
    source:
      type: local
      path: /tmp/c
    target:
      type: local
      path: /tmp/d
  - name: explicitly-enabled-app
    enabled: true
    source:
      type: local
      path: /tmp/e
    target:
      type: local
      path: /tmp/f
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(yaml_text)
        config_path = f.name

    try:
        cfg = Config(config_path)
        assert len(cfg.projects) == 3
        assert cfg.projects[0].enabled is True
        assert cfg.projects[1].enabled is False
        assert cfg.projects[2].enabled is True
        print("  ✓ config: enabled field")
    finally:
        os.unlink(config_path)


def test_config_warnings_for_misplaced_keys(caplog):
    import logging
    from gitacross.config import Config

    yaml_text = """
projects:
  - name: misplaced-keys-app
    mode: commit
    sync_from: v1.0.0
    unknown_key: foo
    source:
      type: local
      path: /tmp/a
    target:
      type: local
      path: /tmp/b
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(yaml_text)
        config_path = f.name

    try:
        with caplog.at_level(logging.WARNING):
            cfg = Config(config_path)
            assert len(cfg.projects) == 1
            log_text = caplog.text
            assert "Project 'misplaced-keys-app': 'mode' was specified at the project level" in log_text
            assert "Project 'misplaced-keys-app': 'sync_from' was specified at the project level" in log_text
            assert "Project 'misplaced-keys-app': unrecognized configuration key 'unknown_key'" in log_text
            print("  ✓ config: warning on misplaced keys")
    finally:
        os.unlink(config_path)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def test_state():
    from gitacross.state import State

    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.yml"
        state = State(str(state_path))

        assert not state.has_release("myapp", "v1.0")
        state.add_release(
            "myapp",
            "v1.0",
            {
                "tag": "v1.0",
                "target_commit": "abc123",
                "published_at": "2026-07-07T10:00:00Z",
            },
        )
        state.save()

        state2 = State(str(state_path))
        assert state2.has_release("myapp", "v1.0")
        assert not state2.has_release("myapp", "v2.0")

        print("  ✓ state persistence")


def test_state_migrates_old_id_keys():
    """Old state files keyed by API release id are re-keyed by tag name on load."""
    from gitacross.state import State

    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.yml"
        state_path.write_text(
            """
projects:
  myapp:
    releases:
      "12345":
        tag: v1.0
        target_commit: abc123
        published_at: "2026-07-07T10:00:00Z"
      "67890":
        tag: v2.0
        target_commit: def456
        published_at: "2026-07-08T10:00:00Z"
"""
        )

        state = State(str(state_path))
        assert state.has_release("myapp", "v1.0")
        assert state.has_release("myapp", "v2.0")
        assert not state.has_release("myapp", "12345")
        assert not state.has_release("myapp", "67890")

    with tempfile.TemporaryDirectory() as tmp:
        # A stale id-keyed duplicate must not overwrite the tag-keyed entry
        state_path = Path(tmp) / "state.yml"
        state_path.write_text(
            """
projects:
  myapp:
    releases:
      "12345":
        tag: v1.0
        target_commit: stale
        published_at: "2026-07-07T10:00:00Z"
      v1.0:
        tag: v1.0
        target_commit: abc123
        published_at: "2026-07-07T10:00:00Z"
"""
        )

        state = State(str(state_path))
        releases = state._data["projects"]["myapp"]["releases"]
        assert "v1.0" in releases
        assert "12345" not in releases
        assert releases["v1.0"]["target_commit"] == "abc123"
        assert releases["v1.0"]["source_date"] == "2026-07-07T10:00:00Z"
        assert "published_at" not in releases["v1.0"]

        print("  ✓ state: migrates id keys to tag keys and published_at to source_date")


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def _make_project_raw(ops=None):
    """Minimal config dict for renderer tests."""
    raw = {
        "name": "test",
        "source": {
            "type": "gitea",
            "repo": "a/b",
            "api": "https://x.com",
            "token": "x",
        },
        "target": {
            "type": "github",
            "repo": "a/b",
            "api": "https://x.com",
            "token": "x",
            "branch": "main",
        },
        "renderer": {
            "operations": ops or [],
        },
    }
    return raw


def test_renderer_ignore():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = []
    raw = _make_project_raw(ops)
    raw["renderer"]["ignore"] = ["node_modules/**", ".env"]
    project = ProjectConfig(raw)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_file(root, "README.md")
        _make_file(root, ".env", "SECRET=1")
        _make_file(root, "node_modules/foo/index.js")
        _make_file(root, "src/main.py")

        apply_operations(root, project)

        assert (root / "README.md").exists()
        assert (root / "src/main.py").exists()
        assert not (root / ".env").exists()
        assert not (root / "node_modules").exists()

    print("  \u2713 renderer ignore")


def test_renderer_remove():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = [{"remove": [{"path": "docs"}, {"path": "*.secret", "pattern": "glob"}]}]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_file(root, "README.md")
        _make_file(root, "docs/guide.md")
        _make_file(root, "docs/api/ref.md")
        _make_file(root, "config.secret")
        _make_file(root, ".env.secret")

        apply_operations(root, project)

        assert (root / "README.md").exists()
        assert not (root / "docs").exists()
        assert not (root / "config.secret").exists()
        assert not (root / ".env.secret").exists()

    print("  ✓ renderer remove")


def test_renderer_rename():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = [{"rename": [{"from": ".gitea", "to": ".github"}]}]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_file(root, "README.md")
        _make_file(root, ".gitea/workflows/build.yml")

        apply_operations(root, project)

        assert (root / "README.md").exists()
        assert not (root / ".gitea").exists()
        assert (root / ".github/workflows/build.yml").exists()

    print("  ✓ renderer rename")


def test_renderer_replace():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = [
        {
            "replace": [
                {
                    "search": r"https://gitea\.example\.com",
                    "replace": "https://github.com",
                    "pattern": "regex",
                    "glob": "*.md",
                }
            ]
        }
    ]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_file(root, "README.md", "Visit https://gitea.example.com for code.")
        _make_file(root, "setup.py", "url = 'https://gitea.example.com'")

        apply_operations(root, project)

        assert "https://github.com" in (root / "README.md").read_text()
        assert "gitea.example.com" in (root / "setup.py").read_text()

    print("  ✓ renderer replace")


def test_renderer_replace_literal():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = [
        {
            "replace": [
                {"search": "old-text", "replace": "new-text", "pattern": "literal"}
            ]
        }
    ]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_file(root, "file.txt", "prefix old-text suffix")

        apply_operations(root, project)

        assert "new-text" in (root / "file.txt").read_text()

    print("  ✓ renderer replace literal")


def test_renderer_validate_ok():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = [
        {
            "validate": [
                {"assert": "file_exists", "path": "README.md"},
                {"assert": "file_absent", "path": "SECRETS.md"},
            ]
        }
    ]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_file(root, "README.md")
        apply_operations(root, project)

    print("  ✓ renderer validate (pass)")


def test_renderer_add():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = [
        {
            "add": [
                {"path": "NEW_FILE.md", "content": "# New\n\nAdded content."},
                {"path": "nested/deep/file.txt", "content": "deep"},
            ]
        }
    ]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_file(root, "README.md")
        apply_operations(root, project)

        assert (root / "NEW_FILE.md").exists()
        assert (root / "NEW_FILE.md").read_text() == "# New\n\nAdded content."
        assert (root / "nested/deep/file.txt").exists()
        assert (root / "nested/deep/file.txt").read_text() == "deep"
        assert (root / "README.md").exists()  # untouched

    print("  ✓ renderer add")


def test_renderer_validate_fail():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = [{"validate": [{"assert": "file_exists", "path": "MISSING.md"}]}]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_file(root, "README.md")

        try:
            apply_operations(root, project)
            assert False, "Should have raised"
        except RuntimeError as e:
            assert "MISSING.md" in str(e)

    print("  ✓ renderer validate (fail)")


# ---------------------------------------------------------------------------
# Git (local repo operations)
# ---------------------------------------------------------------------------


def test_git_repo_local_tags():
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _make_git_repo(repo)
        _make_file(repo, "README.md")
        _git_commit(repo, "first")
        _git_tag(repo, "v1.0")
        _make_file(repo, "CHANGELOG.md")
        _git_commit(repo, "second")
        _git_tag(repo, "v2.0")

        git = GitRepo.local(repo)
        tags = git.list_tags()
        assert "v1.0" in tags
        assert "v2.0" in tags

        assert git.tag_exists("v1.0") is True
        assert git.tag_exists("v999") is False

        print("  ✓ git: local tags")


def test_git_repo_export_tag():
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _make_git_repo(repo)
        _make_file(repo, "hello.txt", "world")
        _git_commit(repo, "first")
        _git_tag(repo, "v1.0")

        git = GitRepo.local(repo)
        dest = Path(tmp) / "export"
        dest.mkdir()
        git.export_tag("v1.0", dest)

        assert (dest / "hello.txt").exists()
        assert (dest / "hello.txt").read_text() == "world"

        print("  ✓ git: export tag")


def test_git_repo_commit_and_tag():
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _make_git_repo(repo)

        git = GitRepo.local(repo)
        git.ensure_branch("main")

        # Commit from a work dir
        work = Path(tmp) / "work"
        work.mkdir()
        _make_file(work, "release.txt", "v1.0 content")

        git.commit(work, "Release v1.0")
        git.tag("v1.0", "Release v1.0")

        assert git.tag_exists("v1.0") is True
        sha = git.head_sha()
        assert len(sha) == 40

        # Second commit (linear)
        work2 = Path(tmp) / "work2"
        work2.mkdir()
        _make_file(work2, "release.txt", "v2.0 content")
        git.commit(work2, "Release v2.0")
        git.tag("v2.0", "Release v2.0")

        assert git.tag_exists("v2.0") is True
        sha2 = git.head_sha()
        assert sha2 != sha  # different commit

        print("  ✓ git: commit and tag")


# ---------------------------------------------------------------------------
# Source (local source fetch)
# ---------------------------------------------------------------------------


def test_source_local():
    from gitacross.config import _EndpointConfig
    from gitacross.source import create_source

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _make_git_repo(repo)
        _make_file(repo, "f.txt")
        _git_commit(repo, "c1")
        _git_tag(repo, "v1.0")
        _make_file(repo, "g.txt")
        _git_commit(repo, "c2")
        _git_tag(repo, "v2.0")
        _git_tag(repo, "beta1")

        cfg = _EndpointConfig(
            {
                "type": "local",
                "path": str(repo),
                "tag_pattern": "v*",
            },
            is_source=True,
        )

        src = create_source(cfg, tmp)
        releases = src.fetch_releases()
        assert len(releases) == 2
        tags = [r["tag_name"] for r in releases]
        assert "v1.0" in tags
        assert "v2.0" in tags
        assert "beta1" not in tags

        print("  ✓ source: local")


def test_source_local_sync_from():
    """sync_from filters out releases older than the given tag."""
    from gitacross.config import _EndpointConfig
    from gitacross.source import create_source

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _make_git_repo(repo)
        _make_file(repo, "a.txt")
        _git_commit(repo, "c1")
        _git_tag(repo, "v1.0")
        _make_file(repo, "b.txt")
        _git_commit(repo, "c2")
        _git_tag(repo, "v2.0")
        _make_file(repo, "c.txt")
        _git_commit(repo, "c3")
        _git_tag(repo, "v3.0")

        # sync_from = v2.0 — should only get v2.0 and v3.0
        cfg = _EndpointConfig(
            {
                "type": "local",
                "path": str(repo),
                "tag_pattern": "v*",
                "sync_from": "v2.0",
            },
            is_source=True,
        )

        src = create_source(cfg, tmp)
        releases = src.fetch_releases()
        assert len(releases) == 2
        tags = [r["tag_name"] for r in releases]
        assert tags == ["v2.0", "v3.0"]

        # sync_from = nonexistent tag — nothing synced (safe default)
        cfg2 = _EndpointConfig(
            {
                "type": "local",
                "path": str(repo),
                "tag_pattern": "v*",
                "sync_from": "v999",
            },
            is_source=True,
        )
        src2 = create_source(cfg2, tmp)
        releases2 = src2.fetch_releases()
        assert releases2 == []

        print("  ✓ source: local sync_from")


def test_source_remote_mode_tag_sync_from():
    """mode: tag reads releases straight from git tags, no API involved."""
    from unittest import mock

    from gitacross.config import _EndpointConfig
    from gitacross.source import _RemoteSource

    cfg = _EndpointConfig(
        {
            "type": "gitea",
            "repo": "u/test",
            "api": "https://git.example.com/api/v1",
            "token": "tok",
            "sync_from": "v0.9.0",
            "mode": "tag",
        },
        is_source=True,
    )

    api = mock.MagicMock()
    mirror = mock.MagicMock()
    mirror.list_tags.return_value = ["v0.1.0", "v0.9.0"]
    mirror.list_tags_sorted_by_date.return_value = ["v0.1.0", "v0.9.0"]
    mirror.tag_commit_date.return_value = "2026-02-01T00:00:00Z"

    with mock.patch("gitacross.source.get_api_client", return_value=api), mock.patch(
        "gitacross.source.GitRepo.ensure_mirror", return_value=mirror
    ):
        src = _RemoteSource(cfg, "cache")
        releases = src.fetch_releases()

    assert [r["tag_name"] for r in releases] == ["v0.9.0"]
    api.list_releases.assert_not_called()
    print("  ✓ source: remote mode: tag reads git tags")


def test_source_remote_sync_from_tag_only_requires_mode_tag():
    """Release mode never treats a git-only tag as a release — warns and syncs nothing."""
    from unittest import mock

    from gitacross.config import _EndpointConfig
    from gitacross.source import _RemoteSource

    cfg = _EndpointConfig(
        {
            "type": "gitea",
            "repo": "u/test",
            "api": "https://git.example.com/api/v1",
            "token": "tok",
            "sync_from": "v0.9.0",
        },
        is_source=True,
    )

    api = mock.MagicMock()
    # API exposes only v0.2.4; v0.9.0 was never turned into a release
    api.list_releases.side_effect = [
        [
            {
                "tag_name": "v0.2.4",
                "prerelease": False,
                "draft": False,
                "published_at": "2026-01-01T00:00:00Z",
            }
        ],
        [],
    ]

    mirror = mock.MagicMock()
    mirror.tag_exists.return_value = True

    with mock.patch("gitacross.source.get_api_client", return_value=api), mock.patch(
        "gitacross.source.GitRepo.ensure_mirror", return_value=mirror
    ):
        src = _RemoteSource(cfg, "cache")
        releases = src.fetch_releases()

    assert releases == []
    print("  ✓ source: remote git-only sync_from needs mode: tag")


def test_source_remote_sync_from_filtered_prerelease():
    """A prerelease sync_from tag is excluded; warning points at include_prereleases."""
    from unittest import mock

    from gitacross.config import _EndpointConfig
    from gitacross.source import _RemoteSource

    cfg = _EndpointConfig(
        {
            "type": "gitea",
            "repo": "u/test",
            "api": "https://git.example.com/api/v1",
            "token": "tok",
            "sync_from": "v0.9.0",
        },
        is_source=True,
    )

    api = mock.MagicMock()
    api.list_releases.side_effect = [
        [
            {
                "tag_name": "v0.2.4",
                "prerelease": False,
                "draft": False,
                "published_at": "2026-01-01T00:00:00Z",
            },
            {
                "tag_name": "v0.9.0",
                "prerelease": True,
                "draft": False,
                "published_at": "2026-02-01T00:00:00Z",
            },
        ],
        [],
    ]

    mirror = mock.MagicMock()
    mirror.tag_exists.return_value = True  # even so, no git-tag fallback in release mode

    with mock.patch("gitacross.source.get_api_client", return_value=api), mock.patch(
        "gitacross.source.GitRepo.ensure_mirror", return_value=mirror
    ):
        src = _RemoteSource(cfg, "cache")
        releases = src.fetch_releases()

    assert releases == []
    print("  ✓ source: remote prerelease sync_from is filtered out")


def test_source_remote_sync_from_missing_everywhere():
    """sync_from pointing at a tag that exists nowhere still syncs nothing."""
    from unittest import mock

    from gitacross.config import _EndpointConfig
    from gitacross.source import _RemoteSource

    cfg = _EndpointConfig(
        {
            "type": "gitea",
            "repo": "u/test",
            "api": "https://git.example.com/api/v1",
            "token": "tok",
            "sync_from": "v9.9.9",
        },
        is_source=True,
    )

    api = mock.MagicMock()
    api.list_releases.side_effect = [
        [
            {
                "tag_name": "v0.2.4",
                "prerelease": False,
                "draft": False,
                "published_at": "2026-01-01T00:00:00Z",
            }
        ],
        [],
    ]

    mirror = mock.MagicMock()
    mirror.tag_exists.return_value = False

    with mock.patch("gitacross.source.get_api_client", return_value=api), mock.patch(
        "gitacross.source.GitRepo.ensure_mirror", return_value=mirror
    ):
        src = _RemoteSource(cfg, "cache")
        releases = src.fetch_releases()

    assert releases == []
    print("  ✓ source: remote sync_from missing everywhere syncs nothing")


def test_source_remote_invalid_mode():
    """Unknown source modes are rejected up front."""
    from gitacross.config import _EndpointConfig
    from gitacross.source import create_source

    cfg = _EndpointConfig(
        {
            "type": "gitea",
            "repo": "u/test",
            "api": "https://git.example.com/api/v1",
            "token": "tok",
            "mode": "banana",
        },
        is_source=True,
    )

    try:
        create_source(cfg, "cache")
        assert False, "should raise"
    except ValueError as e:
        assert "mode" in str(e)
    print("  ✓ source: remote invalid mode rejected")


def test_source_remote_warns_when_no_releases_or_tags(caplog):
    """When a remote source in release mode has no API releases and no tags, log a warning."""
    import logging
    from unittest import mock
    from gitacross.config import _EndpointConfig
    from gitacross.source import _RemoteSource

    cfg = _EndpointConfig(
        {
            "type": "gitea",
            "repo": "u/test",
            "api": "https://git.example.com/api/v1",
            "token": "tok",
            "mode": "release",
        },
        is_source=True,
    )

    api = mock.MagicMock()
    api.list_releases.return_value = []
    mirror = mock.MagicMock()
    mirror.list_tags_sorted_by_date.return_value = []

    with mock.patch("gitacross.source.get_api_client", return_value=api), mock.patch(
        "gitacross.source.GitRepo.ensure_mirror", return_value=mirror
    ):
        with caplog.at_level(logging.WARNING):
            src = _RemoteSource(cfg, "cache")
            releases = src.fetch_releases()

    assert releases == []
    assert "No API releases or git tags found for gitea repo 'u/test'" in caplog.text
    assert "mode: commit" in caplog.text
    print("  ✓ source: remote warns when no releases or tags found")


def test_source_remote_mode_tag_warns_when_no_tags(caplog):
    """When a remote source in tag mode has no tags, log a warning."""
    import logging
    from unittest import mock
    from gitacross.config import _EndpointConfig
    from gitacross.source import _RemoteSource

    cfg = _EndpointConfig(
        {
            "type": "gitea",
            "repo": "u/test",
            "api": "https://git.example.com/api/v1",
            "token": "tok",
            "mode": "tag",
        },
        is_source=True,
    )

    api = mock.MagicMock()
    mirror = mock.MagicMock()
    mirror.list_tags_sorted_by_date.return_value = []

    with mock.patch("gitacross.source.get_api_client", return_value=api), mock.patch(
        "gitacross.source.GitRepo.ensure_mirror", return_value=mirror
    ):
        with caplog.at_level(logging.WARNING):
            src = _RemoteSource(cfg, "cache")
            releases = src.fetch_releases()

    assert releases == []
    assert "No git tags found for gitea repo 'u/test'" in caplog.text
    assert "mode: commit" in caplog.text
    print("  ✓ source: remote tag mode warns when no tags found")


def test_git_ensure_mirror_prunes_stale_refs():
    """A failed push leaves local-only refs; the next ensure_mirror prunes them
    so tag_exists/ensure_branch don't treat them as already-pushed."""
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        remote = tmp / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "-q", str(remote)],
            check=True,
            capture_output=True,
        )

        # Scratch repo with a commit + tag, then a mirror of it
        scratch = tmp / "scratch"
        _make_git_repo(scratch)
        _make_file(scratch, "f.txt")
        _git_commit(scratch, "c1")
        _git_tag(scratch, "v0.9.0")

        mirror = tmp / "mirror.git"
        subprocess.run(
            ["git", "clone", "--mirror", "-q", str(scratch), str(mirror)],
            check=True,
            capture_output=True,
        )
        repo = GitRepo(mirror, is_bare=True)
        assert "v0.9.0" in repo.list_tags()
        assert repo.head_sha()

        # ensure_mirror against the empty remote: the local-only refs must go
        GitRepo.ensure_mirror(str(remote), str(mirror))
        assert "v0.9.0" not in repo.list_tags()
        heads = subprocess.run(
            ["git", "--git-dir", str(mirror), "for-each-ref", "--format=%(refname)", "refs/heads"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert heads == ""

        print("  ✓ git: ensure_mirror prunes stale local refs")


# ---------------------------------------------------------------------------
# Target (local target commit)
# ---------------------------------------------------------------------------


def test_target_local():
    from gitacross.config import _EndpointConfig
    from gitacross.target import create_target

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _make_git_repo(repo)

        cfg = _EndpointConfig(
            {
                "type": "local",
                "path": str(repo),
                "branch": "main",
            },
            is_source=False,
        )

        tgt = create_target(cfg, tmp)
        tgt.setup("main")

        # Commit something
        work = Path(tmp) / "work"
        work.mkdir()
        _make_file(work, "artifact.txt", "hello")
        tgt.commit(work, "Release v1.0")
        tgt.tag("v1.0", "Release v1.0")

        assert tgt.tag_exists("v1.0") is True
        assert tgt.head_sha()

        # Push is a no-op for local — should not crash with or without tag
        tgt.push("main", "v1.0")
        tgt.push("main")
        tgt.create_release("v1.0", "v1.0", "body")
        print("  ✓ target: local")


def test_target_remote_push_commit_mode():
    from gitacross.config import _EndpointConfig
    from gitacross.target import create_target

    with tempfile.TemporaryDirectory() as tmp:
        cfg = _EndpointConfig(
            {
                "type": "github",
                "repo": "owner/repo",
                "api": "https://api.github.com",
                "token": "token",
                "branch": "main",
            },
            is_source=False,
        )
        mock_api = mock.MagicMock()
        mock_api.ensure_repo_exists.return_value = {"clone_url": "https://github.com/owner/repo.git"}
        mock_git = mock.MagicMock()

        with (
            mock.patch("gitacross.target.get_api_client", return_value=mock_api),
            mock.patch("gitacross.git.GitRepo.ensure_mirror", return_value=mock_git),
        ):
            tgt = create_target(cfg, tmp)

            # Push with tag (release mode — force tag with +)
            tgt.push("main", "v1.0.0")
            mock_git.push.assert_called_with("origin", "main", "+refs/tags/v1.0.0")

            # Push without tag (commit mode)
            tgt.push("main")
            mock_git.push.assert_called_with("origin", "main")

        print("  ✓ target: remote push supporting commit and release modes")



# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


def test_retry_success():
    from gitacross.retry import retry

    called = 0

    def fn():
        nonlocal called
        called += 1
        return 42

    result = retry(fn, max_attempts=3)
    assert result == 42
    assert called == 1
    print("  ✓ retry: success first try")


def test_retry_failure():
    from gitacross.retry import retry

    called = 0

    def fn():
        nonlocal called
        called += 1
        raise ValueError("boom")

    try:
        retry(fn, max_attempts=3, backoff_seconds=0.01)
        assert False
    except ValueError:
        assert called == 3

    print("  ✓ retry: exhausts attempts")


# ---------------------------------------------------------------------------
# Remote client access checks
# ---------------------------------------------------------------------------


class _FakeHTTPResponse:
    def __init__(self, body=b"{}"):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _http_error(code):
    return urllib.error.HTTPError(
        "https://api.example.com/repos/owner/repo",
        code,
        "error",
        email.message.Message(),
        io.BytesIO(b'{"message": "nope"}'),
    )


def test_github_verify_access_ok():
    from gitacross.providers.github import GitHubClient

    client = GitHubClient("https://api.github.com", "owner/repo", "token")
    with mock.patch(
        "gitacross.providers.base.urllib.request.urlopen", return_value=_FakeHTTPResponse()
    ):
        client.verify_access()  # should not raise
    print("  ✓ github: verify_access accepts a reachable repo")


def test_github_verify_access_missing_repo_creates_it():
    """When the repo 404s, ensure_repo_exists should create it and return metadata."""
    from gitacross.providers.github import GitHubClient

    client = GitHubClient("https://api.github.com", "owner/repo", "token")
    repo_meta = {"clone_url": "https://github.com/owner/repo.git", "html_url": "https://github.com/owner/repo"}
    user_meta = {"login": "owner"}

    call_count = 0

    def fake_urlopen(req):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call: GET /repos/owner/repo → 404
            raise _http_error(404)
        if call_count == 2:
            # Second call: GET /user → authenticated user
            return _FakeHTTPResponse(json.dumps(user_meta).encode())
        # Third call: POST /user/repos → created
        return _FakeHTTPResponse(json.dumps(repo_meta).encode())

    with mock.patch("gitacross.providers.base.urllib.request.urlopen", side_effect=fake_urlopen):
        result = client.ensure_repo_exists()
    assert result["clone_url"] == repo_meta["clone_url"]
    print("  ✓ github: ensure_repo_exists auto-creates a missing repo")


def test_github_verify_access_missing_repo_creates_it_org():
    """When the owner is an org (not the token user), use /orgs/{org}/repos."""
    from gitacross.providers.github import GitHubClient

    client = GitHubClient("https://api.github.com", "my-org/repo", "token")
    repo_meta = {"clone_url": "https://github.com/my-org/repo.git", "html_url": "https://github.com/my-org/repo"}
    user_meta = {"login": "not-the-org-owner"}
    created_urls = []

    call_count = 0

    def fake_urlopen(req):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _http_error(404)
        if call_count == 2:
            return _FakeHTTPResponse(json.dumps(user_meta).encode())
        created_urls.append(req.full_url)
        return _FakeHTTPResponse(json.dumps(repo_meta).encode())

    with mock.patch("gitacross.providers.base.urllib.request.urlopen", side_effect=fake_urlopen):
        result = client.ensure_repo_exists()
    assert result["clone_url"] == repo_meta["clone_url"]
    assert any("/orgs/my-org/repos" in u for u in created_urls), "should POST to org endpoint"
    print("  ✓ github: ensure_repo_exists auto-creates a missing org repo")


def test_github_create_repo_is_private():
    """Auto-created repos must be private=True in the POST payload."""
    from gitacross.providers.github import GitHubClient
    import json as _json

    client = GitHubClient("https://api.github.com", "owner/newrepo", "token")
    user_meta = {"login": "owner"}
    repo_meta = {"clone_url": "https://github.com/owner/newrepo.git", "html_url": "..."}
    posted_bodies = []

    call_count = 0

    def fake_urlopen(req):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _http_error(404)
        if call_count == 2:
            return _FakeHTTPResponse(_json.dumps(user_meta).encode())
        posted_bodies.append(_json.loads(req.data))
        return _FakeHTTPResponse(_json.dumps(repo_meta).encode())

    with mock.patch("gitacross.providers.base.urllib.request.urlopen", side_effect=fake_urlopen):
        client.ensure_repo_exists()
    assert posted_bodies, "should have POSTed a create-repo body"
    assert posted_bodies[0]["private"] is True, "repo should be created as private"
    print("  ✓ github: auto-created repo uses private=True")


def test_github_create_repo_404_then_422_private_collision():
    """If creation returns 422, raise a clear 'private repo collision' error."""
    from gitacross.providers.github import GitHubClient

    client = GitHubClient("https://api.github.com", "owner/repo", "token")
    user_meta = {"login": "owner"}

    call_count = 0

    def fake_urlopen(req):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _http_error(404)   # repo not visible (private + bad token)
        if call_count == 2:
            return _FakeHTTPResponse(json.dumps(user_meta).encode())
        raise _http_error(422)       # name already taken → repo exists privately

    with mock.patch("gitacross.providers.base.urllib.request.urlopen", side_effect=fake_urlopen):
        try:
            client.ensure_repo_exists()
            assert False, "should raise"
        except RuntimeError as e:
            msg = str(e)
            assert "already exist" in msg or "private" in msg
            assert "repo" in msg.lower()
    print("  ✓ github: 404 → 422 raises actionable private-repo-collision error")


def test_github_verify_access_401_raises_directly():
    """401 should raise immediately without ever attempting repo creation."""
    from gitacross.providers.github import GitHubClient

    client = GitHubClient("https://api.github.com", "owner/repo", "token")
    call_count = 0

    def fake_urlopen(req):
        nonlocal call_count
        call_count += 1
        raise _http_error(401)

    with mock.patch("gitacross.providers.base.urllib.request.urlopen", side_effect=fake_urlopen):
        try:
            client.ensure_repo_exists()
            assert False, "should raise"
        except RuntimeError as e:
            assert "invalid or expired" in str(e)
    assert call_count == 1, "should not retry or call create after 401"
    print("  ✓ github: 401 raises immediately without attempting creation")


def test_gitea_verify_access_bad_token():
    from gitacross.providers.gitea import GiteaClient

    client = GiteaClient("https://git.example.com/api/v1", "owner/repo", "token")
    with mock.patch(
        "gitacross.providers.base.urllib.request.urlopen", side_effect=_http_error(401)
    ):
        try:
            client.verify_access()
            assert False, "should raise"
        except RuntimeError as e:
            assert "invalid or expired" in str(e)
    print("  ✓ gitea: verify_access explains a bad token")


def test_gitea_create_repo_is_private():
    """Auto-created Gitea repos must be private=True in the POST payload."""
    from gitacross.providers.gitea import GiteaClient
    import json as _json

    client = GiteaClient("https://git.example.com/api/v1", "owner/newrepo", "token")
    user_meta = {"login": "owner"}
    repo_meta = {"clone_url": "https://git.example.com/owner/newrepo.git", "html_url": "..."}
    posted_bodies = []

    call_count = 0

    def fake_urlopen(req):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _http_error(404)
        if call_count == 2:
            return _FakeHTTPResponse(_json.dumps(user_meta).encode())
        posted_bodies.append(_json.loads(req.data))
        return _FakeHTTPResponse(_json.dumps(repo_meta).encode())

    with mock.patch("gitacross.providers.base.urllib.request.urlopen", side_effect=fake_urlopen):
        client.ensure_repo_exists()
    assert posted_bodies, "should have POSTed a create-repo body"
    assert posted_bodies[0]["private"] is True, "repo should be created as private"
    print("  ✓ gitea: auto-created repo uses private=True")


def test_gitea_create_repo_404_then_409_private_collision():
    """If Gitea creation returns 409 Conflict, raise a clear private repo collision error."""
    from gitacross.providers.gitea import GiteaClient

    client = GiteaClient("https://git.example.com/api/v1", "owner/repo", "token")
    user_meta = {"login": "owner"}

    call_count = 0

    def fake_urlopen(req):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _http_error(404)
        if call_count == 2:
            return _FakeHTTPResponse(json.dumps(user_meta).encode())
        raise _http_error(409)

    with mock.patch("gitacross.providers.base.urllib.request.urlopen", side_effect=fake_urlopen):
        try:
            client.ensure_repo_exists()
            assert False, "should raise"
        except RuntimeError as e:
            msg = str(e)
            assert "already exist" in msg or "private" in msg
            assert "token" in msg.lower()
    print("  ✓ gitea: 404 → 409 raises actionable private-repo-collision error")


def test_gitea_verify_access_missing_repo_creates_it():
    """When the Gitea repo 404s, ensure_repo_exists should create it."""
    from gitacross.providers.gitea import GiteaClient

    client = GiteaClient("https://git.example.com/api/v1", "owner/repo", "token")
    repo_meta = {"clone_url": "https://git.example.com/owner/repo.git", "html_url": "https://git.example.com/owner/repo"}
    user_meta = {"login": "owner"}

    call_count = 0

    def fake_urlopen(req):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _http_error(404)
        if call_count == 2:
            return _FakeHTTPResponse(json.dumps(user_meta).encode())
        return _FakeHTTPResponse(json.dumps(repo_meta).encode())

    with mock.patch("gitacross.providers.base.urllib.request.urlopen", side_effect=fake_urlopen):
        result = client.ensure_repo_exists()
    assert result["clone_url"] == repo_meta["clone_url"]
    print("  ✓ gitea: ensure_repo_exists auto-creates a missing repo")




def test_git_redact_urls():
    from gitacross.git import _redact

    url = "https://uqkami:secret123@git.nodebay.top/uqkami/Repo.git"
    out = _redact(url)
    assert "secret123" not in out
    assert out == "https://uqkami:***@git.nodebay.top/uqkami/Repo.git"
    # No credentials — untouched
    plain = "https://github.com/uqkami/Repo.git"
    assert _redact(plain) == plain
    # Auth-failure stderr from git echoes the URL with credentials
    fatal = "fatal: Authentication failed for 'https://uqkami:secret123@github.com/uqkami/Repo.git/'"
    assert "secret123" not in _redact(fatal)
    print("  ✓ git: redacts credentials in URLs and git stderr")


# ---------------------------------------------------------------------------
# Integration: end-to-end local → local sync
# ---------------------------------------------------------------------------


def test_e2e_local_to_local():
    from gitacross.config import Config
    from gitacross.main import sync_project
    from gitacross.state import State

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # ── Source repo: 2 tags, one ignored file ──
        src = tmp / "source"
        _make_git_repo(src)
        _make_file(src, "README.md", "# Project")
        _make_file(src, ".env", "SECRET=1")
        _make_file(src, "src/main.py", "print(1)")
        _git_commit(src, "first")
        _git_tag(src, "v1.0")

        _make_file(src, "src/main.py", "print(2)")
        _make_file(src, "CHANGELOG.md", "## v2")
        _git_commit(src, "second")
        _git_tag(src, "v2.0")

        # ── Empty target repo ──
        tgt = tmp / "target"
        _make_git_repo(tgt)

        # ── Config ──
        cfg = tmp / "config.yml"
        cfg.write_text(f"""
- name: e2e
  source:
    type: local
    path: {src}
    tag_pattern: "v*"
  target:
    type: local
    path: {tgt}
    branch: main
  renderer:
    ignore:
      - .env
    operations:
      - add:
          - path: .github/FUNDING.yml
            content: "github: myuser"
      - validate:
          - assert: file_exists
            path: README.md
  retry:
    max_attempts: 2
    backoff_seconds: 1
""")

        # Must chdir so the cache dir and state land inside tmp
        old_cwd = Path.cwd()
        os.chdir(tmp)
        try:
            config = Config(str(cfg))
            state = State("state.yml")

            # ── First run ──
            sync_project(config.projects[0], state, dry_run=False)

            # Target has expected files (latest release = v2.0)
            assert (tgt / "README.md").read_text() == "# Project"
            assert (tgt / "src/main.py").read_text() == "print(2)"
            assert (tgt / "CHANGELOG.md").read_text() == "## v2"
            assert (tgt / ".github/FUNDING.yml").read_text() == "github: myuser"
            assert not (tgt / ".env").exists()  # removed by ignore

            # Tags created
            r = subprocess.run(
                ["git", "-C", str(tgt), "tag", "-l"],
                capture_output=True,
                text=True,
            )
            tags = r.stdout.strip().split()
            assert "v1.0" in tags
            assert "v2.0" in tags

            # Linear history: v1.0 is parent of v2.0
            r = subprocess.run(
                ["git", "-C", str(tgt), "merge-base", "--is-ancestor", "v1.0", "v2.0"],
            )
            assert r.returncode == 0, "v1.0 should be ancestor of v2.0"

            # State recorded with source_commit and target_commit (no redundant commit_sha)
            assert state.has_release("e2e", "v1.0")
            assert state.has_release("e2e", "v2.0")
            rel1 = state._data["projects"]["e2e"]["releases"]["v1.0"]
            assert "source_commit" in rel1 and len(rel1["source_commit"]) == 40
            assert "target_commit" in rel1 and len(rel1["target_commit"]) == 40
            assert "source_date" in rel1 and len(rel1["source_date"]) > 0
            assert "sync_date" in rel1 and len(rel1["sync_date"]) > 0
            assert "commit_sha" not in rel1
            assert "published_at" not in rel1

            # ── Second run: idempotent, no new commits ──
            log_before = subprocess.run(
                ["git", "-C", str(tgt), "rev-list", "--count", "HEAD"],
                capture_output=True,
                text=True,
            ).stdout.strip()

            sync_project(config.projects[0], state, dry_run=False)

            log_after = subprocess.run(
                ["git", "-C", str(tgt), "rev-list", "--count", "HEAD"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            assert log_before == log_after, "second run should not create new commits"

            print("  ✓ e2e: local → local (sync, state, idempotency)")
        finally:
            os.chdir(old_cwd)


def test_clean_text():
    from gitacross.main import _clean_text

    raw = r"Release 1.0.0\n\n6f009f7 Update FFmpeg static binary extraction path in Dockerfile"
    cleaned = _clean_text(raw)
    assert cleaned == "Release 1.0.0\n\n6f009f7 Update FFmpeg static binary extraction path in Dockerfile"
    assert "\n" in cleaned
    assert r"\n" not in cleaned

    crlf_raw = r"Line 1\r\nLine 2"
    assert _clean_text(crlf_raw) == "Line 1\nLine 2"
    assert _clean_text("Normal multiline\ntext") == "Normal multiline\ntext"
    assert _clean_text("") == ""
    assert _clean_text(None) == ""
    print("  ✓ main: _clean_text unescapes literal newlines")


def test_git_repo_resolve_and_export_commit():
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _make_git_repo(repo)
        _make_file(repo, "version.txt", "v1.0")
        _git_commit(repo, "c1")
        _git_tag(repo, "v1.0")

        _make_file(repo, "version.txt", "v1.1-hotfix")
        _git_commit(repo, "c2-hotfix")

        git = GitRepo.local(repo)
        sha_v1 = git.resolve_commit("v1.0")
        sha_head = git.resolve_commit("HEAD")
        assert len(sha_v1) == 40
        assert len(sha_head) == 40
        assert sha_v1 != sha_head

        # Export by commit SHA
        dest = Path(tmp) / "export_commit"
        dest.mkdir()
        git.export_commit(sha_head, dest)
        assert (dest / "version.txt").read_text() == "v1.1-hotfix"

        # Export by tag
        dest_tag = Path(tmp) / "export_tag"
        dest_tag.mkdir()
        git.export_tag("v1.0", dest_tag)
        assert (dest_tag / "version.txt").read_text() == "v1.0"

        print("  ✓ git: resolve_commit and export_commit")


def test_git_repo_resolve_default_branch_head_mirror():
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        origin = Path(tmp) / "origin"
        _make_git_repo(origin)
        _make_file(origin, "file.txt", "hello")
        _git_commit(origin, "init")

        mirror_path = Path(tmp) / "mirror.git"
        git = GitRepo.ensure_mirror(str(origin), mirror_path)

        # Auto-detect default branch on bare mirror
        head_sha = git.resolve_default_branch_head()
        assert len(head_sha) == 40

        # Specific branch resolution on bare mirror
        main_sha = git.resolve_default_branch_head("main") or git.resolve_default_branch_head("master")
        assert main_sha == head_sha
        print("  ✓ git: resolve_default_branch_head on bare mirror")



def test_source_resolves_commit_sha_and_export_release():
    from unittest import mock
    from gitacross.config import _EndpointConfig
    from gitacross.source import _RemoteSource

    cfg = _EndpointConfig(
        {
            "type": "gitea",
            "repo": "u/test",
            "api": "https://git.example.com/api/v1",
            "token": "tok",
        },
        is_source=True,
    )

    api = mock.MagicMock()
    api.list_releases.side_effect = [
        [
            {
                "tag_name": "v1.0.0",
                "target_commitish": "",
                "name": "v1.0.0",
                "body": "Release 1.0.0",
                "prerelease": False,
                "draft": False,
                "published_at": "2026-08-17T19:47:20-04:00",
            },
        ],
        [],
    ]

    mirror = mock.MagicMock()
    mirror.resolve_commit.side_effect = lambda ref: (
        "0f23af18b698cc16866352448da9579082fd9a10" if ref == "v1.0.0" else ""
    )

    with mock.patch("gitacross.source.get_api_client", return_value=api), mock.patch(
        "gitacross.source.GitRepo.ensure_mirror", return_value=mirror
    ):
        src = _RemoteSource(cfg, "cache")
        releases = src.fetch_releases()

        assert len(releases) == 1
        rel = releases[0]
        # Resolved via git tag
        assert rel["commit_sha"] == "0f23af18b698cc16866352448da9579082fd9a10"

        # Export release should use the commit SHA
        dest = Path("/tmp/dummy")
        src.export_release(rel, dest)
        mirror.export_commit.assert_called_with(
            "0f23af18b698cc16866352448da9579082fd9a10", dest
        )

    print("  ✓ source: resolves release commit SHA via git tag and exports by commit")


def test_config_sync_assets():
    import json
    from gitacross.config import Config

    yaml_text = """
projects:
  - name: p1
    sync_assets: true
    source:
      type: gitea
      repo: owner/r1
    target:
      type: github
      repo: owner/r1
  - name: p2
    source:
      type: gitea
      repo: owner/r2
      preserve_assets:
        - "*.tar.gz"
        - "*.zip"
    target:
      type: github
      repo: owner/r2
  - name: p3
    source:
      type: gitea
      repo: owner/r3
    target:
      type: github
      repo: owner/r3
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(yaml_text)
        path = f.name

    try:
        cfg = Config(path)
        assert cfg.projects[0].sync_assets is True
        assert cfg.projects[1].sync_assets == ["*.tar.gz", "*.zip"]
        assert cfg.projects[2].sync_assets is False
    finally:
        os.unlink(path)

    print("  ✓ config: sync_assets / preserve_assets")


def test_matches_asset_filter():
    from gitacross.main import _matches_asset_filter

    assert _matches_asset_filter("app.tar.gz", True) is True
    assert _matches_asset_filter("app.tar.gz", False) is False
    assert _matches_asset_filter("app.tar.gz", None) is False
    assert _matches_asset_filter("app-linux-amd64.tar.gz", "*.tar.gz") is True
    assert _matches_asset_filter("app-win-x64.zip", "*.tar.gz") is False
    assert (
        _matches_asset_filter("app-win-x64.zip", ["*.tar.gz", "*.zip"]) is True
    )
    assert _matches_asset_filter("app.deb", ["*.tar.gz", "*.zip"]) is False

    print("  ✓ main: _matches_asset_filter pattern matching")


def test_github_client_assets():
    import json
    from gitacross.providers.github import GitHubClient

    gh = GitHubClient("https://api.github.com", "owner/repo", "token123")

    # 1. get_release_by_tag
    resp_data = json.dumps({"id": 42, "tag_name": "v1.0.0"}).encode()
    resp_mock = mock.MagicMock()
    resp_mock.read.return_value = resp_data
    resp_mock.__enter__.return_value = resp_mock

    with mock.patch("urllib.request.urlopen", return_value=resp_mock) as m_open:
        rel = gh.get_release_by_tag("v1.0.0")
        assert rel["id"] == 42
        req = m_open.call_args[0][0]
        assert req.full_url == "https://api.github.com/repos/owner/repo/releases/tags/v1.0.0"

    # get_release_by_tag 404 returns None
    err = urllib.error.HTTPError(
        url="url", code=404, msg="Not Found", hdrs={}, fp=io.BytesIO(b'{"message": "Not Found"}')
    )
    with mock.patch("urllib.request.urlopen", side_effect=err):
        assert gh.get_release_by_tag("vmissing") is None

    # 2. list_release_assets
    resp_assets = json.dumps([{"id": 101, "name": "app.zip"}]).encode()
    resp_mock2 = mock.MagicMock()
    resp_mock2.read.return_value = resp_assets
    resp_mock2.__enter__.return_value = resp_mock2

    with mock.patch("urllib.request.urlopen", return_value=resp_mock2):
        assets = gh.list_release_assets(42)
        assert len(assets) == 1
        assert assets[0]["name"] == "app.zip"

    # 3. download_asset
    resp_stream = io.BytesIO(b"binary payload content")
    resp_stream_mock = mock.MagicMock()
    resp_stream_mock.read = resp_stream.read
    resp_stream_mock.__enter__.return_value = resp_stream_mock

    with tempfile.NamedTemporaryFile(delete=False) as tmp_dest:
        dest_path = Path(tmp_dest.name)

    try:
        with mock.patch("urllib.request.urlopen", return_value=resp_stream_mock) as m_open:
            gh.download_asset({"url": "https://api.github.com/repos/owner/repo/releases/assets/101"}, dest_path)
            assert dest_path.read_bytes() == b"binary payload content"
            req = m_open.call_args[0][0]
            assert req.headers["Accept"] == "application/octet-stream"
    finally:
        dest_path.unlink(missing_ok=True)

    # 4. upload_asset success
    upload_resp = json.dumps({"id": 102, "name": "app.zip"}).encode()
    resp_up_mock = mock.MagicMock()
    resp_up_mock.read.return_value = upload_resp
    resp_up_mock.__enter__.return_value = resp_up_mock

    with tempfile.NamedTemporaryFile(delete=False) as tmp_src:
        tmp_src.write(b"app content bytes")
        tmp_src_path = Path(tmp_src.name)

    try:
        with mock.patch("urllib.request.urlopen", return_value=resp_up_mock) as m_open:
            rel_dict = {
                "id": 42,
                "upload_url": "https://uploads.github.com/repos/owner/repo/releases/42/assets{?name,label}",
            }
            res = gh.upload_asset(rel_dict, tmp_src_path, name="app.zip")
            assert res["id"] == 102
            req = m_open.call_args[0][0]
            assert req.full_url == "https://uploads.github.com/repos/owner/repo/releases/42/assets?name=app.zip"
            assert req.headers["Content-type"] == "application/octet-stream"
            assert req.data == b"app content bytes"

        # 5. upload_asset 422 idempotent
        err422 = urllib.error.HTTPError(
            url="url", code=422, msg="Unprocessable Entity", hdrs={}, fp=io.BytesIO(b'{"message": "already exists"}')
        )
        with mock.patch("urllib.request.urlopen", side_effect=err422):
            res = gh.upload_asset(rel_dict, tmp_src_path, name="app.zip")
            assert res is None
    finally:
        tmp_src_path.unlink(missing_ok=True)

    print("  ✓ github: asset operations (download, upload, idempotency)")


def test_gitea_client_assets():
    import json
    from gitacross.providers.gitea import GiteaClient

    gt = GiteaClient("https://gitea.example.com/api/v1", "owner/repo", "token456")

    # 1. get_release_by_tag
    resp_data = json.dumps({"id": 7, "tag_name": "v1.0.0"}).encode()
    resp_mock = mock.MagicMock()
    resp_mock.read.return_value = resp_data
    resp_mock.__enter__.return_value = resp_mock

    with mock.patch("urllib.request.urlopen", return_value=resp_mock) as m_open:
        rel = gt.get_release_by_tag("v1.0.0")
        assert rel["id"] == 7
        req = m_open.call_args[0][0]
        assert req.full_url == "https://gitea.example.com/api/v1/repos/owner/repo/releases/tags/v1.0.0"

    # get_release_by_tag 404 returns None
    err = urllib.error.HTTPError(
        url="url", code=404, msg="Not Found", hdrs={}, fp=io.BytesIO(b'{"message": "Not Found"}')
    )
    with mock.patch("urllib.request.urlopen", side_effect=err):
        assert gt.get_release_by_tag("vmissing") is None

    # 2. download_asset
    resp_stream = io.BytesIO(b"gitea binary data")
    resp_stream_mock = mock.MagicMock()
    resp_stream_mock.read = resp_stream.read
    resp_stream_mock.__enter__.return_value = resp_stream_mock

    with tempfile.NamedTemporaryFile(delete=False) as tmp_dest:
        dest_path = Path(tmp_dest.name)

    try:
        with mock.patch("urllib.request.urlopen", return_value=resp_stream_mock) as m_open:
            gt.download_asset({"browser_download_url": "/attachments/uuid-123"}, dest_path)
            assert dest_path.read_bytes() == b"gitea binary data"
            req = m_open.call_args[0][0]
            assert req.full_url == "https://gitea.example.com/attachments/uuid-123"
    finally:
        dest_path.unlink(missing_ok=True)

    # 3. upload_asset multipart success
    upload_resp = json.dumps({"id": 88, "name": "binary.tar.gz"}).encode()
    resp_up_mock = mock.MagicMock()
    resp_up_mock.read.return_value = upload_resp
    resp_up_mock.__enter__.return_value = resp_up_mock

    with tempfile.NamedTemporaryFile(delete=False) as tmp_src:
        tmp_src.write(b"tar.gz content")
        tmp_src_path = Path(tmp_src.name)

    try:
        with mock.patch("urllib.request.urlopen", return_value=resp_up_mock) as m_open:
            res = gt.upload_asset(7, tmp_src_path, name="binary.tar.gz")
            assert res["id"] == 88
            req = m_open.call_args[0][0]
            assert "https://gitea.example.com/api/v1/repos/owner/repo/releases/7/assets?name=binary.tar.gz" in req.full_url
            assert "multipart/form-data" in req.headers["Content-type"]
            assert b"binary.tar.gz" in req.data
            assert b"tar.gz content" in req.data

        # 4. upload_asset 409 idempotent
        err409 = urllib.error.HTTPError(
            url="url", code=409, msg="Conflict", hdrs={}, fp=io.BytesIO(b'{"message": "attachment already exists"}')
        )
        with mock.patch("urllib.request.urlopen", side_effect=err409):
            res = gt.upload_asset(7, tmp_src_path, name="binary.tar.gz")
            assert res is None
    finally:
        tmp_src_path.unlink(missing_ok=True)

    print("  ✓ gitea: asset operations (download, multipart upload, idempotency)")


def test_sync_release_assets_orchestration():
    from gitacross.main import _sync_release_assets

    source = mock.MagicMock()
    target = mock.MagicMock()

    rel = {
        "tag_name": "v1.0.0",
        "assets": [
            {"name": "app-x86_64.tar.gz", "size": 1024, "browser_download_url": "http://example.com/1"},
            {"name": "checksums.txt", "size": 64, "browser_download_url": "http://example.com/2"},
            {"name": "app.dmg", "size": 2048, "browser_download_url": "http://example.com/3"},
        ],
    }

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)

        # 1. sync only *.tar.gz
        _sync_release_assets(
            source=source,
            target=target,
            rel=rel,
            tag="v1.0.0",
            target_release={"id": 99},
            sync_assets=["*.tar.gz"],
            tmpdir=tmpdir,
        )

        assert source.download_asset.call_count == 1
        assert source.download_asset.call_args[0][0]["name"] == "app-x86_64.tar.gz"
        assert target.upload_release_asset.call_count == 1
        assert target.upload_release_asset.call_args[1]["name"] == "app-x86_64.tar.gz"

        source.reset_mock()
        target.reset_mock()

        # 2. dry run
        _sync_release_assets(
            source=source,
            target=target,
            rel=rel,
            tag="v1.0.0",
            target_release={"id": 99},
            sync_assets=True,
            tmpdir=tmpdir,
            dry_run=True,
        )

        assert source.download_asset.call_count == 0
        assert target.upload_release_asset.call_count == 0

    print("  ✓ main: _sync_release_assets orchestration and filtering")


def test_e2e_remote_with_assets():
    from gitacross.config import Config
    from gitacross.main import sync_project
    from gitacross.state import State

    with tempfile.TemporaryDirectory() as tmp:
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            cfg_yaml = """
projects:
  - name: gitea-to-github
    sync_assets: true
    source:
      type: gitea
      repo: myorg/app
      api: https://gitea.example.com/api/v1
      token: gitea_secret
    target:
      type: github
      repo: myorg/app
      api: https://api.github.com
      token: github_secret
      branch: main
"""
            Path("config.yml").write_text(cfg_yaml)
            config = Config("config.yml")
            state = State("state.yml")

            # Mock source and target clients
            mock_gitea = mock.MagicMock()
            mock_gitea.list_releases.side_effect = [
                [
                    {
                        "tag_name": "v1.0.0",
                        "name": "v1.0.0",
                        "body": "First release",
                        "published_at": "2026-08-18T10:00:00Z",
                        "assets": [
                            {
                                "id": 1,
                                "name": "app-linux-amd64.tar.gz",
                                "size": 100,
                                "browser_download_url": "/att/1",
                            }
                        ],
                    }
                ],
                [],
            ]

            def _fake_download(asset, dest):
                Path(dest).write_bytes(b"binary-data-prebuilt")

            mock_gitea.download_asset.side_effect = _fake_download

            mock_github = mock.MagicMock()
            mock_github.create_release.return_value = {
                "id": 999,
                "upload_url": "https://uploads.github.com/repos/myorg/app/releases/999/assets{?name,label}",
            }

            mock_git = mock.MagicMock()
            mock_git.resolve_commit.return_value = (
                "1111111111111111111111111111111111111111"
            )
            mock_git.tag_exists.return_value = False
            mock_git.head_sha.return_value = (
                "2222222222222222222222222222222222222222"
            )

            with (
                mock.patch("gitacross.source.get_api_client", return_value=mock_gitea),
                mock.patch("gitacross.target.get_api_client", return_value=mock_github),
                mock.patch(
                    "gitacross.git.GitRepo.ensure_mirror",
                    return_value=mock_git,
                ),
            ):
                sync_project(config.projects[0], state, dry_run=False)

                # Verify release was created
                mock_github.create_release.assert_called_once_with(
                    "v1.0.0", "v1.0.0", "First release", False
                )

                # Verify asset was downloaded from source and uploaded to target
                mock_gitea.download_asset.assert_called_once()
                mock_github.upload_asset.assert_called_once()
                upload_args = mock_github.upload_asset.call_args
                assert upload_args[0][0]["id"] == 999
                assert upload_args[1]["name"] == "app-linux-amd64.tar.gz"

                # Verify state was saved
                assert state.has_release("gitea-to-github", "v1.0.0")

            print("  ✓ e2e: remote → remote with asset prebuilt sync")
        finally:
            os.chdir(old_cwd)


def test_config_preserve_description():
    from gitacross.config import Config

    yaml_text = """
projects:
  - name: default-preserves
    source:
      type: gitea
      repo: owner/r1
    target:
      type: github
      repo: owner/r1
  - name: project-disabled
    preserve_description: false
    source:
      type: gitea
      repo: owner/r2
    target:
      type: github
      repo: owner/r2
  - name: source-disabled
    source:
      type: gitea
      repo: owner/r3
      preserve_description: false
    target:
      type: github
      repo: owner/r3
  - name: target-disabled
    source:
      type: gitea
      repo: owner/r4
    target:
      type: github
      repo: owner/r4
      preserve_release_description: false
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(yaml_text)
        path = f.name

    try:
        cfg = Config(path)
        assert cfg.projects[0].preserve_description is True
        assert cfg.projects[1].preserve_description is False
        assert cfg.projects[2].preserve_description is False
        assert cfg.projects[3].preserve_description is False
    finally:
        os.unlink(path)

    print("  ✓ config: preserve_description options and defaults")


def test_sync_preserve_description_and_prerelease():
    from gitacross.config import Config
    from gitacross.main import sync_project
    from gitacross.state import State

    yaml_text = """
projects:
  - name: sync-test-preserve-off
    preserve_description: false
    source:
      type: gitea
      repo: src/repo
      include_prereleases: true
    target:
      type: github
      repo: tgt/repo
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(yaml_text)
        path = f.name

    with tempfile.TemporaryDirectory() as tmp:
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            cfg = Config(path)
            state = State("state.yml")

            mock_gitea = mock.MagicMock()
            mock_gitea.list_releases.side_effect = [
                [
                    {
                        "tag_name": "v1.0.0-beta.1",
                        "name": "v1.0.0 Beta 1",
                        "body": "Detailed release notes here",
                        "prerelease": True,
                        "published_at": "2026-08-18T10:00:00Z",
                    }
                ],
                [],
            ]

            mock_github = mock.MagicMock()
            mock_github.create_release.return_value = {"id": 100}

            mock_git = mock.MagicMock()
            mock_git.resolve_commit.return_value = "aaaa" * 10
            mock_git.tag_exists.return_value = False
            mock_git.head_sha.return_value = "bbbb" * 10

            with (
                mock.patch("gitacross.source.get_api_client", return_value=mock_gitea),
                mock.patch("gitacross.target.get_api_client", return_value=mock_github),
                mock.patch("gitacross.git.GitRepo.ensure_mirror", return_value=mock_git),
            ):
                sync_project(cfg.projects[0], state, dry_run=False)

                # Description should be empty because preserve_description is false.
                # Prerelease should be True because the source was a prerelease.
                mock_github.create_release.assert_called_once_with(
                    "v1.0.0-beta.1",
                    "v1.0.0 Beta 1",
                    "",
                    True,
                )
        finally:
            os.chdir(old_cwd)
            os.unlink(path)

    print("  ✓ e2e: preserve_description=false creates release with empty body and preserves prerelease flag")


def test_provider_registry():
    from gitacross.providers import get_api_client, register_provider
    from gitacross.providers.gitea import GiteaClient
    from gitacross.providers.github import GitHubClient

    gt = get_api_client("gitea", "https://gitea.example.com", "u/r", "tok")
    assert isinstance(gt, GiteaClient)

    gh = get_api_client("github", "https://api.github.com", "u/r", "tok")
    assert isinstance(gh, GitHubClient)

    try:
        get_api_client("unknown_platform", "https://example.com", "u/r", "tok")
        assert False, "should raise ValueError"
    except ValueError as e:
        assert "Unknown provider type" in str(e)
        assert "gitea" in str(e)
        assert "github" in str(e)

    # Test custom runtime registration
    class CustomClient:
        def __init__(self, api, repo, token):
            self.api = api

    register_provider("custom", CustomClient)
    custom = get_api_client("custom", "https://custom.example.com", "u/r", "tok")
    assert isinstance(custom, CustomClient)
    assert custom.api == "https://custom.example.com"
    print("  ✓ providers: registry lookup and dynamic registration")


def test_resolve_author_helper():
    from gitacross.config import _AuthorConfig
    from gitacross.target import _resolve_author

    # Disabled / empty
    assert _resolve_author(None) == (None, None)
    assert _resolve_author(_AuthorConfig({})) == (None, None)

    # Name only
    assert _resolve_author(_AuthorConfig({"name": "Bot"})) == ("Bot", None)

    # Full author
    assert _resolve_author(_AuthorConfig({"name": "Bot", "email": "bot@example.com"})) == (
        "Bot",
        "bot@example.com",
    )
    print("  ✓ target: _resolve_author helper")


def test_config_stream_assets():
    from gitacross.config import Config

    yaml_text = """
projects:
  - name: p_default
    source:
      type: gitea
      repo: o/r
    target:
      type: github
      repo: o/r
  - name: p_stream
    stream_assets: true
    source:
      type: gitea
      repo: o/r
    target:
      type: github
      repo: o/r
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(yaml_text)
        path = f.name

    try:
        cfg = Config(path)
        assert cfg.projects[0].stream_assets is False
        assert cfg.projects[1].stream_assets is True
    finally:
        os.unlink(path)

    print("  ✓ config: stream_assets parsing and default")


def test_gitea_multipart_reader_stream():
    from gitacross.providers.gitea import _MultipartReader

    header = b"--boundary\r\nheader\r\n\r\n"
    footer = b"\r\n--boundary--\r\n"
    file_content = b"streaming file chunk payload"

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(file_content)
        tmp_path = Path(tmp.name)

    try:
        with _MultipartReader(header, tmp_path, footer) as reader:
            chunk1 = reader.read(10)
            chunk2 = reader.read(15)
            rest = reader.read()

        total = chunk1 + chunk2 + rest
        assert total == header + file_content + footer
    finally:
        tmp_path.unlink(missing_ok=True)

    print("  ✓ gitea: _MultipartReader streams exact multipart payload")


def test_github_streaming_upload():
    import json
    from gitacross.providers.github import GitHubClient

    gh = GitHubClient("https://api.github.com", "owner/repo", "token123")
    upload_resp = json.dumps({"id": 200, "name": "streamed.zip"}).encode()
    resp_mock = mock.MagicMock()
    resp_mock.read.return_value = upload_resp
    resp_mock.__enter__.return_value = resp_mock

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(b"huge asset content payload")
        tmp_path = Path(tmp.name)

    try:
        with mock.patch("urllib.request.urlopen", return_value=resp_mock) as m_open:
            rel_dict = {"id": 1, "upload_url": "https://uploads.github.com/repos/owner/repo/releases/1/assets{?name,label}"}
            res = gh.upload_asset(rel_dict, tmp_path, name="streamed.zip", stream=True)
            assert res["id"] == 200
            req = m_open.call_args[0][0]
            assert req.headers["Content-length"] == str(len(b"huge asset content payload"))
            assert req.headers["Content-type"] == "application/octet-stream"
            # req.data is an open file object in stream mode
            assert hasattr(req.data, "read")
    finally:
        tmp_path.unlink(missing_ok=True)

    print("  ✓ github: streaming asset upload")


# ---------------------------------------------------------------------------
# Linter Tests
# ---------------------------------------------------------------------------


def test_linter_invalid_yaml():
    from gitacross.linter import ConfigLinter, LintSeverity

    linter = ConfigLinter()
    report = linter.lint_yaml_string("projects: [invalid yaml: {")
    assert not report.is_valid
    assert len(report.errors) == 1
    assert "YAML syntax error" in report.errors[0].message
    print("  ✓ linter: detects invalid YAML syntax")


def test_linter_missing_required_fields():
    from gitacross.linter import ConfigLinter, LintSeverity

    linter = ConfigLinter()
    report = linter.lint_yaml_string("""
- name: missing-endpoints
- name: bad-source
  source:
    type: gitea
  target:
    type: github
""")
    assert not report.is_valid
    error_messages = [e.message.lower() for e in report.errors]
    assert any("missing required 'source'" in m for m in error_messages)
    assert any("missing required 'target'" in m for m in error_messages)
    assert any("missing required 'repo'" in m for m in error_messages)
    print("  ✓ linter: detects missing required fields")


def test_linter_misplaced_and_unknown_keys():
    from gitacross.linter import ConfigLinter

    linter = ConfigLinter()
    report = linter.lint_yaml_string("""
- name: misplaced-app
  mode: commit
  unknown_project_opt: true
  source:
    type: gitea
    repo: a/b
    api: https://git.example.com
    token: tok
  target:
    type: github
    repo: a/b
    api: https://api.github.com
    token: tok
""")
    assert not report.is_valid
    assert any("mode: commit" in e.message and "belongs under 'source:'" in e.message for e in report.errors)
    assert any("Unrecognized project-level option 'unknown_project_opt'" in w.message for w in report.warnings)
    print("  ✓ linter: detects misplaced and unrecognized keys")


def test_linter_redundant_options():
    from gitacross.linter import ConfigLinter

    linter = ConfigLinter()
    report = linter.lint_yaml_string("""
- name: redundant-app
  enabled: true
  preserve_description: true
  sync_assets: false
  stream_assets: false
  source:
    type: gitea
    repo: a/b
    api: https://git.example.com
    token: tok
    mode: release
    include_prereleases: false
    include_drafts: false
    sync_from: ""
  target:
    type: github
    repo: a/b
    api: https://api.github.com
    token: tok
    branch: main
  retry:
    max_attempts: 3
    backoff_seconds: 2
  renderer:
    operations:
      - remove:
          - path: .gitea
            pattern: literal
      - rename:
          - from: old.txt
            to: new.txt
            pattern: literal
      - replace:
          - search: foo
            replace: bar
            pattern: literal
""")
    assert report.is_valid  # redundant options are not errors
    red_msgs = [r.message for r in report.redundant]
    assert any("'enabled: true' is redundant" in m for m in red_msgs)
    assert any("'preserve_description: true' is redundant" in m for m in red_msgs)
    assert any("'sync_assets: false' is redundant" in m for m in red_msgs)
    assert any("'stream_assets: false' is redundant" in m for m in red_msgs)
    assert any("'source.mode: release' is redundant" in m for m in red_msgs)
    assert any("'source.include_prereleases: false' is redundant" in m for m in red_msgs)
    assert any("'source.include_drafts: false' is redundant" in m for m in red_msgs)
    assert any("'target.branch: main' is redundant" in m for m in red_msgs)
    assert any("'retry.max_attempts: 3' is redundant" in m for m in red_msgs)
    assert any("'retry.backoff_seconds: 2' is redundant" in m for m in red_msgs)
    assert any("'pattern: literal' in remove operation is redundant" in m for m in red_msgs)
    assert any("'pattern: literal' in rename operation is redundant" in m for m in red_msgs)
    assert any("'pattern: literal' in replace operation is redundant" in m for m in red_msgs)
    print("  ✓ linter: flags redundant defaults")


def test_linter_cli_flag():
    from gitacross.linter import lint_config

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write("""
- name: test-app
  source:
    type: gitea
    repo: a/b
    api: https://git.example.com
    token: tok
  target:
    type: github
    repo: a/b
    api: https://api.github.com
    token: tok
""")
        config_path = f.name

    try:
        report = lint_config(config_path, print_output=False)
        assert report.is_valid
        assert len(report.errors) == 0
        print("  ✓ linter: lint_config CLI helper")
    finally:
        os.unlink(config_path)


def test_config_fixer():
    import yaml
    from gitacross.linter import ConfigFixer, fix_config, lint_config

    raw_yaml = """
- name: messy-app
  mode: commit
  sync_from: ""
  enabled: true
  preserve_description: true
  sync_assets: false
  stream_assets: false
  source:
    type: gitea
    repo: a/b
    api: https://git.example.com
    token: tok
    include_prereleases: false
    include_drafts: false
  target:
    type: github
    repo: a/b
    api: https://api.github.com
    token: tok
    branch: main
  retry:
    max_attempts: 3
    backoff_seconds: 2
  renderer:
    operations:
      - remove:
          - path: .gitea
            pattern: literal
      - rename:
          - from: old.txt
            to: new.txt
            pattern: literal
      - replace:
          - search: foo
            replace: bar
            pattern: literal
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(raw_yaml)
        config_path = f.name

    try:
        report = fix_config(config_path, write_back=True, print_output=False)
        assert report.is_valid
        assert len(report.fixes) > 0

        # Verify the file is fixed
        fixed_data = yaml.safe_load(Path(config_path).read_text())
        p = fixed_data[0]

        # 1. mode was moved into source
        assert "mode" not in p
        assert p["source"]["mode"] == "commit"

        # 2. redundant fields are stripped
        assert "enabled" not in p
        assert "preserve_description" not in p
        assert "sync_assets" not in p
        assert "stream_assets" not in p
        assert "sync_from" not in p
        assert "include_prereleases" not in p["source"]
        assert "include_drafts" not in p["source"]
        assert "branch" not in p["target"]
        assert "retry" not in p
        assert "pattern" not in p["renderer"]["operations"][0]["remove"][0]
        assert "pattern" not in p["renderer"]["operations"][1]["rename"][0]
        assert "pattern" not in p["renderer"]["operations"][2]["replace"][0]

        # 3. Linter on fixed file should report 0 errors, 0 warnings, 0 redundant
        lint_rep = lint_config(config_path, print_output=False)
        assert lint_rep.is_valid
        assert len(lint_rep.errors) == 0
        assert len(lint_rep.warnings) == 0
        assert len(lint_rep.redundant) == 0

        print("  ✓ linter: ConfigFixer automatically cleans and fixes configs")
    finally:
        os.unlink(config_path)


def test_config_fixer_vice_versa():
    """Test that options misplaced in source or target are moved to project level or correct endpoint."""
    import yaml
    from gitacross.linter import fix_config, lint_config

    raw_yaml = """
- name: misplaced-in-endpoints
  source:
    type: gitea
    repo: a/b
    api: https://git.example.com
    token: tok
    sync_assets:
      - "*.tar.gz"
    stream_assets: true
  target:
    type: github
    repo: a/b
    api: https://api.github.com
    token: tok
    mode: commit
    retry:
      max_attempts: 5
      backoff_seconds: 4
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(raw_yaml)
        config_path = f.name

    try:
        report = fix_config(config_path, write_back=True, print_output=False)
        assert report.is_valid
        assert len(report.fixes) > 0

        # Verify the file is fixed
        fixed_data = yaml.safe_load(Path(config_path).read_text())
        p = fixed_data[0]

        # sync_assets and stream_assets moved from source to project level
        assert "sync_assets" not in p["source"]
        assert "stream_assets" not in p["source"]
        assert p["sync_assets"] == ["*.tar.gz"]
        assert p["stream_assets"] is True

        # retry moved from target to project level
        assert "retry" not in p["target"]
        assert p["retry"]["max_attempts"] == 5
        assert p["retry"]["backoff_seconds"] == 4

        # mode: commit moved from target to source
        assert "mode" not in p["target"]
        assert p["source"]["mode"] == "commit"

        # Linter passes
        lint_rep = lint_config(config_path, print_output=False)
        assert lint_rep.is_valid
        assert len(lint_rep.errors) == 0
        assert len(lint_rep.warnings) == 0

        print("  ✓ linter: ConfigFixer vice-versa moves endpoint-misplaced options")
    finally:
        os.unlink(config_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Running self-checks...")
    test_config_source_target()
    test_config_local()
    test_config_enabled()
    test_config_token_resolution()
    test_config_sync_assets()
    test_config_stream_assets()
    test_state()
    test_state_migrates_old_id_keys()
    test_clean_text()
    test_matches_asset_filter()
    test_renderer_ignore()
    test_renderer_remove()
    test_renderer_rename()
    test_renderer_replace()
    test_renderer_replace_literal()
    test_renderer_validate_ok()
    test_renderer_add()
    test_renderer_validate_fail()
    test_git_repo_local_tags()
    test_git_repo_export_tag()
    test_git_repo_commit_and_tag()
    test_git_repo_resolve_and_export_commit()
    test_git_repo_resolve_default_branch_head_mirror()
    test_git_ensure_mirror_prunes_stale_refs()
    test_source_local()
    test_source_local_sync_from()
    test_source_remote_mode_tag_sync_from()
    test_source_remote_sync_from_tag_only_requires_mode_tag()
    test_source_remote_sync_from_filtered_prerelease()
    test_source_remote_sync_from_missing_everywhere()
    test_source_remote_invalid_mode()
    test_source_resolves_commit_sha_and_export_release()
    test_target_local()
    test_target_remote_push_commit_mode()
    test_resolve_author_helper()
    test_retry_success()
    test_retry_failure()
    test_provider_registry()
    test_github_verify_access_ok()
    test_github_verify_access_missing_repo_creates_it()
    test_github_verify_access_missing_repo_creates_it_org()
    test_github_create_repo_is_private()
    test_github_create_repo_404_then_422_private_collision()
    test_github_verify_access_401_raises_directly()
    test_gitea_verify_access_bad_token()
    test_gitea_create_repo_is_private()
    test_gitea_create_repo_404_then_409_private_collision()
    test_gitea_verify_access_missing_repo_creates_it()
    test_github_client_assets()
    test_github_streaming_upload()
    test_gitea_client_assets()
    test_gitea_multipart_reader_stream()
    test_sync_release_assets_orchestration()
    test_e2e_remote_with_assets()
    test_config_preserve_description()
    test_sync_preserve_description_and_prerelease()
    test_git_redact_urls()
    test_linter_invalid_yaml()
    test_linter_missing_required_fields()
    test_linter_misplaced_and_unknown_keys()
    test_linter_redundant_options()
    test_linter_cli_flag()
    test_config_fixer()
    test_config_fixer_vice_versa()
    print("\nAll checks passed ✓")
