"""Source endpoint factory — fetches releases from any configured source type.

Supported types: gitea, github, local.
"""
from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import final

from .git import GitRepo
from .providers import ENDPOINT_TYPES, REMOTE_TYPES, get_api_client

logger = logging.getLogger(__name__)


def create_source(config, cache_dir, dry_run=False):
    """Factory: build a Source endpoint from the config."""
    t = config.type
    if t == "local":
        return _LocalSource(config)
    if t in REMOTE_TYPES:
        if config.mode not in ("release", "tag", "commit"):
            raise ValueError(
                f"Unknown source mode '{config.mode}' — expected 'release', 'tag', or 'commit'"
            )
        return _RemoteSource(config, cache_dir, dry_run=dry_run)
    raise ValueError(
        f"Unknown source type: {t} — expected one of {sorted(ENDPOINT_TYPES)}"
    )


def _filter_from_sync_point(releases, sync_from):
    """Filter to releases at or after the sync_from tag (inclusive).

    Releases must already be sorted oldest-first. If sync_from is empty
    return all releases. If the tag isn't found, return nothing —
    better to skip than silently backfill history.
    """
    if not sync_from:
        return releases
    for i, rel in enumerate(releases):
        if rel["tag_name"] == sync_from:
            return releases[i:]
    logger.warning("sync_from tag '%s' not found — syncing nothing", sync_from)
    return []


# ---------------------------------------------------------------------------
# Source interface (duck-typed, no ABC to keep imports light)
# Required: fetch_releases(), export_tag(tag, dest)
# ---------------------------------------------------------------------------


@final
class _RemoteSource:
    """Source backed by a remote API (Gitea/GitHub) + a bare git mirror."""

    def __init__(self, config, cache_dir, dry_run=False):
        self._repo = config.repo
        self._type = config.type
        self._api = get_api_client(config.type, config.api, config.repo, config.token)
        self._api.ensure_repo_exists(create=not dry_run)
        self._git = GitRepo.ensure_mirror(
            config.clone_url,
            Path(cache_dir) / f"source_{config.type}_{config.repo_slug}.git",
        )
        self._include_prereleases = config.include_prereleases
        self._include_drafts = config.include_drafts
        self._sync_from = config.sync_from
        self._mode = config.mode
        # Branch used to resolve HEAD in commit mode (empty = auto-detect)
        self._branch = getattr(config, "branch", "")

    def fetch_releases(self):
        """Return the release list for the configured source mode.

        mode=release (default): paginated API releases, filtered, sorted
        oldest-first. Falls back to git tags only when the API has no
        releases at all.
        mode=tag: git tags from the mirror treated as releases.
        mode=commit: single snapshot of the current branch HEAD, gated on
        sync_from ancestry.
        """
        if self._mode == "commit":
            logger.info("Source mode 'commit' — syncing latest branch HEAD")
            return self._fetch_commit_snapshot()

        if self._mode == "tag":
            logger.info("Source mode 'tag' — reading releases from git tags")
            return self._fetch_tags_as_releases()

        releases = []
        page = 1
        while True:
            batch = self._api.list_releases(page=page)
            if not batch:
                break
            releases.extend(batch)
            page += 1

        # No API releases — fall back to git tags from the mirror.
        # This handles repos where tags were pushed without creating releases.
        if not releases:
            logger.info("No API releases found — falling back to git tags")
            tags = self._fetch_tags_as_releases()
            if not tags:
                logger.warning(
                    "No API releases or git tags found for %s repo '%s'. "
                    + "If this repository does not use releases/tags, set 'mode: commit' under 'source:' in your config.",
                    self._type,
                    self._repo,
                )
            return tags

        result = []
        for rel in releases:
            if not self._include_prereleases and rel.get("prerelease"):
                continue
            if not self._include_drafts and rel.get("draft"):
                continue
            result.append(rel)

        result.sort(
            key=lambda r: r.get("source_date")
            or r.get("published_at")
            or r.get("created_at")
            or ""
        )

        for rel in result:
            rel["commit_sha"] = self._git.resolve_commit(rel.get("tag_name", ""))
            rel["source_date"] = rel.get("published_at") or rel.get("created_at") or ""

        # Release mode: sync_from must be an API release. If it's missing from
        # the filtered list, explain why and sync nothing — the user can switch
        # this source to mode: tag when the tag only exists in git.
        if self._sync_from and not any(
            r["tag_name"] == self._sync_from for r in result
        ):
            if any(r["tag_name"] == self._sync_from for r in releases):
                logger.warning(
                    "sync_from tag '%s' is a release but was filtered out (prerelease/draft). "
                    + "set include_prereleases/include_drafts to include it; syncing nothing for now.",
                    self._sync_from,
                )
            elif self._git.tag_exists(self._sync_from):
                logger.warning(
                    "sync_from tag '%s' has no API release (git tag only). "
                    + "set source mode: tag to sync from git tags; syncing nothing for now.",
                    self._sync_from,
                )
            else:
                return _filter_from_sync_point(result, self._sync_from)
            return []

        return _filter_from_sync_point(result, self._sync_from)

    def _fetch_tags_as_releases(self):
        """Read git tags from the local mirror (sorted oldest-first) and treat them as releases.

        Uses only ``list_tags_sorted_by_date`` — the unsorted ``list_tags``
        call is unnecessary because both return the same tag set.
        """
        sorted_tags = self._git.list_tags_sorted_by_date()
        if not sorted_tags and self._mode == "tag":
            logger.warning(
                "No git tags found for %s repo '%s'. "
                + "If this repository does not use tags, set 'mode: commit' under 'source:' in your config.",
                self._type,
                self._repo,
            )
        result = []
        for tag in sorted_tags:
            commit_sha = self._git.resolve_commit(tag)
            result.append({
                "id": tag,
                "tag_name": tag,
                "commit_sha": commit_sha,
                "body": "",
                "prerelease": False,
                "draft": False,
                "source_date": self._git.tag_commit_date(tag),
            })
        return _filter_from_sync_point(result, self._sync_from)

    def _fetch_commit_snapshot(self):
        """Return a single synthetic release for the current branch HEAD.

        Returns an empty list when the HEAD SHA cannot be resolved.
        State's SHA key handles idempotency — already-synced commits are
        skipped by the caller.

        The synthetic release has ``tag_name=None`` to tell the sync loop
        that this is a raw commit snapshot — no tag or API release should be
        created on the target.
        """
        if self._sync_from:
            logger.warning(
                "sync_from is set but has no effect in commit mode — "
                + "state already prevents re-syncing the same SHA. "
                + "Remove sync_from from your config to suppress this warning."
            )

        branch = self._branch or None  # pass None to trigger auto-detect
        head_sha = self._git.resolve_default_branch_head(branch)
        if not head_sha:
            logger.warning(
                "Could not resolve branch HEAD in commit mode — skipping"
            )
            return []

        commit_date = self._git.tag_commit_date(head_sha)
        logger.info(
            "Commit mode: HEAD is %s (%s)",
            head_sha[:12],
            commit_date or "unknown date",
        )
        return [{
            "id": head_sha,
            # tag_name=None signals 'no tag' to the sync loop
            "tag_name": None,
            "commit_sha": head_sha,
            "body": "",
            "prerelease": False,
            "draft": False,
            "source_date": commit_date,
        }]

    def resolve_commit(self, ref):
        return self._git.resolve_commit(ref)

    def export_tag(self, tag, dest):
        self._git.export_tag(tag, dest)

    def export_release(self, rel, dest):
        ref = rel.get("commit_sha") or rel.get("tag_name", "")
        self._git.export_commit(ref, dest)

    def download_asset(self, asset, dest):
        self._api.download_asset(asset, dest)

    def list_release_assets(self, release_id):
        return self._api.list_release_assets(release_id)

    @property
    def repo_path(self):
        return self._git.git_dir


@final
class _LocalSource:
    """Source backed by a local git repository — tags are 'releases'."""

    def __init__(self, config):
        self._git = GitRepo.local(config.path)
        self._tag_pattern = config.tag_pattern
        self._sync_from = config.sync_from

    def fetch_releases(self):
        # list_tags_sorted_by_date returns the complete tag set, already ordered
        # oldest-first — no need to call list_tags() separately for filtering.
        sorted_tags = self._git.list_tags_sorted_by_date()
        # Apply tag pattern filter directly (avoids the O(n²) list-in-list check)
        ordered = [t for t in sorted_tags if fnmatch.fnmatch(t, self._tag_pattern)]
        # Local tags have no prerelease/draft/body metadata.
        result = []
        for tag in ordered:
            commit_sha = self._git.resolve_commit(tag)
            result.append(
                {
                    "id": tag,  # tag name doubles as release ID
                    "tag_name": tag,
                    "commit_sha": commit_sha,
                    "body": "",
                    "prerelease": False,
                    "draft": False,
                    "source_date": self._git.tag_commit_date(tag),
                }
            )
        return _filter_from_sync_point(result, self._sync_from)

    def resolve_commit(self, ref):
        return self._git.resolve_commit(ref)

    def export_tag(self, tag, dest):
        self._git.export_tag(tag, dest)

    def export_release(self, rel, dest):
        ref = rel.get("commit_sha") or rel.get("tag_name", "")
        self._git.export_commit(ref, dest)

    def download_asset(self, _asset, _dest):
        pass

    def list_release_assets(self, _release_id):
        return []

    @property
    def repo_path(self):
        return self._git.git_dir
