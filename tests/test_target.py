"""Tests for target endpoints and retry — split from the original single-file suite."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from tests.conftest import (
    _git_commit,
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


def test_target_local_empty_path_raises():
    """A local target without a path must not default to the current directory."""
    from gitacross.config import _EndpointConfig
    from gitacross.target import create_target

    cfg = _EndpointConfig({"type": "local", "path": ""}, is_source=False)
    try:
        _ = create_target(cfg, "cache")
        assert False, "should have raised"
    except ValueError as e:
        assert "path" in str(e).lower()
    print("  ✓ target: local target with empty path is rejected")


def test_target_local_auto_init_missing_dir():
    """A local target whose path does not exist yet is created (mkdir + init)."""
    from gitacross.config import _EndpointConfig
    from gitacross.target import create_target

    with tempfile.TemporaryDirectory() as tmp:
        target_path = Path(tmp) / "nested" / "backup-repo"  # does not exist

        cfg = _EndpointConfig(
            {"type": "local", "path": str(target_path), "branch": "main"},
            is_source=False,
        )

        tgt = create_target(cfg, tmp)
        tgt.setup("main")

        work = Path(tmp) / "work"
        work.mkdir()
        _ = _make_file(work, "artifact.txt", "hello")
        _ = tgt.commit(work, "Release v1.0")

        # Repo + files exist at the configured path, not elsewhere
        assert (target_path / ".git").is_dir()
        assert (target_path / "artifact.txt").read_text() == "hello"
        assert tgt.head_sha()
        print("  ✓ target: local auto-initialises a missing target path")


def test_target_local_auto_init_inside_ancestor_repo():
    """A target path inside another repo must get its own repo, not clobber the ancestor.

    Regression: git resolves a non-repo path inside a repository to the
    enclosing repo, which used to make local targets commit to and hard-reset
    the repository the tool is run from instead of the configured path.
    """
    from gitacross.config import _EndpointConfig
    from gitacross.target import create_target

    with tempfile.TemporaryDirectory() as tmp:
        ancestor = Path(tmp) / "ancestor"
        _ = _make_git_repo(ancestor)
        _ = _make_file(ancestor, "keep.txt", "must survive")
        _git_commit(ancestor, "c1")
        ancestor_head_before = subprocess.run(
            ["git", "-C", str(ancestor), "rev-parse", "HEAD"],
            capture_output=True, check=True, text=True,
        ).stdout.strip()

        target_path = ancestor / "test-repo"  # not a repo itself

        cfg = _EndpointConfig(
            {"type": "local", "path": str(target_path), "branch": "main"},
            is_source=False,
        )

        tgt = create_target(cfg, tmp)
        tgt.setup("main")

        work = Path(tmp) / "work"
        work.mkdir()
        _ = _make_file(work, "artifact.txt", "hello")
        _ = tgt.commit(work, "Release v1.0")

        # The configured path is its own repo with the committed files
        assert (target_path / ".git").is_dir()
        assert (target_path / "artifact.txt").read_text() == "hello"

        # The ancestor repo is untouched
        assert (ancestor / "keep.txt").read_text() == "must survive"
        ancestor_head_after = subprocess.run(
            ["git", "-C", str(ancestor), "rev-parse", "HEAD"],
            capture_output=True, check=True, text=True,
        ).stdout.strip()
        assert ancestor_head_after == ancestor_head_before
        assert not (ancestor / "artifact.txt").exists()
        print("  ✓ target: local auto-init inside an ancestor repo is isolated")


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

def test_remote_target_cache_separates_hosts():
    """Same-named target repos on different hosts get separate mirror caches."""
    from gitacross.config import _EndpointConfig
    from gitacross.target import create_target

    calls = []

    def fake_ensure_mirror(url, dest):
        calls.append((url, dest))
        return mock.MagicMock()

    host_a = _EndpointConfig(
        {"type": "gitea", "repo": "uqkami/Awara",
         "api": "https://git.nodebay.top/api/v1", "token": "t"},
        is_source=False,
    )
    host_b = _EndpointConfig(
        {"type": "gitea", "repo": "uqkami/Awara",
         "api": "https://gitea.example.com/api/v1", "token": "t"},
        is_source=False,
    )
    same_as_a = _EndpointConfig(
        {"type": "gitea", "repo": "uqkami/Awara",
         "api": "https://git.nodebay.top/api/v1", "token": "other"},
        is_source=False,
    )

    mock_apis = []
    for _cfg in (host_a, host_b, same_as_a):
        api = mock.MagicMock()
        api.ensure_repo_exists.return_value = {
            "clone_url": f"https://{_cfg.host}/{_cfg.repo}.git"
        }
        mock_apis.append(api)

    with mock.patch(
        "gitacross.target.get_api_client", side_effect=mock_apis
    ), mock.patch(
        "gitacross.git.GitRepo.ensure_mirror", side_effect=fake_ensure_mirror
    ):
        _ = create_target(host_a, "cache")
        _ = create_target(host_b, "cache")
        _ = create_target(same_as_a, "cache")

    dest_a, dest_b, dest_same = (Path(d) for _, d in calls)
    assert "target_gitea_git.nodebay.top_uqkami_Awara_" in str(dest_a)
    assert "target_gitea_gitea.example.com_uqkami_Awara_" in str(dest_b)
    for d in (dest_a, dest_b):
        assert str(d).endswith(".git")
    assert dest_a != dest_b
    assert dest_a == dest_same  # same host + repo shares one mirror (dedup)
    print("  ✓ target: remote target cache dirs are separated per host")


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
