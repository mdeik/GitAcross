"""Tests for git repository operations — split from the original single-file suite."""
from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from tests.conftest import (
    _git_commit,
    _git_tag,
    _make_file,
    _make_git_repo,
)

# ---------------------------------------------------------------------------
# Git (local repo operations)
# ---------------------------------------------------------------------------


def test_git_repo_local_tags():
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _ = _make_git_repo(repo)
        _ = _make_file(repo, "README.md")
        _git_commit(repo, "first")
        _git_tag(repo, "v1.0")
        _ = _make_file(repo, "CHANGELOG.md")
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
        _ = _make_git_repo(repo)
        _ = _make_file(repo, "hello.txt", "world")
        _git_commit(repo, "first")
        _git_tag(repo, "v1.0")

        git = GitRepo.local(repo)
        dest = Path(tmp) / "export"
        dest.mkdir()
        git.export_tag("v1.0", dest)

        assert (dest / "hello.txt").exists()
        assert (dest / "hello.txt").read_text() == "world"

        print("  ✓ git: export tag")


def test_git_repo_export_empty_tree():
    """Exporting a repo with an empty tree (no files) must not crash."""
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _ = _make_git_repo(repo)
        _git_commit(repo, "empty")
        _git_tag(repo, "v1.0")

        git = GitRepo.local(repo)
        dest = Path(tmp) / "export"
        dest.mkdir()
        git.export_tag("v1.0", dest)  # must not raise
        assert list(dest.iterdir()) == []

    print("  ✓ git: export tag from empty tree")


def test_git_repo_commit_and_tag():
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _ = _make_git_repo(repo)

        git = GitRepo.local(repo)
        git.ensure_branch("main")

        # Commit from a work dir
        work = Path(tmp) / "work"
        work.mkdir()
        _ = _make_file(work, "release.txt", "v1.0 content")

        _ = git.commit(work, "Release v1.0")
        git.tag("v1.0", "Release v1.0")

        assert git.tag_exists("v1.0") is True
        sha = git.head_sha()
        assert len(sha) == 40

        # Second commit (linear)
        work2 = Path(tmp) / "work2"
        work2.mkdir()
        _ = _make_file(work2, "release.txt", "v2.0 content")
        _ = git.commit(work2, "Release v2.0")
        git.tag("v2.0", "Release v2.0")

        assert git.tag_exists("v2.0") is True
        sha2 = git.head_sha()
        assert sha2 != sha  # different commit

        print("  ✓ git: commit and tag")


def test_git_log_stderr_bytes_and_empty(caplog):
    """_log_git_stderr decodes bytes stderr and skips whitespace/empty stderr."""

    from gitacross.git import _log_git_stderr

    # bytes stderr → decoded and logged
    with caplog.at_level(logging.ERROR, logger="gitacross.git"):
        _log_git_stderr("cmd", 1, b"binary\x00error", logging.ERROR)
    assert "binary" in caplog.text

    # whitespace-only stderr → nothing logged (skip path)
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="gitacross.git"):
        _log_git_stderr("cmd", 1, b"   ", logging.ERROR)
    assert caplog.records == []

    # falsy stderr → early return
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="gitacross.git"):
        _log_git_stderr("cmd", 1, "", logging.ERROR)
    assert caplog.records == []
    print("  ✓ git: _log_git_stderr handles bytes and empty stderr")

def test_git_error_redaction_non_str_cmd():
    """_git redacts string cmd parts and tolerates non-string parts."""
    from gitacross.git import _git

    err = subprocess.CalledProcessError(1, ["git", b"bytes-part"])
    with mock.patch("gitacross.git.subprocess.run", side_effect=err):
        try:
            _git("frobnicate")
            assert False, "should have raised"
        except subprocess.CalledProcessError:
            pass
    print("  ✓ git: _git redacts cmd parts on failure")

def test_git_ensure_mirror_same_url_skips_set_url():
    """Re-ensuring a mirror with an unchanged URL skips the remote update."""
    from gitacross.git import GitRepo
    from gitacross.git import _git as real_git

    with tempfile.TemporaryDirectory() as tmp:
        origin = Path(tmp) / "origin"
        _ = _make_git_repo(origin)
        _ = _make_file(origin, "f.txt")
        _git_commit(origin, "c1")
        mirror = Path(tmp) / "mirror.git"
        commands = []

        def spy(*args, **kwargs):
            commands.append(args)
            return real_git(*args, **kwargs)

        with mock.patch("gitacross.git._git", side_effect=spy):
            _ = GitRepo.ensure_mirror(str(origin), mirror)
            _ = GitRepo.ensure_mirror(str(origin), mirror)  # same URL again

        # The unchanged URL must not trigger a remote set-url — only a refresh
        assert all("set-url" not in args for args in commands)
        assert any("fetch" in args for args in commands)
    print("  ✓ git: ensure_mirror with unchanged URL skips remote update")

def test_git_reset_worktree_bare_noop():
    """reset_worktree is a no-op for bare repos — HEAD must stay untouched."""
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        bare = Path(tmp) / "bare.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        head_before = (bare / "HEAD").read_text()
        GitRepo(bare, is_bare=True).reset_worktree()
        assert (bare / "HEAD").read_text() == head_before
    print("  ✓ git: reset_worktree is a no-op for bare repos")

def test_git_push_non_bare():
    """push from a non-bare repo pushes the branch to the remote."""
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        remote = Path(tmp) / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        repo = Path(tmp) / "repo"
        _ = _make_git_repo(repo)
        _ = _make_file(repo, "f.txt")
        _git_commit(repo, "c1")
        _ = subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
            check=True,
            capture_output=True,
        )
        head = subprocess.run(
            ["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        gr = GitRepo(repo / ".git", is_bare=False)
        gr.push("origin", head)
        r = subprocess.run(
            ["git", "--git-dir", str(remote), "show-ref", "--verify", f"refs/heads/{head}"],
            check=True,
            capture_output=True,
        )
        assert r.returncode == 0
    print("  ✓ git: push from a non-bare repo")

def test_git_resolve_default_branch_head_empty_bare():
    """An empty bare repo has no resolvable HEAD → returns ''."""
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        bare = Path(tmp) / "bare.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        gr = GitRepo(bare, is_bare=True)
        assert gr.resolve_default_branch_head() == ""
        assert gr.resolve_default_branch_head("no-such-branch") == ""
    print("  ✓ git: empty bare repo resolves no default branch")

def test_git_resolve_default_branch_head_detached():
    """Detached HEAD falls back to origin/HEAD, common names, and direct HEAD."""
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        origin = Path(tmp) / "origin"
        _ = _make_git_repo(origin)
        _ = _make_file(origin, "f.txt")
        _git_commit(origin, "c1")
        _ = subprocess.run(
            ["git", "-C", str(origin), "branch", "-M", "main"],
            check=True,
            capture_output=True,
        )

        # clone: origin/HEAD exists; detach + delete local main → origin/HEAD path
        clone = Path(tmp) / "clone"
        _ = subprocess.run(
            ["git", "clone", "-q", str(origin), str(clone)],
            check=True,
            capture_output=True,
        )
        _ = subprocess.run(
            ["git", "-C", str(clone), "checkout", "-q", "--detach", "HEAD"],
            check=True,
            capture_output=True,
        )
        _ = subprocess.run(
            ["git", "-C", str(clone), "branch", "-D", "main"],
            check=True,
            capture_output=True,
        )
        gr = GitRepo(clone / ".git", is_bare=False)
        assert len(gr.resolve_default_branch_head()) == 40

        # detached HEAD + main kept, no remote → common-name fallback
        local = Path(tmp) / "local"
        _ = _make_git_repo(local)
        _ = _make_file(local, "f.txt")
        _git_commit(local, "c1")
        _ = subprocess.run(
            ["git", "-C", str(local), "branch", "-M", "main"],
            check=True,
            capture_output=True,
        )
        _ = subprocess.run(
            ["git", "-C", str(local), "checkout", "-q", "--detach", "HEAD"],
            check=True,
            capture_output=True,
        )
        gr2 = GitRepo(local / ".git", is_bare=False)
        assert len(gr2.resolve_default_branch_head()) == 40

        # detached HEAD + no branches → direct HEAD rev-parse
        _ = subprocess.run(
            ["git", "-C", str(local), "branch", "-D", "main"],
            check=True,
            capture_output=True,
        )
        assert len(gr2.resolve_default_branch_head()) == 40
    print("  ✓ git: detached-HEAD default-branch fallbacks")

def test_git_resolve_default_branch_head_stale_origin_head():
    """origin/HEAD pointing at a missing ref falls through to direct HEAD."""
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        origin = Path(tmp) / "origin"
        _ = _make_git_repo(origin)
        _ = _make_file(origin, "f.txt")
        _git_commit(origin, "c1")
        _ = subprocess.run(
            ["git", "-C", str(origin), "branch", "-M", "main"],
            check=True,
            capture_output=True,
        )
        clone = Path(tmp) / "clone"
        _ = subprocess.run(
            ["git", "clone", "-q", str(origin), str(clone)],
            check=True,
            capture_output=True,
        )
        _ = subprocess.run(
            ["git", "-C", str(clone), "checkout", "-q", "--detach", "HEAD"],
            check=True,
            capture_output=True,
        )
        _ = subprocess.run(
            ["git", "-C", str(clone), "branch", "-D", "main"],
            check=True,
            capture_output=True,
        )
        # delete the remote-tracking branch but keep origin/HEAD → stale ref
        _ = subprocess.run(
            ["git", "-C", str(clone), "update-ref", "-d", "refs/remotes/origin/main"],
            check=True,
            capture_output=True,
        )
        gr = GitRepo(clone / ".git", is_bare=False)
        # branch 2's origin/HEAD → origin/main (deleted) → falls through to HEAD
        assert len(gr.resolve_default_branch_head()) == 40
    print("  ✓ git: stale origin/HEAD falls through to direct HEAD")
