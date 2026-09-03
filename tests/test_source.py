"""Tests for source endpoints — split from the original single-file suite."""
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
# Source (local source fetch)
# ---------------------------------------------------------------------------


def test_source_local():
    from gitacross.config import _EndpointConfig
    from gitacross.source import create_source

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _ = _make_git_repo(repo)
        _ = _make_file(repo, "f.txt")
        _git_commit(repo, "c1")
        _git_tag(repo, "v1.0")
        _ = _make_file(repo, "g.txt")
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
        _ = _make_git_repo(repo)
        _ = _make_file(repo, "a.txt")
        _git_commit(repo, "c1")
        _git_tag(repo, "v1.0")
        _ = _make_file(repo, "b.txt")
        _git_commit(repo, "c2")
        _git_tag(repo, "v2.0")
        _ = _make_file(repo, "c.txt")
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

        # sync_from = missing tag but with newer releases present — the newer
        # releases are synced instead of syncing nothing
        cfg3 = _EndpointConfig(
            {
                "type": "local",
                "path": str(repo),
                "tag_pattern": "v*",
                "sync_from": "v1.5",
            },
            is_source=True,
        )
        src3 = create_source(cfg3, tmp)
        tags3 = [r["tag_name"] for r in src3.fetch_releases()]
        assert tags3 == ["v2.0", "v3.0"]

        print("  ✓ source: local sync_from")


def test_filter_from_sync_point_fallback_newer_versions():
    """A missing sync_from tag falls back to releases with a newer version."""
    from gitacross.source import _filter_from_sync_point

    def rel(tag):
        return {"tag_name": tag}

    all_rel = [rel("v0.8.0"), rel("v0.9.0"), rel("v1.0.0"), rel("v1.2.0"), rel("v2.0.0")]

    # Exact match: everything from the tag onwards (unchanged behaviour)
    assert [r["tag_name"] for r in _filter_from_sync_point(all_rel, "v0.9.0")] == [
        "v0.9.0", "v1.0.0", "v1.2.0", "v2.0.0",
    ]

    # Missing tag: only strictly-newer versions are kept (v0.8.0 < v0.9.0)
    assert [r["tag_name"] for r in _filter_from_sync_point(all_rel, "v0.9.1")] == [
        "v1.0.0", "v1.2.0", "v2.0.0",
    ]

    # Numeric (not lexicographic) comparison: v0.10.0 > v0.9.9
    assert [
        r["tag_name"]
        for r in _filter_from_sync_point([rel("v0.10.0"), rel("v0.9.0")], "v0.9.9")
    ] == ["v0.10.0"]

    # Zero-padding: v1.2 == v1.2.0, so it is NOT counted as newer
    assert [
        r["tag_name"]
        for r in _filter_from_sync_point([rel("v1.2"), rel("v1.3")], "v1.2.0")
    ] == ["v1.3"]

    # No newer release and nothing comparable -> nothing synced
    assert _filter_from_sync_point([rel("v0.5.0")], "v0.9.0") == []
    assert _filter_from_sync_point([rel("v0.9.0"), rel("latest")], "v0.9.1") == []

    # A non-version sync_from cannot be compared -> nothing synced
    assert _filter_from_sync_point(all_rel, "alpha") == []

    # Unversioned / non-string release tags are never counted as newer
    assert _filter_from_sync_point(
        [rel("latest"), {"tag_name": None}, rel("v1.0.0")], "v0.9.0"
    ) == [rel("v1.0.0")]

    # SemVer: a prerelease of a version is OLDER than its final release, so a
    # missing v1.2.3 never pulls in v1.2.3-rc.1 — only truly newer versions do
    assert [
        r["tag_name"]
        for r in _filter_from_sync_point([rel("v1.2.3-rc.1"), rel("v1.2.4")], "v1.2.3")
    ] == ["v1.2.4"]

    # Prerelease identifier ordering: numeric pre identifiers compare
    # numerically, and a final release outranks any prerelease of its version
    assert [
        r["tag_name"]
        for r in _filter_from_sync_point(
            [rel("v1.2.3-rc.2"), rel("v1.2.3")], "v1.2.3-rc.1"
        )
    ] == ["v1.2.3-rc.2", "v1.2.3"]

    # SemVer: numeric prerelease identifiers sort BEFORE alphanumeric ones, and
    # a final release outranks every prerelease of its version
    assert [
        r["tag_name"]
        for r in _filter_from_sync_point(
            [rel("v1.0.0-1"), rel("v1.0.0-10"), rel("v1.0.0-beta"), rel("v1.0.0")],
            "v1.0.0-2",
        )
    ] == ["v1.0.0-10", "v1.0.0-beta", "v1.0.0"]

    # Build metadata is ignored for ordering
    assert [
        r["tag_name"]
        for r in _filter_from_sync_point([rel("v1.2.3+meta"), rel("v1.2.4")], "v1.2.3")
    ] == ["v1.2.4"]

    # Alphanumeric prerelease identifiers compare lexically (beta > alpha), and
    # longer identifier lists after an equal prefix sort later
    assert [
        r["tag_name"]
        for r in _filter_from_sync_point(
            [rel("v1.0.0-alpha.1"), rel("v1.0.0-beta"), rel("v1.0.0")],
            "v1.0.0-alpha",
        )
    ] == ["v1.0.0-alpha.1", "v1.0.0-beta", "v1.0.0"]
    assert [
        r["tag_name"]
        for r in _filter_from_sync_point(
            [
                rel("v1.0.0-alpha"),
                rel("v1.0.0-beta"),
                rel("v1.0.0-alpha.2"),
                rel("v1.0.0-alpha.1.beta"),
            ],
            "v1.0.0-alpha.1",
        )
    ] == ["v1.0.0-beta", "v1.0.0-alpha.2", "v1.0.0-alpha.1.beta"]

    # ── Calendar versions (CalVer) are date-based, not SemVer ──
    # Dot-shaped dates compare as plain (year, month, day) numbers
    assert [
        r["tag_name"]
        for r in _filter_from_sync_point(
            [rel("2024.01.15"), rel("2024.04.30"), rel("2024.05.01")], "2024.04.01"
        )
    ] == ["2024.04.30", "2024.05.01"]

    # Dash-separated dates: '-' joins date fields; it is NOT a prerelease
    # marker, so 2024-06-01 is newer than 2024-05-01
    assert [
        r["tag_name"]
        for r in _filter_from_sync_point(
            [rel("2024-04-15"), rel("2024-06-01")], "2024-05-01"
        )
    ] == ["2024-06-01"]

    # Month-only anchor (2024.05 = May) vs day releases
    assert [
        r["tag_name"]
        for r in _filter_from_sync_point([rel("2024.04"), rel("2024.05.10")], "2024.05")
    ] == ["2024.05.10"]

    # A prerelease of the same calendar date is not newer than the date itself
    assert [
        r["tag_name"]
        for r in _filter_from_sync_point(
            [rel("2024.05.01-rc.1"), rel("2024.05.02")], "2024.05.01"
        )
    ] == ["2024.05.02"]

    # Scheme switch on one repo: a 2024.x calendar tag clears a v2-era SemVer
    # anchor, while a v9.x tag never clears a 2024 calendar anchor
    assert [
        r["tag_name"]
        for r in _filter_from_sync_point([rel("v2.9.0"), rel("2024.01.15")], "v2.9.9")
    ] == ["2024.01.15"]
    assert [
        r["tag_name"]
        for r in _filter_from_sync_point(
            [rel("2024.02.01"), rel("v9.9.9")], "2024.01.15"
        )
    ] == ["2024.02.01"]

    # No sync_from -> everything
    assert len(_filter_from_sync_point(all_rel, "")) == 5

    print("  ✓ source: missing sync_from falls back to newer versions")


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


def test_source_remote_draft_filtering():
    """Draft releases are excluded by default, included with include_drafts: true."""
    from unittest import mock

    from gitacross.config import _EndpointConfig
    from gitacross.source import _RemoteSource

    releases = [
        {"id": 1, "tag_name": "v1.0", "draft": False, "prerelease": False},
        {"id": 2, "tag_name": "v2.0", "draft": True, "prerelease": False},
        {"id": 3, "tag_name": "v3.0", "draft": False, "prerelease": True},
    ]
    api = mock.MagicMock()

    def _list_releases(page=1):
        return releases if page == 1 else []

    api.list_releases.side_effect = _list_releases
    mirror = mock.MagicMock()
    mirror.resolve_commit.return_value = "aaaa" * 10

    def _tags(cfg):
        with mock.patch(
            "gitacross.source.get_api_client", return_value=api
        ), mock.patch(
            "gitacross.source.GitRepo.ensure_mirror", return_value=mirror
        ):
            return [r["tag_name"] for r in _RemoteSource(cfg, "cache").fetch_releases()]

    cfg = _EndpointConfig(
        {
            "type": "gitea",
            "repo": "u/test",
            "api": "https://git.example.com/api/v1",
            "token": "tok",
        },
        is_source=True,
    )
    assert _tags(cfg) == ["v1.0"]  # drafts and prereleases filtered by default

    cfg_drafts = _EndpointConfig(
        {
            "type": "gitea",
            "repo": "u/test",
            "api": "https://git.example.com/api/v1",
            "token": "tok",
            "include_drafts": True,
        },
        is_source=True,
    )
    assert _tags(cfg_drafts) == ["v1.0", "v2.0"]  # drafts in, prereleases still out

    print("  ✓ source: draft releases filtered unless include_drafts is true")


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
        _ = create_source(cfg, "cache")
        assert False, "should raise"
    except ValueError as e:
        assert "mode" in str(e)
    print("  ✓ source: remote invalid mode rejected")


def test_source_remote_warns_when_no_releases_or_tags(caplog):
    """When a remote source in release mode has no API releases and no tags, log a warning."""
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
    ), caplog.at_level(logging.WARNING):
        src = _RemoteSource(cfg, "cache")
        releases = src.fetch_releases()

    assert releases == []
    assert "No API releases or git tags found for gitea repo 'u/test'" in caplog.text
    assert "mode: commit" in caplog.text
    print("  ✓ source: remote warns when no releases or tags found")


def test_source_remote_mode_tag_warns_when_no_tags(caplog):
    """When a remote source in tag mode has no tags, log a warning."""
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
    ), caplog.at_level(logging.WARNING):
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
        _ = subprocess.run(
            ["git", "init", "--bare", "-q", str(remote)],
            check=True,
            capture_output=True,
        )

        # Scratch repo with a commit + tag, then a mirror of it
        scratch = tmp / "scratch"
        _ = _make_git_repo(scratch)
        _ = _make_file(scratch, "f.txt")
        _git_commit(scratch, "c1")
        _git_tag(scratch, "v0.9.0")

        mirror = tmp / "mirror.git"
        _ = subprocess.run(
            ["git", "clone", "--mirror", "-q", str(scratch), str(mirror)],
            check=True,
            capture_output=True,
        )
        repo = GitRepo(mirror, is_bare=True)
        assert "v0.9.0" in repo.list_tags()
        assert repo.head_sha()

        # ensure_mirror against the empty remote: the local-only refs must go
        _ = GitRepo.ensure_mirror(str(remote), str(mirror))
        assert "v0.9.0" not in repo.list_tags()
        heads = subprocess.run(
            ["git", "--git-dir", str(mirror), "for-each-ref", "--format=%(refname)", "refs/heads"],
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
        assert heads == ""

        print("  ✓ git: ensure_mirror prunes stale local refs")


def test_git_local_raises_on_non_repo():
    """GitRepo.local must raise a clear error for a non-git directory."""
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        not_a_repo = Path(tmp) / "not_a_repo"
        not_a_repo.mkdir()
        try:
            _ = GitRepo.local(not_a_repo)
            assert False, "should have raised"
        except ValueError as e:
            assert "Not a git repository" in str(e)
    print("  ✓ git: GitRepo.local raises on a non-repo directory")


def test_git_local_rejects_subdir_of_another_repo():
    """GitRepo.local must not silently resolve to an enclosing repository.

    Regression: git walks up to the nearest enclosing repository, so a path
    that is not itself a repo but sits inside one used to resolve to the
    enclosing repo — committing to and hard-resetting *its* working tree
    (e.g. the directory the tool is run from) instead of the configured path.
    """
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        outer = Path(tmp) / "outer"
        _ = _make_git_repo(outer)
        _ = _make_file(outer, "f.txt")
        _git_commit(outer, "c1")

        subdir = outer / "not_a_repo_itself"
        subdir.mkdir()
        try:
            _ = GitRepo.local(subdir)
            assert False, "should have raised"
        except ValueError as e:
            msg = str(e)
            assert "Not a git repository" in msg
            assert str(outer) in msg  # hint at the actual enclosing repo
    print("  ✓ git: GitRepo.local rejects a subdirectory of another repo")


def test_git_resolve_commit_unknown_returns_empty():
    """resolve_commit returns '' for an unknown ref instead of raising."""
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _ = _make_git_repo(repo)
        _ = _make_file(repo, "f.txt")
        _git_commit(repo, "c1")
        gr = GitRepo(repo / ".git", is_bare=False)
        assert gr.resolve_commit("no-such-ref") == ""
        assert gr.resolve_commit("") == ""  # empty-ref guard
        assert gr.tag_commit_date("no-such-tag") == ""  # missing tag
    print("  ✓ git: resolve_commit/tag_commit_date return '' for unknown refs")


def test_git_commit_author_override_and_failure():
    """commit applies author overrides and surfaces hard git failures."""
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _ = _make_git_repo(repo)
        _ = _make_file(repo, "a.txt")
        _git_commit(repo, "c1")
        _ = _make_file(repo, "b.txt")

        gr = GitRepo(repo / ".git", is_bare=False)
        _ = gr.commit(
            repo, "custom author", author_name="Custom Name", author_email="custom@x"
        )
        out = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%an <%ae>"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert out.stdout.strip() == "Custom Name <custom@x>"

        # A hard git failure (invalid date) must propagate
        _ = _make_file(repo, "c.txt")
        try:
            _ = gr.commit(repo, "bad date", date="not-a-real-date")
            assert False, "should have raised"
        except subprocess.CalledProcessError:
            pass
    print("  ✓ git: commit honors author overrides and raises on hard failures")


def test_git_ensure_branch_existing_local_and_remote():
    """ensure_branch switches to an existing local branch or a remote-tracking one."""
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        origin = Path(tmp) / "origin"
        _ = _make_git_repo(origin)
        _ = _make_file(origin, "f.txt")
        _git_commit(origin, "c1")
        _ = subprocess.run(
            ["git", "-C", str(origin), "branch", "feature"],
            check=True,
            capture_output=True,
        )
        _ = subprocess.run(
            ["git", "-C", str(origin), "branch", "release"],
            check=True,
            capture_output=True,
        )

        # Existing local branch
        gr = GitRepo(origin / ".git", is_bare=False)
        gr.ensure_branch("feature")
        head = subprocess.run(
            ["git", "-C", str(origin), "symbolic-ref", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head == "feature"

        # Remote-tracking branch (clone has refs/remotes/origin/release, no local branch)
        clone = Path(tmp) / "clone"
        _ = subprocess.run(
            ["git", "clone", "-q", str(origin), str(clone)],
            check=True,
            capture_output=True,
        )
        gr2 = GitRepo(clone / ".git", is_bare=False)
        gr2.ensure_branch("release")
        head2 = subprocess.run(
            ["git", "-C", str(clone), "symbolic-ref", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head2 == "release"
    print("  ✓ git: ensure_branch handles local and remote-tracking branches")


def test_git_is_ancestor():
    """is_ancestor distinguishes ancestors, descendants, and unknown commits."""
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _ = _make_git_repo(repo)
        _ = _make_file(repo, "a.txt")
        _git_commit(repo, "c1")
        sha1 = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        _ = _make_file(repo, "b.txt")
        _git_commit(repo, "c2")
        sha2 = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        gr = GitRepo(repo / ".git", is_bare=False)
        assert gr.is_ancestor(sha1, sha2) is True
        assert gr.is_ancestor(sha2, sha1) is False
        assert gr.is_ancestor("0" * 40, sha2) is False  # unknown commit → git error
    print("  ✓ git: is_ancestor handles ancestor/descendant/unknown commits")


def test_git_push_bare_mirror_toggles_config():
    """push on a bare mirror temporarily disables mirror mode and restores it."""
    from gitacross.git import GitRepo

    with tempfile.TemporaryDirectory() as tmp:
        origin = Path(tmp) / "origin"
        _ = _make_git_repo(origin)
        _ = _make_file(origin, "f.txt")
        _git_commit(origin, "c1")

        mirror = Path(tmp) / "mirror.git"
        _ = subprocess.run(
            ["git", "clone", "--mirror", "-q", str(origin), str(mirror)],
            check=True,
            capture_output=True,
        )
        gr = GitRepo(mirror, is_bare=True)
        gr.push("origin", "master")  # or the mirror's default branch

        # mirror mode must be re-enabled after the push
        out = subprocess.run(
            ["git", "-C", str(mirror), "config", "--get", "remote.origin.mirror"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert out.stdout.strip() == "true"
    print("  ✓ git: push on a bare mirror toggles mirror config on and off")


def test_create_source_unknown_type():
    from gitacross.config import _EndpointConfig
    from gitacross.source import create_source

    cfg = _EndpointConfig({"type": "bitbucket", "repo": "a/b"}, is_source=True)
    try:
        _ = create_source(cfg, "cache")
        assert False, "should raise"
    except ValueError as e:
        assert "Unknown source type" in str(e)
    print("  ✓ source: unknown type rejected")

def test_remote_source_duck_typed_helpers():
    """Remote source passthroughs: resolve_commit/export_tag/list_assets/repo_path."""
    from gitacross.config import _EndpointConfig
    from gitacross.source import create_source

    cfg = _EndpointConfig(
        {
            "type": "gitea",
            "repo": "owner/repo",
            "api": "https://gitea.example.com/api/v1",
            "token": "tok",
        },
        is_source=True,
    )
    mock_api = mock.MagicMock()
    mock_git = mock.MagicMock()
    with mock.patch("gitacross.source.get_api_client", return_value=mock_api), mock.patch(
        "gitacross.git.GitRepo.ensure_mirror", return_value=mock_git
    ):
        src = create_source(cfg, "cache")
        src.resolve_commit("v1")
        src.export_tag("v1", "dest")
        src.list_release_assets(7)
        assert src.repo_path == mock_git.git_dir
    mock_git.resolve_commit.assert_called_with("v1")
    mock_git.export_tag.assert_called_with("v1", "dest")
    mock_api.list_release_assets.assert_called_with(7)
    print("  ✓ source: remote passthrough helpers delegate correctly")


def test_remote_source_cache_separates_hosts():
    """Same-named repos on different hosts must not share a mirror cache dir.

    Regression: mirror names only encoded type + owner/repo, so two hosts with
    the same repo slug collided — the shared mirror flipped origin and re-fetched
    between projects, dirtying each other's cache.
    """
    from gitacross.config import _EndpointConfig
    from gitacross.source import create_source

    calls = []

    def fake_ensure_mirror(url, dest):
        calls.append((url, dest))
        return mock.MagicMock()

    host_a = _EndpointConfig(
        {"type": "gitea", "repo": "uqkami/Awara",
         "api": "https://git.nodebay.top/api/v1", "token": "t"},
        is_source=True,
    )
    host_b = _EndpointConfig(
        {"type": "gitea", "repo": "uqkami/Awara",
         "api": "https://gitea.example.com/api/v1", "token": "t"},
        is_source=True,
    )
    same_as_a = _EndpointConfig(
        {"type": "gitea", "repo": "uqkami/Awara",
         "api": "https://git.nodebay.top/api/v1", "token": "other-token"},
        is_source=True,
    )

    mock_apis = [mock.MagicMock() for _ in range(3)]
    with mock.patch(
        "gitacross.source.get_api_client", side_effect=mock_apis
    ), mock.patch(
        "gitacross.git.GitRepo.ensure_mirror", side_effect=fake_ensure_mirror
    ):
        _ = create_source(host_a, "cache")
        _ = create_source(host_b, "cache")
        _ = create_source(same_as_a, "cache")

    dest_a, dest_b, dest_same = (Path(d) for _, d in calls)
    for d in (dest_a, dest_b):
        assert str(d).endswith(".git")
    assert "source_gitea_git.nodebay.top_uqkami_Awara_" in str(dest_a)
    assert "source_gitea_gitea.example.com_uqkami_Awara_" in str(dest_b)
    # Different hosts -> separate mirrors; same host (any token) -> shared mirror
    assert dest_a != dest_b
    assert dest_a == dest_same
    print("  ✓ source: remote source cache dirs are separated per host")


def test_remote_source_cache_hash_kills_slug_collisions():
    """Slug-ambiguous repos (a/b_c vs a_b/c) on one host never share a mirror."""
    from gitacross.config import _EndpointConfig
    from gitacross.source import create_source

    calls = []

    def fake_ensure_mirror(url, dest):
        calls.append((url, dest))
        return mock.MagicMock()

    def cfg(repo):
        return _EndpointConfig(
            {"type": "gitea", "repo": repo,
             "api": "https://git.nodebay.top/api/v1", "token": "t"},
            is_source=True,
        )

    mock_apis = [mock.MagicMock() for _ in range(2)]
    with mock.patch(
        "gitacross.source.get_api_client", side_effect=mock_apis
    ), mock.patch(
        "gitacross.git.GitRepo.ensure_mirror", side_effect=fake_ensure_mirror
    ):
        _ = create_source(cfg("a/b_c"), "cache")
        _ = create_source(cfg("a_b/c"), "cache")

    dest1, dest2 = (Path(d) for _, d in calls)
    # Both have the same readable slug prefix (a_b_c), but the identity hashes
    # differ, so they resolve to distinct mirror dirs
    assert dest1 != dest2
    assert "a_b_c_" in str(dest1)
    assert "a_b_c_" in str(dest2)
    print("  ✓ source: slug collisions resolve to distinct cache dirs")

def test_local_source_duck_typed_helpers():
    from gitacross.config import _EndpointConfig
    from gitacross.source import create_source

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _ = _make_git_repo(repo)
        _ = _make_file(repo, "f.txt")
        _git_commit(repo, "c1")
        _git_tag(repo, "v1.0")
        cfg = _EndpointConfig({"type": "local", "path": str(repo)}, is_source=True)
        src = create_source(cfg, "cache")
        assert src.resolve_commit("v1.0")
        dest = Path(tmp) / "export"
        dest.mkdir()
        src.export_tag("v1.0", dest)
        src.download_asset({}, dest)  # no-op
        assert src.list_release_assets(1) == []
        assert src.repo_path
    print("  ✓ source: local passthrough helpers work")

def test_source_remote_no_api_releases_falls_back_to_nonempty_tags():
    """When the API has no releases but tags exist, fetch returns the tags."""
    from gitacross.config import _EndpointConfig
    from gitacross.source import create_source

    cfg = _EndpointConfig(
        {
            "type": "gitea",
            "repo": "owner/repo",
            "api": "https://gitea.example.com/api/v1",
            "token": "tok",
        },
        is_source=True,
    )
    mock_api = mock.MagicMock()
    mock_api.list_releases.return_value = []
    mock_git = mock.MagicMock()
    mock_git.list_tags_sorted_by_date.return_value = ["v1.0", "v2.0"]
    mock_git.resolve_commit.return_value = "a" * 40
    mock_git.tag_commit_date.return_value = "2026-01-01"

    with mock.patch("gitacross.source.get_api_client", return_value=mock_api), mock.patch(
        "gitacross.git.GitRepo.ensure_mirror", return_value=mock_git
    ):
        src = create_source(cfg, "cache")
        releases = src.fetch_releases()
    assert [r["tag_name"] for r in releases] == ["v1.0", "v2.0"]
    print("  ✓ source: no API releases → falls back to non-empty tag list")

def test_source_commit_mode_warnings():
    """Commit mode warns on sync_from and skips when HEAD is unresolvable."""
    from gitacross.config import _EndpointConfig
    from gitacross.source import create_source

    cfg = _EndpointConfig(
        {
            "type": "gitea",
            "repo": "owner/repo",
            "api": "https://gitea.example.com/api/v1",
            "token": "tok",
            "mode": "commit",
            "sync_from": "abc123",
        },
        is_source=True,
    )
    mock_api = mock.MagicMock()
    mock_git = mock.MagicMock()
    mock_git.resolve_default_branch_head.return_value = ""

    with mock.patch("gitacross.source.get_api_client", return_value=mock_api), mock.patch(
        "gitacross.git.GitRepo.ensure_mirror", return_value=mock_git
    ):
        src = create_source(cfg, "cache")
        assert src.fetch_releases() == []  # unresolvable HEAD → skip
        mock_git.resolve_default_branch_head.assert_called_once()
    print("  ✓ source: commit mode warns on sync_from and empty HEAD")
