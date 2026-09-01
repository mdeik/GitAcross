"""Tests for target endpoints and retry — split from the original single-file suite."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from tests.conftest import (
    _make_file,
    _make_git_repo,
)

# ---------------------------------------------------------------------------
# Target (local target commit)
# ---------------------------------------------------------------------------


def test_target_local():
    from gitacross.config import _EndpointConfig
    from gitacross.target import create_target

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _ = _make_git_repo(repo)

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
        _ = _make_file(work, "artifact.txt", "hello")
        _ = tgt.commit(work, "Release v1.0")
        tgt.tag("v1.0", "Release v1.0")

        assert tgt.tag_exists("v1.0") is True
        assert tgt.head_sha()

        # Push is a no-op for local — should not crash with or without tag
        tgt.push("main", "v1.0")
        tgt.push("main")
        _ = tgt.create_release("v1.0", "v1.0", "body")
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

        with mock.patch("gitacross.target.get_api_client", return_value=mock_api), mock.patch("gitacross.git.GitRepo.ensure_mirror", return_value=mock_git):
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


def test_retry_zero_attempts():
    """retry with max_attempts=0 exits the loop without calling fn."""
    from gitacross.retry import retry

    called = {"n": 0}

    def fn():
        called["n"] += 1
        return "x"

    assert retry(fn, max_attempts=0) is None
    assert called["n"] == 0
    print("  ✓ retry: zero attempts returns None without calling fn")

def test_create_target_unknown_type():
    from gitacross.config import _EndpointConfig
    from gitacross.target import create_target

    cfg = _EndpointConfig(
        {"type": "bitbucket", "repo": "a/b", "api": "https://x"}, is_source=False
    )
    try:
        _ = create_target(cfg, "cache")
        assert False, "should raise"
    except ValueError as e:
        assert "Unknown target type" in str(e)
    print("  ✓ target: unknown type rejected")

def test_remote_target_no_clone_url_raises():
    """A remote target without any clone URL raises a clear error."""
    from gitacross.config import _EndpointConfig
    from gitacross.target import create_target

    cfg = _EndpointConfig(
        {"type": "github", "repo": "", "api": "", "token": ""}, is_source=False
    )
    mock_api = mock.MagicMock()
    mock_api.ensure_repo_exists.return_value = None
    with mock.patch("gitacross.target.get_api_client", return_value=mock_api):
        try:
            _ = create_target(cfg, "cache")
            assert False, "should raise"
        except RuntimeError as e:
            assert "Could not determine clone URL" in str(e)
    print("  ✓ target: missing clone URL raises")

def test_remote_target_misc_methods():
    """Remote target tag_exists/repo_path/upload paths (with and without a release)."""
    from gitacross.config import _EndpointConfig
    from gitacross.target import create_target

    cfg = _EndpointConfig(
        {
            "type": "github",
            "repo": "owner/repo",
            "api": "https://api.github.com",
            "token": "tok",
        },
        is_source=False,
    )
    mock_api = mock.MagicMock()
    mock_api.ensure_repo_exists.return_value = {
        "clone_url": "https://github.com/owner/repo.git"
    }
    mock_api.get_release_by_tag.return_value = None
    mock_api.upload_asset.return_value = {"id": 1}
    mock_git = mock.MagicMock()

    with mock.patch("gitacross.target.get_api_client", return_value=mock_api), mock.patch(
        "gitacross.git.GitRepo.ensure_mirror", return_value=mock_git
    ):
        tgt = create_target(cfg, "cache")
        tgt.tag_exists("v1")
        assert tgt.repo_path == mock_git.git_dir
        # release=None + get_release_by_tag → None → warn and skip
        assert tgt.upload_release_asset("v1", "f.zip") is None
        # explicit release → upload directly
        assert tgt.upload_release_asset("v1", "f.zip", release={"id": 9}) == {"id": 1}
        mock_api.get_release_by_tag.assert_called_with("v1")
        mock_api.upload_asset.assert_called_with({"id": 9}, "f.zip", name=None, stream=False)
    print("  ✓ target: remote tag_exists/repo_path/upload paths")

def test_local_target_misc_methods():
    """Local target upload is a no-op; repo_path points at the git dir."""
    from gitacross.config import _EndpointConfig
    from gitacross.target import create_target

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _ = _make_git_repo(repo)
        cfg = _EndpointConfig({"type": "local", "path": str(repo)}, is_source=False)
        tgt = create_target(cfg, "cache")
        assert tgt.upload_release_asset("v1", "f.zip") is None
        assert tgt.repo_path == (repo / ".git")
    print("  ✓ target: local upload is a no-op")
