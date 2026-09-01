"""Tests for sync_project/run/main APIs — split from the original single-file suite."""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

from tests.conftest import (
    _git_commit,
    _git_tag,
    _make_file,
    _make_git_repo,
    _write_local_config,
)

# ---------------------------------------------------------------------------
# sync_project – enabled flag
# ---------------------------------------------------------------------------


def test_sync_long_text_passthrough():
    """Long commit messages and release bodies pass through unmodified (no truncation)."""
    from gitacross.config import Config
    from gitacross.main import sync_project

    long_body = "b" * 200_000
    long_msg = "m" * 5_000

    yaml_text = (
        "projects:\n"
        "  - name: sync-long-text\n"
        f'    commit_message: "{long_msg}"\n'
        "    source:\n"
        "      type: gitea\n"
        "      repo: src/repo\n"
        "    target:\n"
        "      type: github\n"
        "      repo: tgt/repo\n"
    )
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
                        "tag_name": "v1.0.0",
                        "name": "v1.0.0",
                        "body": long_body,
                        "prerelease": False,
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

                # Full release body passed through untouched (no truncation)
                mock_github.create_release.assert_called_once_with(
                    "v1.0.0", "v1.0.0", long_body, False
                )
                # Full commit message passed through untouched
                mock_git.commit.assert_called_once()
                assert mock_git.commit.call_args[0][1] == long_msg
        finally:
            os.chdir(old_cwd)
            os.unlink(path)

    print("  ✓ sync: long commit messages and release bodies pass through unmodified")


def test_sync_release_failure_not_marked_synced():
    """If create_release fails after the commit, the release is not marked synced."""
    from gitacross.config import Config
    from gitacross.main import sync_project
    from gitacross.state import State

    yaml_text = """
projects:
  - name: sync-fail-release
    source:
      type: gitea
      repo: src/repo
    target:
      type: github
      repo: tgt/repo
    retry:
      max_attempts: 1
      backoff_seconds: 0
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
                        "tag_name": "v1.0.0",
                        "name": "v1.0.0",
                        "body": "Notes",
                        "prerelease": False,
                        "published_at": "2026-08-18T10:00:00Z",
                    }
                ],
                [],
            ]

            mock_github = mock.MagicMock()
            mock_github.create_release.side_effect = RuntimeError("release api down")

            mock_git = mock.MagicMock()
            mock_git.resolve_commit.return_value = "aaaa" * 10
            mock_git.tag_exists.return_value = False
            mock_git.head_sha.return_value = "bbbb" * 10

            with mock.patch("gitacross.source.get_api_client", return_value=mock_gitea), mock.patch("gitacross.target.get_api_client", return_value=mock_github), mock.patch("gitacross.git.GitRepo.ensure_mirror", return_value=mock_git):
                try:
                    _ = sync_project(cfg.projects[0], ".", dry_run=False)
                    assert False, "should have raised"
                except RuntimeError as e:
                    assert "release api down" in str(e)

                # The failed release must not be recorded as synced — next run retries it
                assert not State(".").has_release("sync-fail-release", "v1.0.0")
        finally:
            os.chdir(old_cwd)
            os.unlink(path)

    print("  ✓ sync: failed release is not marked synced (safe to retry next run)")


def test_sync_project_skips_disabled():
    """sync_project must return immediately and make no network/git calls when
    project.enabled is False, regardless of how the caller obtained the project."""
    from gitacross.config import ProjectConfig
    from gitacross.main import sync_project

    raw = {
        "name": "disabled-proj",
        "enabled": False,
        "source": {"type": "local", "path": "/nonexistent"},
        "target": {"type": "local", "path": "/nonexistent"},
    }
    project = ProjectConfig(raw)

    with tempfile.TemporaryDirectory() as tmp, mock.patch(
        "gitacross.main.create_source", side_effect=AssertionError("should not be called")
    ), mock.patch(
        "gitacross.main.create_target", side_effect=AssertionError("should not be called")
    ):
        # Patch create_source / create_target to fail hard if called
        _ = sync_project(project, tmp)  # must not raise

    print("  ✓ sync_project: skips disabled project without making any calls")


# ---------------------------------------------------------------------------
# run() – public Python API
# ---------------------------------------------------------------------------


def test_run_raises_on_missing_config():
    import gitacross

    try:
        _ = gitacross.run("/nonexistent/path/config.yml")
        assert False, "Should have raised FileNotFoundError"
    except FileNotFoundError as e:
        assert "config.yml" in str(e)

    print("  ✓ run: raises FileNotFoundError when config file is missing")


def test_run_raises_on_unknown_project():
    import gitacross

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        _ = f.write("projects:\n  - name: alpha\n    source:\n      type: local\n      path: /x\n    target:\n      type: local\n      path: /y\n")
        config_path = f.name

    try:
        _ = gitacross.run(config_path, project="nonexistent")
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "nonexistent" in str(e)
    finally:
        os.unlink(config_path)

    print("  ✓ run: raises ValueError when named project is not in config")


def test_run_skips_disabled_projects():
    """Disabled projects must not appear in run() results at all."""
    import gitacross

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.yml"
        _write_local_config(cfg, [
            {"name": "enabled-proj"},
            {"name": "disabled-proj", "enabled": False},
        ])

        # sync_project will be called only for the enabled project
        with mock.patch("gitacross.main.sync_project") as mock_sync:
            results = gitacross.run(str(cfg), work_dir=tmp)

        names = [r["project"] for r in results]
        assert "enabled-proj" in names
        assert "disabled-proj" not in names
        assert mock_sync.call_count == 1
        assert mock_sync.call_args[0][0].name == "enabled-proj"

    print("  ✓ run: disabled projects are excluded from results and not synced")


def test_run_project_filter():
    """run(project='name') syncs only the named project."""
    import gitacross

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.yml"
        _write_local_config(cfg, [
            {"name": "proj-a"},
            {"name": "proj-b"},
        ])

        with mock.patch("gitacross.main.sync_project") as mock_sync:
            results = gitacross.run(str(cfg), project="proj-b", work_dir=tmp)

        assert len(results) == 1
        assert results[0]["project"] == "proj-b"
        assert mock_sync.call_count == 1
        assert mock_sync.call_args[0][0].name == "proj-b"

    print("  ✓ run: project= filter syncs only the named project")


def test_run_empty_config():
    """A config with zero projects returns an empty results list."""
    import gitacross

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.yml"
        _ = cfg.write_text("projects: []\n")
        results = gitacross.run(str(cfg), work_dir=tmp)
        assert results == []

    # run() also accepts a Config instance or a stream — no file needed
    with tempfile.TemporaryDirectory() as tmp:
        from gitacross.config import Config

        results = gitacross.run(
            Config(io.StringIO("projects: []\n")), work_dir=tmp
        )
        assert results == []
        results = gitacross.run(io.StringIO("projects: []\n"), work_dir=tmp)
        assert results == []

    print("  ✓ run: config with zero projects returns []")


def test_run_returns_synced_true_on_success():
    import gitacross

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.yml"
        _write_local_config(cfg, [{"name": "proj-ok"}])

        mock_release = [{"tag": "v1.0", "source_commit": "abc", "target_commit": "def", "source_date": "2026-01-01"}]
        with mock.patch("gitacross.main.sync_project", return_value=mock_release):
            results = gitacross.run(str(cfg), work_dir=tmp)

        assert results == [{
            "project": "proj-ok",
            "synced": True,
            "releases_synced": 1,
            "releases": mock_release,
            "error": None,
        }]

    print("  ✓ run: returns synced=True, releases_synced, and releases list when sync_project succeeds")


def test_run_returns_synced_false_on_failure():
    import gitacross

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.yml"
        _write_local_config(cfg, [{"name": "proj-bad"}, {"name": "proj-ok"}])

        call_count = {"n": 0}

        def _side_effect(proj, work_dir=None, dry_run=False, _state=None):
            del work_dir, dry_run
            call_count["n"] += 1
            if proj.name == "proj-bad":
                raise RuntimeError("boom")
            return [{"tag": "v1.0", "source_commit": "a", "target_commit": "b", "source_date": "2026-01-01"}]

        with mock.patch("gitacross.main.sync_project", side_effect=_side_effect):
            results = gitacross.run(str(cfg), work_dir=tmp)

        assert results[0] == {"project": "proj-bad", "synced": False, "releases_synced": 0, "releases": [], "error": "boom"}
        assert results[1]["project"] == "proj-ok"
        assert results[1]["synced"] is True
        assert results[1]["releases_synced"] == 1
        assert results[1]["releases"][0]["tag"] == "v1.0"
        assert results[1]["error"] is None
        assert call_count["n"] == 2  # run() continues after a failure

    print("  ✓ run: returns synced=False on failure and continues remaining projects")


def test_run_dry_run_passed_through():
    import gitacross

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.yml"
        _write_local_config(cfg, [{"name": "proj-x"}])

        with mock.patch("gitacross.main.sync_project", return_value=[]) as mock_sync:
            _ = gitacross.run(str(cfg), dry_run=True, work_dir=tmp)

        _, _, kwargs = mock_sync.mock_calls[0]
        assert kwargs.get("dry_run") is True

    print("  ✓ run: dry_run=True is forwarded to sync_project")


def test_dry_run_makes_no_changes_to_workdir_or_target():
    """Dry-run must not create repos, write to work_dir, or touch the target.

    Regression: dry-run used to clone source/target mirrors into
    work_dir/cache, create work_dir itself, call ensure_repo_exists on the
    target platform (potentially creating the repo), and run target.setup().
    """
    import gitacross

    with tempfile.TemporaryDirectory() as tmp:
        work_dir = Path(tmp) / ".gitsync"
        cfg = Path(tmp) / "config.yml"
        _ = cfg.write_text(
            """
projects:
  - name: dry-run-clean
    source:
      type: gitea
      repo: owner/app
      api: https://gitea.example.com/api/v1
      token: tok
    target:
      type: github
      repo: owner/app
      api: https://api.github.com
      token: tok
      branch: main
"""
        )

        mock_gitea = mock.MagicMock()
        mock_gitea.list_releases.side_effect = [
            [
                {
                    "tag_name": "v1.0.0",
                    "name": "v1.0.0",
                    "body": "First release",
                    "published_at": "2026-08-18T10:00:00Z",
                }
            ],
            [],
        ]
        mock_github = mock.MagicMock()
        mock_repo = mock.MagicMock()
        mock_repo.resolve_commit.return_value = (
            "1111111111111111111111111111111111111111"
        )

        with mock.patch(
            "gitacross.source.get_api_client", return_value=mock_gitea
        ), mock.patch(
            "gitacross.target.get_api_client", return_value=mock_github
        ), mock.patch(
            "gitacross.git.GitRepo.ensure_mirror", return_value=mock_repo
        ) as mock_mirror:
            results = gitacross.run(str(cfg), dry_run=True, work_dir=str(work_dir))

        assert results[0]["synced"] is True
        assert results[0]["releases_synced"] == 1
        assert results[0]["releases"][0]["target_commit"] is None

        # work_dir (state + cache) must not be created or written by a dry-run
        assert not work_dir.exists()

        # The source repo is only checked, never created
        mock_gitea.ensure_repo_exists.assert_called_once_with(create=False)

        # The target is never built in dry-run: no API call, no mirror clone
        mock_github.ensure_repo_exists.assert_not_called()

        # Only the source mirror is cloned, into a temp dir — not work_dir/cache
        assert mock_mirror.call_count == 1
        mirror_dest = mock_mirror.call_args[0][1]
        assert not str(mirror_dest).startswith(str(work_dir))

        # The target repo/branch is never touched
        mock_repo.ensure_branch.assert_not_called()
        mock_repo.commit.assert_not_called()
        mock_repo.push.assert_not_called()
        mock_github.create_release.assert_not_called()

    print(
        "  ✓ dry-run: no repos created, nothing written to work_dir, target untouched"
    )


def test_run_result_error_key_on_success():
    """Every successful result must have error=None (consistent shape)."""
    import gitacross

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.yml"
        _write_local_config(cfg, [{"name": "ok-proj"}, {"name": "ok-proj-2"}])

        with mock.patch("gitacross.main.sync_project", return_value=[]):
            results = gitacross.run(str(cfg), work_dir=tmp)

        for r in results:
            assert "error" in r, f"'error' key missing from result: {r}"
            assert r["error"] is None, f"Expected error=None on success, got {r['error']!r}"
            assert isinstance(r["releases"], list)

    print("  ✓ run: error=None on every successful result")


def test_run_result_error_key_on_failure():
    """Failed results must carry the exception message string under 'error'."""
    import gitacross

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.yml"
        _write_local_config(cfg, [{"name": "bad-proj"}])

        with mock.patch(
            "gitacross.main.sync_project",
            side_effect=RuntimeError("connection refused"),
        ):
            results = gitacross.run(str(cfg), work_dir=tmp)

        assert len(results) == 1
        r = results[0]
        assert r["synced"] is False
        assert "error" in r
        assert "connection refused" in r["error"]
        assert r["releases"] == []

    print("  ✓ run: error=str(exc) on failed result")


def test_releases_synced_count_e2e():
    """Verify releases list and count in sync_project and run across initial sync, idempotency, and dry-run."""
    import gitacross
    from gitacross.config import Config
    from gitacross.state import State

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src_repo"
        tgt = Path(tmp) / "tgt_repo"
        _ = _make_git_repo(src)
        _ = _make_git_repo(tgt)

        # 2 tags in source repo
        _ = _make_file(src, "f1.txt", "v1.0")
        _git_commit(src, "c1")
        _git_tag(src, "v1.0")

        _ = _make_file(src, "f2.txt", "v2.0")
        _git_commit(src, "c2")
        _git_tag(src, "v2.0")

        cfg_path = Path(tmp) / "config.yml"
        _write_local_config(cfg_path, [{"name": "e2e-count", "src": str(src), "tgt": str(tgt)}])
        work_dir = Path(tmp) / ".custom_workdir"

        # 1. Dry run -> reports 2 releases with target_commit=None, ordered oldest-first
        res_dry = gitacross.run(str(cfg_path), dry_run=True, work_dir=str(work_dir))
        assert res_dry[0]["releases_synced"] == 2
        assert res_dry[0]["synced"] is True
        assert len(res_dry[0]["releases"]) == 2
        # Oldest first (head vs tail)
        assert res_dry[0]["releases"][0]["tag"] == "v1.0"
        assert res_dry[0]["releases"][0]["target_commit"] is None
        assert len(res_dry[0]["releases"][0]["source_commit"]) == 40
        assert res_dry[0]["releases"][-1]["tag"] == "v2.0"
        assert res_dry[0]["releases"][-1]["target_commit"] is None

        state = State(str(work_dir))
        assert not state.has_release("e2e-count", "v1.0")

        # 2. Actual run -> syncs 2 releases, populating valid target_commit SHAs
        res = gitacross.run(str(cfg_path), dry_run=False, work_dir=str(work_dir))
        assert res[0]["releases_synced"] == 2
        assert res[0]["synced"] is True
        assert len(res[0]["releases"]) == 2
        assert res[0]["releases"][0]["tag"] == "v1.0"
        assert len(res[0]["releases"][0]["target_commit"]) == 40
        assert res[0]["releases"][-1]["tag"] == "v2.0"
        assert len(res[0]["releases"][-1]["target_commit"]) == 40

        # Verify state.yml exists in work_dir (cache/ is created lazily for remote endpoints)
        assert (work_dir / "state.yml").exists()

        # 3. Second run (idempotent) -> 0 new releases to sync
        res2 = gitacross.run(str(cfg_path), dry_run=False, work_dir=str(work_dir))
        assert res2[0]["releases_synced"] == 0
        assert res2[0]["releases"] == []
        assert res2[0]["synced"] is True

        # 4. Direct sync_project call on disabled project returns []
        config = Config(str(cfg_path))
        config.projects[0].enabled = False
        releases = gitacross.sync_project(config.projects[0], work_dir=str(work_dir))
        assert releases == []

    print("  ✓ run/sync_project: releases_synced count accurate across dry-run, initial sync, and idempotent re-run")


def test_releases_detail_ordering_and_commit_mode():
    """Verify chronological ordering (oldest=head, newest=tail) and commit mode (tag=None)."""
    import gitacross

    with tempfile.TemporaryDirectory() as tmp:
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            cfg_yaml = """
projects:
  - name: commit-mode-app
    source:
      type: gitea
      repo: org/app
      api: https://gitea.example.com/api/v1
      token: secret
      mode: commit
    target:
      type: github
      repo: org/app
      api: https://api.github.com
      token: secret
      branch: main
"""
            _ = Path("config.yml").write_text(cfg_yaml)

            mock_gitea = mock.MagicMock()
            mock_github = mock.MagicMock()
            mock_git = mock.MagicMock()
            mock_git.resolve_default_branch_head.return_value = "c" * 40
            mock_git.tag_commit_date.return_value = "2026-08-22T10:00:00Z"
            mock_git.head_sha.return_value = "d" * 40

            with mock.patch("gitacross.source.get_api_client", return_value=mock_gitea), mock.patch("gitacross.target.get_api_client", return_value=mock_github), mock.patch("gitacross.git.GitRepo.ensure_mirror", return_value=mock_git):
                res = gitacross.run("config.yml", work_dir=tmp)

            assert res[0]["synced"] is True
            assert res[0]["releases_synced"] == 1
            rel = res[0]["releases"][0]
            # In commit mode, tag must be None
            assert rel["tag"] is None
            assert rel["source_commit"] == "c" * 40
            assert rel["target_commit"] == "d" * 40
            assert rel["source_date"] == "2026-08-22T10:00:00Z"

            print("  ✓ run: commit mode returns tag=None and valid commit SHAs")
        finally:
            os.chdir(old_cwd)


def test_run_reset_clears_work_dir(tmp_path):
    """reset=True must wipe work_dir before syncing."""
    import gitacross

    cfg = tmp_path / "config.yml"
    _write_local_config(cfg, [{"name": "proj-r"}])

    work_dir = tmp_path / "custom_gitsync"
    work_dir.mkdir()
    sentinel = work_dir / "sentinel.txt"
    sentinel.write_text("should be gone")

    with mock.patch("gitacross.main.sync_project"):
        _ = gitacross.run(str(cfg), reset=True, work_dir=str(work_dir))

    # Wiped sentinel
    assert not sentinel.exists()

    print("  ✓ run: reset=True removes work_dir/ before syncing")


def test_run_reset_dry_run_keeps_work_dir():
    """reset=True + dry_run=True must not delete work_dir — dry-run changes nothing."""
    import gitacross

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.yml"
        _write_local_config(cfg, [{"name": "proj-r"}])

        work_dir = Path(tmp) / "custom_gitsync"
        work_dir.mkdir()
        sentinel = work_dir / "sentinel.txt"
        _ = sentinel.write_text("must survive")

        with mock.patch("gitacross.main.sync_project"):
            _ = gitacross.run(
                str(cfg), reset=True, dry_run=True, work_dir=str(work_dir)
            )

        # Nothing deleted in dry-run
        assert work_dir.exists()
        assert sentinel.read_text() == "must survive"

    print("  ✓ run: reset=True + dry_run=True leaves work_dir untouched")


# ---------------------------------------------------------------------------
# main() – CLI entry point exit codes
# ---------------------------------------------------------------------------


def test_main_exits_zero_on_success():
    from gitacross.main import main

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.yml"
        _write_local_config(cfg, [{"name": "proj-ok"}])

        with mock.patch("gitacross.main.sync_project", return_value=[]), mock.patch("sys.argv", ["gitacross", "--config", str(cfg)]):
            try:
                main()
            except SystemExit as e:
                assert e.code == 0 or e.code is None, f"Expected exit 0, got {e.code}"

    print("  ✓ main: exits 0 when all projects succeed")


def test_main_exits_nonzero_on_failure():
    from gitacross.main import main

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.yml"
        _write_local_config(cfg, [{"name": "proj-bad"}])

        with mock.patch("gitacross.main.sync_project", side_effect=RuntimeError("boom")), mock.patch("sys.argv", ["gitacross", "--config", str(cfg)]):
            try:
                main()
                assert False, "main() should have called sys.exit"
            except SystemExit as e:
                assert e.code != 0, f"Expected non-zero exit, got {e.code}"

    print("  ✓ main: exits non-zero when a project fails")


def test_main_exits_nonzero_on_missing_config():
    from gitacross.main import main

    with mock.patch("sys.argv", ["gitacross", "--config", "/nonexistent/config.yml"]):
        try:
            main()
            assert False, "main() should have called sys.exit"
        except SystemExit as e:
            assert e.code != 0

    print("  ✓ main: exits non-zero when config file is missing")


def test_main_project_filter():
    """--project flag must reach run() correctly."""
    from gitacross.main import main

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.yml"
        _write_local_config(cfg, [{"name": "alpha"}, {"name": "beta"}])

        synced = []

        def _capture(proj, work_dir=None, dry_run=False, _state=None):
            del work_dir, dry_run
            synced.append(proj.name)
            return []

        with mock.patch("gitacross.main.sync_project", side_effect=_capture), mock.patch("sys.argv", ["gitacross", "--config", str(cfg), "--project", "beta"]):
            try:
                main()
            except SystemExit:
                pass

        assert synced == ["beta"]

    print("  ✓ main: --project flag syncs only the named project")


def test_main_workdir_flag():
    """--workdir flag must pass custom work directory to run()."""
    from gitacross.main import main

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.yml"
        _write_local_config(cfg, [{"name": "alpha"}])
        custom_workdir = Path(tmp) / "custom_workdir"

        captured_paths = []

        def _capture(proj, work_dir=None, dry_run=False, _state=None):
            del proj, dry_run
            captured_paths.append(str(work_dir))
            return []

        with mock.patch("gitacross.main.sync_project", side_effect=_capture), mock.patch("sys.argv", ["gitacross", "--config", str(cfg), "--workdir", str(custom_workdir)]):
            try:
                main()
            except SystemExit:
                pass

        assert captured_paths == [str(custom_workdir)]

    print("  ✓ main: --workdir flag sets custom state and cache directory")


def test_main_lint_flag():
    """main() --lint exits 0 on a valid config and non-zero on lint errors."""
    from gitacross.main import main

    with tempfile.TemporaryDirectory() as tmp:
        good = Path(tmp) / "good.yml"
        _ = good.write_text(
            "projects:\n"
            + "  - name: ok\n"
            + "    source: {type: local, path: /x}\n"
            + "    target: {type: local, path: /y}\n"
        )
        exited: dict[str, Any] = {"code": None}
        with mock.patch("sys.argv", ["gitacross", "--config", str(good), "--lint"]):
            try:
                main()
            except SystemExit as e:
                exited["code"] = e.code
        assert exited["code"] == 0

        bad = Path(tmp) / "bad.yml"
        _ = bad.write_text("projects: [invalid: {")
        exited_bad: dict[str, Any] = {"code": None}
        with mock.patch("sys.argv", ["gitacross", "--config", str(bad), "--lint"]):
            try:
                main()
            except SystemExit as e:
                exited_bad["code"] = e.code
        assert exited_bad["code"] is not None and exited_bad["code"] != 0

    print("  ✓ main: --lint exits 0 on valid config, non-zero on errors")


def test_main_fix_flag():
    """main() --fix rewrites the config, removing redundant options, then exits 0."""
    from gitacross.main import main

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "fixme.yml"
        _ = cfg.write_text(
            "projects:\n"
            + "  - name: p\n"
            + "    enabled: true\n"
            + "    source:\n"
            + "      type: local\n"
            + "      path: /x\n"
            + "      mode: release\n"
            + "    target:\n"
            + "      type: local\n"
            + "      path: /y\n"
        )
        exited: dict[str, Any] = {"code": None}
        with mock.patch("sys.argv", ["gitacross", "--config", str(cfg), "--fix"]):
            try:
                main()
            except SystemExit as e:
                exited["code"] = e.code
        assert exited["code"] == 0

        fixed = cfg.read_text()
        assert "enabled: true" not in fixed
        assert "mode: release" not in fixed

    print("  ✓ main: --fix rewrites config and removes redundant options")


def test_main_fix_flag_dry_run():
    """main() --fix --dry-run previews the fixes but must not rewrite the config."""
    from gitacross.main import main

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "fixme.yml"
        original = (
            "projects:\n"
            + "  - name: p\n"
            + "    enabled: true\n"
            + "    source:\n"
            + "      type: local\n"
            + "      path: /x\n"
            + "      mode: release\n"
            + "    target:\n"
            + "      type: local\n"
            + "      path: /y\n"
        )
        _ = cfg.write_text(original)

        exited: dict[str, Any] = {"code": None}
        captured = io.StringIO()
        with mock.patch("sys.argv", ["gitacross", "--config", str(cfg), "--fix", "--dry-run"]), mock.patch(
            "sys.stdout", new=captured
        ):
            try:
                main()
            except SystemExit as e:
                exited["code"] = e.code
        assert exited["code"] == 0

        # Config must be unchanged by a dry-run fix
        assert cfg.read_text() == original
        assert "[DRY-RUN]" in captured.getvalue()

    print("  ✓ main: --fix --dry-run previews fixes without rewriting config")


def test_main_module_runs():
    """`python -m gitacross --help` exits 0 (covers the __main__ wrapper)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "src")
    result = subprocess.run(
        [sys.executable, "-m", "gitacross", "--help"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0
    assert "usage" in result.stdout.lower()
    print("  ✓ main: python -m gitacross --help exits 0")


def test_package_version_fallback():
    """__version__ falls back to parsing pyproject.toml when not installed."""
    from importlib.metadata import PackageNotFoundError

    import gitacross

    with mock.patch(
        "gitacross._installed_version", side_effect=PackageNotFoundError
    ):
        assert isinstance(gitacross._package_version(), str)
    assert isinstance(gitacross.__version__, str)
    print("  ✓ package: __version__ fallback parses pyproject.toml")


def test_main_module_entrypoint_runpy():
    """Executing gitacross.__main__ as __main__ covers the entry-point guard."""
    import runpy

    with mock.patch("sys.argv", ["gitacross", "--help"]):
        try:
            runpy.run_module("gitacross.__main__", run_name="__main__")
            assert False, "argparse --help should exit"
        except SystemExit as e:
            assert e.code == 0
    print("  ✓ main: __main__ entry point runs via runpy")

def test_render_template_edge_cases():
    """Template/asset-filter/release-context edge cases."""
    from gitacross.main import (
        _matches_asset_filter,
        _release_context,
        _render_template,
    )

    assert _render_template("", {"a": 1}) == ""
    assert _render_template("{unclosed", {}) == "{unclosed"  # except branch

    assert _matches_asset_filter("x", 42) is False

    class FakeSource:
        def resolve_commit(self, tag):
            return "b" * 40

    rel = {"tag_name": "v1", "body": "", "published_at": "2026-01-01"}
    tag, cm, sc, _ss, _sd, _rb, ctx = _release_context(
        rel, FakeSource(), mock.Mock(name="p")
    )
    assert tag == "v1" and cm is False and sc == "b" * 40
    assert ctx["release_name"] == "v1"
    print("  ✓ main: template/asset-filter/release-context edge cases")

def test_sync_project_dry_run_tag_only_source():
    """Dry-run works with duck-typed sources that export tags, not releases."""
    import gitacross
    from gitacross.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.yml"
        _write_local_config(cfg, [{"name": "tag-only", "src": "/x", "tgt": "/y"}])
        config = Config(str(cfg))

        class TagOnlySource:
            def fetch_releases(self):
                return [
                    {
                        "tag_name": "v1.0",
                        "commit_sha": "a" * 40,
                        "body": "",
                        "published_at": "2026-01-01",
                    }
                ]

            def resolve_commit(self, tag):
                return "a" * 40

            def export_tag(self, tag, dest):
                _ = (Path(dest) / "file.txt").write_text("hi")

        with mock.patch("gitacross.main.create_source", return_value=TagOnlySource()):
            releases = gitacross.sync_project(
                config.projects[0], work_dir=tmp, dry_run=True
            )
        assert releases[0]["tag"] == "v1.0"
    print("  ✓ sync: dry-run with a tag-only duck-typed source")

def test_sync_project_real_loop_tag_only_source():
    """The real sync loop also works with tag-only sources (local target)."""
    import gitacross
    from gitacross.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        tgt = Path(tmp) / "tgt"
        _ = _make_git_repo(tgt)
        cfg = Path(tmp) / "config.yml"
        _write_local_config(cfg, [{"name": "tag-only", "src": "/x", "tgt": str(tgt)}])
        config = Config(str(cfg))

        class TagOnlySource:
            def fetch_releases(self):
                return [
                    {
                        "tag_name": "v1.0",
                        "commit_sha": "a" * 40,
                        "body": "",
                        "published_at": "2026-01-01T10:00:00Z",
                    }
                ]

            def resolve_commit(self, tag):
                return "a" * 40

            def export_tag(self, tag, dest):
                _ = (Path(dest) / "file.txt").write_text("hi")

        with mock.patch("gitacross.main.create_source", return_value=TagOnlySource()):
            releases = gitacross.sync_project(
                config.projects[0], work_dir=tmp, dry_run=False
            )
        assert releases[0]["tag"] == "v1.0"
    print("  ✓ sync: real loop with a tag-only duck-typed source")

def test_sync_project_dry_run_with_assets():
    """Dry-run with sync_assets reports assets without downloading them."""
    import gitacross
    from gitacross.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.yml"
        _ = cfg.write_text(
            "projects:\n"
            + "  - name: assets\n"
            + "    sync_assets: true\n"
            + "    commit_message: 'chore(sync): {tag}'\n"
            + "    source: {type: local, path: /x}\n"
            + "    target: {type: local, path: /y}\n"
        )
        config = Config(str(cfg))

        class AssetSource:
            def fetch_releases(self):
                return [
                    {
                        "tag_name": "v1.0",
                        "commit_sha": "a" * 40,
                        "body": "",
                        "assets": [{"name": "app.zip", "size": 100}],
                        "published_at": "2026-01-01",
                    }
                ]

            def resolve_commit(self, tag):
                return "a" * 40

            def export_release(self, rel, dest):
                _ = (Path(dest) / "file.txt").write_text("hi")

        with mock.patch("gitacross.main.create_source", return_value=AssetSource()):
            releases = gitacross.sync_project(
                config.projects[0], work_dir=tmp, dry_run=True
            )
        assert releases[0]["tag"] == "v1.0"
    print("  ✓ sync: dry-run with sync_assets")

def test_sync_release_assets_no_assets_field():
    """_sync_release_assets falls back to source.list_release_assets when rel has none."""
    from gitacross.main import _sync_release_assets

    src = mock.MagicMock()
    src.list_release_assets.return_value = [{"name": "x.zip", "size": 1}]
    rel = {"id": 7, "tag_name": "v1"}  # no assets key

    _sync_release_assets(
        source=src,
        target=None,
        rel=rel,
        tag="v1",
        target_release=None,
        sync_assets="*.zip",
        tmpdir=Path("/tmp"),
        dry_run=True,
    )
    src.list_release_assets.assert_called_with(7)

    # nothing matches the filter → debug log + return
    _sync_release_assets(
        source=src,
        target=None,
        rel=rel,
        tag="v1",
        target_release=None,
        sync_assets="*.exe",
        tmpdir=Path("/tmp"),
        dry_run=True,
    )
    print("  ✓ sync: release-assets fallback and no-match paths")

def test_main_fix_and_lint_missing_config():
    """--fix/--lint with a missing config exit with the message."""
    from gitacross.main import main

    with tempfile.TemporaryDirectory() as tmp:
        missing = str(Path(tmp) / "nope.yml")
        expected = "Config file not found: " + missing

        for flag in ("--fix", "--lint"):
            with mock.patch("sys.argv", ["gitacross", "--config", missing, flag]):
                try:
                    main()
                    assert False, "should exit"
                except SystemExit as e:
                    assert e.code == expected
    print("  ✓ main: --fix/--lint with missing config exit with the message")

def test_main_fix_invalid_and_fix_lint():
    """--fix with unfixable config exits 1; --fix --lint exits per lint result."""
    from gitacross.main import main

    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "bad.yml"
        _ = bad.write_text("projects: [unclosed: {")

        with mock.patch("sys.argv", ["gitacross", "--config", str(bad), "--fix"]):
            try:
                main()
                assert False, "should exit"
            except SystemExit as e:
                assert e.code == 1

        good = Path(tmp) / "good.yml"
        _ = good.write_text(
            "projects:\n"
            + "  - name: p\n"
            + "    enabled: true\n"
            + "    source: {type: local, path: /a}\n"
            + "    target: {type: local, path: /b}\n"
        )
        with mock.patch(
            "sys.argv", ["gitacross", "--config", str(good), "--fix", "--lint"]
        ):
            try:
                main()
                assert False, "should exit"
            except SystemExit as e:
                assert e.code == 0
    print("  ✓ main: --fix invalid exits 1; --fix --lint exits per lint")

def test_main_module_plain_import():
    """Importing gitacross.__main__ (not as __main__) must not run main()."""
    import importlib

    _ = importlib.import_module("gitacross.__main__")
    print("  ✓ main: __main__ guard is False on plain import")

def test_main_module_as_script():
    """Running gitacross.main as __main__ (python main.py) exits via argparse."""
    import runpy
    import warnings

    with mock.patch("sys.argv", ["gitacross", "--help"]), warnings.catch_warnings():
        # runpy warns when re-executing an already-imported module — expected here
        warnings.simplefilter("ignore", RuntimeWarning)
        try:
            runpy.run_module("gitacross.main", run_name="__main__")
            assert False, "should exit"
        except SystemExit as e:
            assert e.code == 0
    print("  ✓ main: main module runs as __main__")
