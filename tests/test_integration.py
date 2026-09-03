"""Tests for end-to-end sync — split from the original single-file suite."""
from __future__ import annotations

import email.message
import io
import json
import os
import subprocess
import tempfile
import urllib.error
from pathlib import Path
from typing import final
from unittest import mock

from tests.conftest import (
    _git_commit,
    _git_tag,
    _make_file,
    _make_git_repo,
)

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
        _ = _make_git_repo(src)
        _ = _make_file(src, "README.md", "# Project")
        _ = _make_file(src, ".env", "SECRET=1")
        _ = _make_file(src, "src/main.py", "print(1)")
        _git_commit(src, "first")
        _git_tag(src, "v1.0")

        _ = _make_file(src, "src/main.py", "print(2)")
        _ = _make_file(src, "CHANGELOG.md", "## v2")
        _git_commit(src, "second")
        _git_tag(src, "v2.0")

        # ── Empty target repo ──
        tgt = tmp / "target"
        _ = _make_git_repo(tgt)

        # ── Config ──
        cfg = tmp / "config.yml"
        _ = cfg.write_text(f"""
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

            # ── First run ──
            _ = sync_project(config.projects[0], ".", dry_run=False)

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
                check=True,
                text=True,
            )
            tags = r.stdout.strip().split()
            assert "v1.0" in tags
            assert "v2.0" in tags

            # Linear history: v1.0 is parent of v2.0
            r = subprocess.run(
                ["git", "-C", str(tgt), "merge-base", "--is-ancestor", "v1.0", "v2.0"],
                check=True,
            )
            assert r.returncode == 0, "v1.0 should be ancestor of v2.0"

            # State recorded with source_commit and target_commit (no redundant commit_sha)
            st = State(".")
            assert st.has_release("e2e", "v1.0")
            assert st.has_release("e2e", "v2.0")
            rel1 = st._data["projects"]["e2e"]["releases"]["v1.0"]
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
                check=True,
                text=True,
            ).stdout.strip()

            _ = sync_project(config.projects[0], ".", dry_run=False)

            log_after = subprocess.run(
                ["git", "-C", str(tgt), "rev-list", "--count", "HEAD"],
                capture_output=True,
                check=True,
                text=True,
            ).stdout.strip()
            assert log_before == log_after, "second run should not create new commits"

            print("  ✓ e2e: local → local (sync, state, idempotency)")
        finally:
            os.chdir(old_cwd)


def test_e2e_local_target_auto_init_inside_ancestor_repo():
    """Local target path is created as its own repo — never the enclosing repo.

    Regression: a target path that is not itself a repo but sits inside another
    repository used to resolve to the enclosing repo, so syncs committed to and
    hard-reset the directory the tool runs from instead of the configured path.
    """
    from gitacross.config import Config
    from gitacross.main import sync_project

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # ── Source repo: one tag ──
        src = tmp / "source"
        _ = _make_git_repo(src)
        _ = _make_file(src, "README.md", "# Project")
        _git_commit(src, "first")
        _git_tag(src, "v1.0")

        # ── Ancestor repo that must NOT receive the sync ──
        ancestor = tmp / "ancestor"
        _ = _make_git_repo(ancestor)
        _ = _make_file(ancestor, "keep.txt", "untouched")
        _git_commit(ancestor, "c1")
        ancestor_head_before = subprocess.run(
            ["git", "-C", str(ancestor), "rev-parse", "HEAD"],
            capture_output=True, check=True, text=True,
        ).stdout.strip()

        # ── Target path inside the ancestor repo, not itself a repo ──
        tgt = ancestor / "test-repo"

        cfg = tmp / "config.yml"
        _ = cfg.write_text(f"""
- name: e2e-auto-init
  source:
    type: local
    path: {src}
    tag_pattern: "v*"
  target:
    type: local
    path: {tgt}
    branch: main
""")

        old_cwd = Path.cwd()
        os.chdir(tmp)
        try:
            config = Config(str(cfg))
            _ = sync_project(config.projects[0], ".", dry_run=False)

            # Synced content lives in the auto-created repo at the configured path
            assert (tgt / ".git").is_dir()
            assert (tgt / "README.md").read_text() == "# Project"
            tags = subprocess.run(
                ["git", "-C", str(tgt), "tag", "-l"],
                capture_output=True, check=True, text=True,
            ).stdout.strip().split()
            assert "v1.0" in tags

            # The ancestor repo was not touched
            assert (ancestor / "keep.txt").read_text() == "untouched"
            assert not (ancestor / "README.md").exists()
            ancestor_head_after = subprocess.run(
                ["git", "-C", str(ancestor), "rev-parse", "HEAD"],
                capture_output=True, check=True, text=True,
            ).stdout.strip()
            assert ancestor_head_after == ancestor_head_before

            print("  ✓ e2e: local target auto-initialised inside an ancestor repo")
        finally:
            os.chdir(old_cwd)


def test_e2e_local_target_existing_repo_on_target_branch_appends():
    """An existing repo already on the target branch keeps its history: release
    commits are appended on top and nothing is re-initialised or rewritten.
    """
    from gitacross.config import Config
    from gitacross.main import sync_project

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # ── Source: two releases ──
        src = tmp / "source"
        _ = _make_git_repo(src)
        for tag, content in (("v1.0", "# v1"), ("v2.0", "# v2")):
            _ = _make_file(src, "README.md", content)
            _git_commit(src, f"c-{tag}")
            _git_tag(src, tag)

        # ── Target: existing repo on main with its own history ──
        tgt = tmp / "target"
        subprocess.run(["git", "init", "-q", "-b", "main", str(tgt)], check=True)
        subprocess.run(["git", "-C", str(tgt), "config", "user.email", "t@test"], check=True)
        subprocess.run(["git", "-C", str(tgt), "config", "user.name", "Test"], check=True)
        _ = _make_file(tgt, "notes.txt", "unrelated pre-existing work")
        _git_commit(tgt, "pre-existing work on main")
        pre = subprocess.run(
            ["git", "-C", str(tgt), "rev-parse", "HEAD"],
            capture_output=True, check=True, text=True,
        ).stdout.strip()

        cfg = tmp / "config.yml"
        _ = cfg.write_text(f"""
- name: e2e-existing
  source:
    type: local
    path: {src}
    tag_pattern: "v*"
  target:
    type: local
    path: {tgt}
    branch: main
""")
        config = Config(str(cfg))
        _ = sync_project(config.projects[0], work_dir=tmp / "state", dry_run=False)

        # History was appended to, not replaced: pre-existing commit is an ancestor
        rc = subprocess.run(
            ["git", "-C", str(tgt), "merge-base", "--is-ancestor", pre, "main"],
            capture_output=True, check=False,
        )
        assert rc.returncode == 0, "existing history should be an ancestor of main"
        count = subprocess.run(
            ["git", "-C", str(tgt), "rev-list", "--count", "main"],
            capture_output=True, check=True, text=True,
        ).stdout.strip()
        assert count == "3"  # pre-existing + v1.0 + v2.0

        # Branch tip carries the latest release content
        assert (tgt / "README.md").read_text() == "# v2"
        tags = subprocess.run(
            ["git", "-C", str(tgt), "tag", "-l"],
            capture_output=True, check=True, text=True,
        ).stdout.strip().split()
        assert "v1.0" in tags and "v2.0" in tags

        # Re-running against the same repo is a no-op (state-driven)
        _ = sync_project(config.projects[0], work_dir=tmp / "state", dry_run=False)
        count2 = subprocess.run(
            ["git", "-C", str(tgt), "rev-list", "--count", "main"],
            capture_output=True, check=True, text=True,
        ).stdout.strip()
        assert count2 == count

        print("  ✓ e2e: existing repo on target branch gets releases appended")


def test_e2e_local_target_existing_repo_on_other_branch_untouched():
    """An existing repo whose history lives on another branch (e.g. master) is
    left untouched: sync builds on its own branch and never touches the other.
    """
    from gitacross.config import Config
    from gitacross.main import sync_project

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        src = tmp / "source"
        _ = _make_git_repo(src)
        _ = _make_file(src, "README.md", "# v1")
        _git_commit(src, "c1")
        _git_tag(src, "v1.0")

        tgt = tmp / "target"
        subprocess.run(["git", "init", "-q", "-b", "master", str(tgt)], check=True)
        subprocess.run(["git", "-C", str(tgt), "config", "user.email", "t@test"], check=True)
        subprocess.run(["git", "-C", str(tgt), "config", "user.name", "Test"], check=True)
        _ = _make_file(tgt, "notes.txt", "unrelated work on master")
        _git_commit(tgt, "pre-existing work on master")
        master_before = subprocess.run(
            ["git", "-C", str(tgt), "rev-parse", "master"],
            capture_output=True, check=True, text=True,
        ).stdout.strip()

        cfg = tmp / "config.yml"
        _ = cfg.write_text(f"""
- name: e2e-other-branch
  source:
    type: local
    path: {src}
    tag_pattern: "v*"
  target:
    type: local
    path: {tgt}
    branch: main
""")
        config = Config(str(cfg))
        _ = sync_project(config.projects[0], work_dir=tmp / "state", dry_run=False)

        # master (and its content) is untouched
        master_after = subprocess.run(
            ["git", "-C", str(tgt), "rev-parse", "master"],
            capture_output=True, check=True, text=True,
        ).stdout.strip()
        assert master_after == master_before
        notes = subprocess.run(
            ["git", "-C", str(tgt), "ls-tree", "-r", "--name-only", "master"],
            capture_output=True, check=True, text=True,
        ).stdout.strip()
        assert notes == "notes.txt"

        # Sync landed on its own new branch with release content
        head = subprocess.run(
            ["git", "-C", str(tgt), "symbolic-ref", "--short", "HEAD"],
            capture_output=True, check=True, text=True,
        ).stdout.strip()
        assert head == "main"
        assert (tgt / "README.md").read_text() == "# v1"
        tags = subprocess.run(
            ["git", "-C", str(tgt), "tag", "-l"],
            capture_output=True, check=True, text=True,
        ).stdout.strip().split()
        assert "v1.0" in tags

        print("  ✓ e2e: existing repo on another branch is left untouched")


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
        _ = _make_git_repo(repo)
        _ = _make_file(repo, "version.txt", "v1.0")
        _git_commit(repo, "c1")
        _git_tag(repo, "v1.0")

        _ = _make_file(repo, "version.txt", "v1.1-hotfix")
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
        _ = _make_git_repo(origin)
        _ = _make_file(origin, "file.txt", "hello")
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
      sync_assets:
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
        _ = f.write(yaml_text)
        path = f.name

    try:
        cfg = Config(path)
        assert cfg.projects[0].sync_assets is True
        assert cfg.projects[1].sync_assets == ["*.tar.gz", "*.zip"]
        assert cfg.projects[2].sync_assets is False
    finally:
        os.unlink(path)

    print("  ✓ config: sync_assets")


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

    from gitacross.providers.github import GitHubClient

    gh = GitHubClient("https://api.github.com", "owner/repo", "token123")

    # 1. get_release_by_tag
    resp_data = json.dumps({"id": 42, "tag_name": "v1.0.0"}).encode()
    resp_mock = mock.MagicMock()
    resp_mock.read.return_value = resp_data
    resp_mock.__enter__.return_value = resp_mock

    with mock.patch("urllib.request.urlopen", return_value=resp_mock) as m_open:
        rel = gh.get_release_by_tag("v1.0.0")
        assert rel is not None
        assert rel["id"] == 42
        req = m_open.call_args[0][0]
        assert req.full_url == "https://api.github.com/repos/owner/repo/releases/tags/v1.0.0"

    # get_release_by_tag 404 returns None
    err = urllib.error.HTTPError(
        url="url", code=404, msg="Not Found", hdrs=email.message.Message(), fp=io.BytesIO(b'{"message": "Not Found"}')
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
        _ = tmp_src.write(b"app content bytes")
        tmp_src_path = Path(tmp_src.name)

    try:
        with mock.patch("urllib.request.urlopen", return_value=resp_up_mock) as m_open:
            rel_dict = {
                "id": 42,
                "upload_url": "https://uploads.github.com/repos/owner/repo/releases/42/assets{?name,label}",
            }
            res = gh.upload_asset(rel_dict, tmp_src_path, name="app.zip")
            assert res is not None
            assert res["id"] == 102
            req = m_open.call_args[0][0]
            assert req.full_url == "https://uploads.github.com/repos/owner/repo/releases/42/assets?name=app.zip"
            assert req.headers["Content-type"] == "application/octet-stream"
            assert req.data == b"app content bytes"

        # 5. upload_asset 422 idempotent
        err422 = urllib.error.HTTPError(
            url="url", code=422, msg="Unprocessable Entity", hdrs=email.message.Message(), fp=io.BytesIO(b'{"message": "already exists"}')
        )
        with mock.patch("urllib.request.urlopen", side_effect=err422):
            res = gh.upload_asset(rel_dict, tmp_src_path, name="app.zip")
            assert res is None
    finally:
        tmp_src_path.unlink(missing_ok=True)

    print("  ✓ github: asset operations (download, upload, idempotency)")


def test_gitea_client_assets():

    from gitacross.providers.gitea import GiteaClient

    gt = GiteaClient("https://gitea.example.com/api/v1", "owner/repo", "token456")

    # 1. get_release_by_tag
    resp_data = json.dumps({"id": 7, "tag_name": "v1.0.0"}).encode()
    resp_mock = mock.MagicMock()
    resp_mock.read.return_value = resp_data
    resp_mock.__enter__.return_value = resp_mock

    with mock.patch("urllib.request.urlopen", return_value=resp_mock) as m_open:
        rel = gt.get_release_by_tag("v1.0.0")
        assert rel is not None
        assert rel["id"] == 7
        req = m_open.call_args[0][0]
        assert req.full_url == "https://gitea.example.com/api/v1/repos/owner/repo/releases/tags/v1.0.0"

    # get_release_by_tag 404 returns None
    err = urllib.error.HTTPError(
        url="url", code=404, msg="Not Found", hdrs=email.message.Message(), fp=io.BytesIO(b'{"message": "Not Found"}')
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
        _ = tmp_src.write(b"tar.gz content")
        tmp_src_path = Path(tmp_src.name)

    try:
        with mock.patch("urllib.request.urlopen", return_value=resp_up_mock) as m_open:
            res = gt.upload_asset(7, tmp_src_path, name="binary.tar.gz")
            assert res is not None
            assert res["id"] == 88
            req = m_open.call_args[0][0]
            assert "https://gitea.example.com/api/v1/repos/owner/repo/releases/7/assets?name=binary.tar.gz" in req.full_url
            assert "multipart/form-data" in req.headers["Content-type"]
            assert b"binary.tar.gz" in req.data
            assert b"tar.gz content" in req.data

        # 4. upload_asset 409 idempotent
        err409 = urllib.error.HTTPError(
            url="url", code=409, msg="Conflict", hdrs=email.message.Message(), fp=io.BytesIO(b'{"message": "attachment already exists"}')
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
            _ = Path("config.yml").write_text(cfg_yaml)
            config = Config("config.yml")

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

            def _fake_download(_asset, dest):
                _ = Path(dest).write_bytes(b"binary-data-prebuilt")

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

            with mock.patch(
                "gitacross.source.get_api_client", return_value=mock_gitea
            ), mock.patch(
                "gitacross.target.get_api_client", return_value=mock_github
            ), mock.patch(
                "gitacross.git.GitRepo.ensure_mirror", return_value=mock_git
            ):
                _ = sync_project(config.projects[0], ".", dry_run=False)

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
                assert State(".").has_release("gitea-to-github", "v1.0.0")

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
      preserve_description: false
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        _ = f.write(yaml_text)
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
        _ = f.write(yaml_text)
        path = f.name

    with tempfile.TemporaryDirectory() as tmp:
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            cfg = Config(path)

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

            with mock.patch("gitacross.source.get_api_client", return_value=mock_gitea), mock.patch("gitacross.target.get_api_client", return_value=mock_github), mock.patch("gitacross.git.GitRepo.ensure_mirror", return_value=mock_git):
                _ = sync_project(cfg.projects[0], ".", dry_run=False)

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


def test_config_commit_message_and_release_description():
    from gitacross.config import Config

    yaml_text = """
projects:
  - name: default-templates
    source: {type: gitea, repo: s/r}
    target: {type: github, repo: t/r}

  - name: custom-templates
    commit_message: "chore: mirror {tag} ({short_sha})"
    release_description: "Upstream notes for {tag}:\\n{body}"
    source: {type: gitea, repo: s/r}
    target: {type: github, repo: t/r}
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        _ = f.write(yaml_text)
        path = f.name
    try:
        cfg = Config(path)
        assert cfg.projects[0].commit_message is None
        assert cfg.projects[0].release_description is None

        assert cfg.projects[1].commit_message == "chore: mirror {tag} ({short_sha})"
        assert cfg.projects[1].release_description == "Upstream notes for {tag}:\n{body}"
    finally:
        os.unlink(path)

    print("  ✓ config: commit_message and release_description parsing")


def test_sync_commit_message_and_release_description_templates():
    from gitacross.config import Config
    from gitacross.main import sync_project

    yaml_text = """
projects:
  - name: sync-template-test
    commit_message: "chore(sync): sync {tag} ({short_sha}) [{project_name}]"
    release_description: "Custom header for {tag} ({short_sha}):\\n\\n{body}"
    source:
      type: gitea
      repo: src/repo
    target:
      type: github
      repo: tgt/repo
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        _ = f.write(yaml_text)
        path = f.name

    with tempfile.TemporaryDirectory() as tmp:
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            cfg = Config(path)

            mock_gitea = mock.MagicMock()
            mock_gitea.list_releases.side_effect = [
                [
                    {
                        "tag_name": "v1.2.3",
                        "name": "v1.2.3",
                        "body": "Original changelog",
                        "prerelease": False,
                        "published_at": "2026-08-25T10:00:00Z",
                    }
                ],
                [],
            ]

            mock_github = mock.MagicMock()
            mock_github.create_release.return_value = {"id": 200}

            mock_git = mock.MagicMock()
            mock_git.resolve_commit.return_value = "1234567890abcdef1234567890abcdef12345678"
            mock_git.tag_exists.return_value = False
            mock_git.head_sha.return_value = "abcdef1234567890abcdef1234567890abcdef12"

            with mock.patch("gitacross.source.get_api_client", return_value=mock_gitea), mock.patch("gitacross.target.get_api_client", return_value=mock_github), mock.patch("gitacross.git.GitRepo.ensure_mirror", return_value=mock_git):
                _ = sync_project(cfg.projects[0], ".", dry_run=False)

                # Check custom commit message passed to commit
                mock_git.commit.assert_called_once()
                call_args = mock_git.commit.call_args
                commit_msg = call_args[0][1]
                assert commit_msg == "chore(sync): sync v1.2.3 (1234567890ab) [sync-template-test]"

                # Check custom release description passed to create_release
                mock_github.create_release.assert_called_once_with(
                    "v1.2.3",
                    "v1.2.3",
                    "Custom header for v1.2.3 (1234567890ab):\n\nOriginal changelog",
                    False,
                )
        finally:
            os.chdir(old_cwd)
            os.unlink(path)

    print("  ✓ e2e: custom commit_message and release_description templates applied during sync")


def test_sync_tmpdir_os_path_and_cleanup():
    from gitacross.config import Config
    from gitacross.main import sync_project

    yaml_text = """
projects:
  - name: sync-tmpdir-test
    source:
      type: gitea
      repo: src/repo
    target:
      type: github
      repo: tgt/repo
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        _ = f.write(yaml_text)
        path = f.name

    with tempfile.TemporaryDirectory() as tmp:
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            cfg = Config(path)

            mock_gitea = mock.MagicMock()
            mock_gitea.list_releases.side_effect = [
                [
                    {
                        "tag_name": "v1.0",
                        "name": "v1.0",
                        "body": "Body",
                        "published_at": "2026-08-25T10:00:00Z",
                    }
                ],
                [],
            ]

            mock_github = mock.MagicMock()
            mock_github.create_release.return_value = {"id": 300}

            mock_git = mock.MagicMock()
            mock_git.resolve_commit.return_value = "aaaa" * 10
            mock_git.tag_exists.return_value = False
            mock_git.head_sha.return_value = "bbbb" * 10

            seen_tmpdirs = []
            orig_mkdtemp = tempfile.mkdtemp

            def _track_mkdtemp(*args, **kwargs):
                d = orig_mkdtemp(*args, **kwargs)
                seen_tmpdirs.append(d)
                return d

            with mock.patch("gitacross.source.get_api_client", return_value=mock_gitea), mock.patch("gitacross.target.get_api_client", return_value=mock_github), mock.patch("gitacross.git.GitRepo.ensure_mirror", return_value=mock_git), mock.patch("tempfile.mkdtemp", side_effect=_track_mkdtemp):
                _ = sync_project(cfg.projects[0], ".", dry_run=False)

            assert len(seen_tmpdirs) == 1
            temp_path = Path(seen_tmpdirs[0])
            # The tmpdir should be created under standard OS temp dir (tempfile.gettempdir())
            assert str(temp_path.parent) == tempfile.gettempdir()
            # And it must be cleaned up / removed after sync completes
            assert not temp_path.exists()
        finally:
            os.chdir(old_cwd)
            os.unlink(path)

    print("  ✓ sync: tmpdir uses OS temp path (tempfile.gettempdir) and cleans up on completion")


def test_linter_commit_message_and_release_description():
    from gitacross.linter import ConfigLinter

    linter = ConfigLinter()
    valid_yaml = """
projects:
  - name: proj-valid
    commit_message: "chore: {tag}"
    release_description: "Release notes: {body}"
    source:
      type: gitea
      repo: src/repo
      api: https://git.example.com
      token: tok
    target:
      type: github
      repo: tgt/repo
      api: https://api.github.com
      token: tok
"""
    report = linter.lint_yaml_string(valid_yaml)
    assert report.is_valid
    assert len(report.errors) == 0
    assert len(report.warnings) == 0

    invalid_yaml = """
projects:
  - name: proj-invalid
    commit_message: 12345
    release_description: ["not", "a", "string"]
    source:
      type: gitea
      repo: src/repo
      api: https://git.example.com
      token: tok
    target:
      type: github
      repo: tgt/repo
      api: https://api.github.com
      token: tok
"""
    report_inv = linter.lint_yaml_string(invalid_yaml)
    assert not report_inv.is_valid
    assert len(report_inv.errors) == 2
    err_msgs = [e.message for e in report_inv.errors]
    assert any("'commit_message' must be a string" in m for m in err_msgs)
    assert any("'release_description' must be a string" in m for m in err_msgs)

    print("  ✓ linter: commit_message and release_description validation")


def test_reprs_are_informative():
    from gitacross.config import Config
    from gitacross.linter import (
        ConfigFixer,
        ConfigLinter,
        FixReport,
        LintIssue,
        LintReport,
        LintSeverity,
    )
    from gitacross.state import State

    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = Path(tmp) / "config.yml"
        _ = cfg_path.write_text(
            "projects:\n"
            + "  - name: proj-a\n"
            + "    source: {type: local, path: /x}\n"
            + "    target: {type: local, path: /y}\n"
        )
        cfg = Config(str(cfg_path))
        proj = cfg.projects[0]

        issue = LintIssue(LintSeverity.ERROR, "boom", project_name="proj-a")

        for obj, needle in [
            (cfg, "projects=1"),
            (proj, "proj-a"),
            (proj, "local"),
            (State(tmp), "work_dir"),
            (ConfigLinter(), "issues=0"),
            (ConfigFixer(), "fixes=0"),
            (issue, "ERROR"),
            (LintReport([issue]), "errors=1"),
            (FixReport([], "", True), "is_valid=True"),
        ]:
            r = repr(obj)
            assert "0x" not in r, f"{type(obj).__name__} repr has a memory address: {r}"
            assert needle in r, f"{type(obj).__name__} repr missing {needle!r}: {r}"

        # FixReport repr must not dump the full config content
        assert "content" not in repr(FixReport([], "x" * 10_000, True))

    print("  ✓ reprs: public classes have informative __repr__ (no memory addresses)")


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
    @final
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
        _ = f.write(yaml_text)
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
        _ = tmp.write(file_content)
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

    from gitacross.providers.github import GitHubClient

    gh = GitHubClient("https://api.github.com", "owner/repo", "token123")
    upload_resp = json.dumps({"id": 200, "name": "streamed.zip"}).encode()
    resp_mock = mock.MagicMock()
    resp_mock.read.return_value = upload_resp
    resp_mock.__enter__.return_value = resp_mock

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        _ = tmp.write(b"huge asset content payload")
        tmp_path = Path(tmp.name)

    try:
        with mock.patch("urllib.request.urlopen", return_value=resp_mock) as m_open:
            rel_dict = {"id": 1, "upload_url": "https://uploads.github.com/repos/owner/repo/releases/1/assets{?name,label}"}
            res = gh.upload_asset(rel_dict, tmp_path, name="streamed.zip", stream=True)
            assert res is not None
            assert res["id"] == 200
            req = m_open.call_args[0][0]
            assert req.headers["Content-length"] == str(len(b"huge asset content payload"))
            assert req.headers["Content-type"] == "application/octet-stream"
            # req.data is an open file object in stream mode
            assert hasattr(req.data, "read")
    finally:
        tmp_path.unlink(missing_ok=True)

    print("  ✓ github: streaming asset upload")


