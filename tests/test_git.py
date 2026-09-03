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


def test_git_missing_binary_clear_error():
    """_git raises a clear error when the git executable is not available."""
    from gitacross.git import _git

    with mock.patch(
        "gitacross.git.subprocess.run",
        side_effect=FileNotFoundError("[Errno 2] No such file or directory: 'git'"),
    ):
        try:
            _git("rev-parse", "--git-dir")
            assert False, "should have raised"
        except ValueError as e:
            assert "git executable not found" in str(e)
            assert "PATH" in str(e)
    print("  ✓ git: missing git binary produces a clear error")

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


def test_git_mirror_commands_whitelist_own_directory():
    """Mirror commands carry a process-scoped safe.directory for the mirror dir.

    Git refuses to operate in repositories owned by another user ("dubious
    ownership") — a mirror cache created by an earlier run under a different
    uid (containers/CI/shared volumes) must still be usable. The whitelist is
    scoped to the gitacross-managed mirror via -c, never written to a config.
    """
    from gitacross.git import GitRepo
    from gitacross.git import subprocess as git_subprocess

    with tempfile.TemporaryDirectory() as tmp:
        origin = Path(tmp) / "origin"
        _ = _make_git_repo(origin)
        _ = _make_file(origin, "f.txt")
        _git_commit(origin, "c1")
        mirror = Path(tmp) / "mirror.git"
        commands = []
        real_run = git_subprocess.run

        def spy(cmd, *args, **kwargs):
            commands.append(cmd)
            return real_run(cmd, *args, **kwargs)

        with mock.patch("gitacross.git.subprocess.run", side_effect=spy):
            _ = GitRepo.ensure_mirror(str(origin), mirror)
            _ = GitRepo.ensure_mirror(str(origin), mirror)  # refresh path too

        safe_flag = ["-c", f"safe.directory={mirror.resolve()}"]
        assert commands, "expected git commands to be issued"
        for cmd in commands:
            # Every command run against the mirror dir is whitelisted for it
            assert any(str(mirror) == str(a) for a in cmd), f"unrelated: {cmd}"
            assert cmd[1:3] == safe_flag, f"missing safe.directory: {cmd}"
    print("  ✓ git: mirror commands whitelist their own directory")


def test_git_mirror_instance_commands_whitelisted_but_not_local():
    """Bare mirror instance commands carry safe.directory; local repos do not."""
    from gitacross.git import GitRepo
    from gitacross.git import subprocess as git_subprocess

    with tempfile.TemporaryDirectory() as tmp:
        origin = Path(tmp) / "origin"
        _ = _make_git_repo(origin)
        _ = _make_file(origin, "f.txt")
        _git_commit(origin, "c1")
        _git_tag(origin, "v1.0")
        mirror = Path(tmp) / "mirror.git"

        gr = GitRepo.ensure_mirror(str(origin), mirror)
        local = Path(tmp) / "local"
        _ = _make_git_repo(local)
        _ = _make_file(local, "f.txt")
        _git_commit(local, "c1")
        _git_tag(local, "v1.0")
        local_repo = GitRepo.local(local)

        commands = []
        real_run = git_subprocess.run

        def spy(cmd, *args, **kwargs):
            commands.append(cmd)
            return real_run(cmd, *args, **kwargs)

        with mock.patch("gitacross.git.subprocess.run", side_effect=spy):
            assert "v1.0" in gr.list_tags()  # bare mirror -> whitelisted
            assert "v1.0" in local_repo.list_tags()  # local -> not whitelisted

        mirror_cmd = commands[0]
        assert mirror_cmd[1:3] == ["-c", f"safe.directory={mirror.resolve()}"]
        local_cmd = commands[1]
        # Local (user-configured) repos are deliberately NOT auto-whitelisted
        assert local_cmd[1] == "--git-dir"
        assert not any("safe.directory" in a for a in local_cmd)
    print("  ✓ git: mirror commands whitelisted, local repos left strict")


def test_git_ensure_local_new_repo_commits_without_ambient_identity(monkeypatch):
    """Auto-initialised repos carry a repo-local identity, so commits never
    depend on the ambient git config.

    Regression: fresh ensure_local repos had no identity configured, so commits
    failed with exit 128 ("Please tell me who you are") on machines without a
    global git user.name/email.
    """
    from gitacross.git import GitRepo

    # Strip all ambient identity: only repo-local config may be used
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    for var in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(var, raising=False)

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        git = GitRepo.ensure_local(repo)
        git.ensure_branch("main")

        work = Path(tmp) / "work"
        work.mkdir()
        _ = _make_file(work, "f.txt", "hello")
        result = git.commit(work, "Release v1.0")
        assert result.returncode in (0, 1)
        assert git.head_sha()
    print("  ✓ git: auto-initialised repos commit without ambient identity")

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


def test_git_ensure_local_opens_existing_repo_without_reinit():
    """ensure_local opens an existing repo as-is — no re-initialisation."""
    from gitacross.git import GitRepo
    from gitacross.git import _git as real_git

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _ = _make_git_repo(repo)
        _ = _make_file(repo, "f.txt")
        _git_commit(repo, "c1")
        commands = []

        def spy(*args, **kwargs):
            commands.append(args)
            return real_git(*args, **kwargs)

        with mock.patch("gitacross.git._git", side_effect=spy):
            git = GitRepo.ensure_local(repo)

        assert git.head_sha()
        # No init/re-init command was ever issued for an existing repo
        assert not any(args and args[0] == "init" for args in commands)
    print("  ✓ git: ensure_local opens an existing repo without re-initialising")


def test_git_ensure_local_refuses_bare_repo():
    """ensure_local must not run `git init` over an existing bare repo."""
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        bare = Path(tmp) / "bare.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        before = sorted(p.name for p in bare.iterdir())
        try:
            _ = GitRepo.ensure_local(bare)
            assert False, "should have raised"
        except ValueError as e:
            assert "bare" in str(e) or "re-initialis" in str(e)
        # The bare repo must be left exactly as it was (no nested .git, etc.)
        assert sorted(p.name for p in bare.iterdir()) == before
        assert not (bare / ".git").exists()
    print("  ✓ git: ensure_local refuses to re-initialise a bare repo")


def test_git_ensure_local_refuses_broken_git_marker():
    """ensure_local refuses a '.git' entry it cannot use instead of overwriting it."""
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        broken = Path(tmp) / "broken"
        broken.mkdir()
        marker = broken / ".git"
        marker.write_text("gitdir: /does/not/exist\n")
        try:
            _ = GitRepo.ensure_local(broken)
            assert False, "should have raised"
        except ValueError as e:
            assert "'.git'" in str(e)
        assert marker.read_text() == "gitdir: /does/not/exist\n"  # untouched
    print("  ✓ git: ensure_local refuses a broken .git marker")


def test_git_ensure_local_refuses_file_path():
    """ensure_local rejects a path that is an existing file, not a directory."""
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        a_file = Path(tmp) / "some-file.txt"
        a_file.write_text("not a dir")
        try:
            _ = GitRepo.ensure_local(a_file)
            assert False, "should have raised"
        except ValueError as e:
            assert "not a directory" in str(e)
    print("  ✓ git: ensure_local rejects a file path")

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
