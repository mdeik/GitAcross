"""Source endpoint factory — fetches releases from any configured source type.

Supported types: gitea, github, local.
"""
from __future__ import annotations

import fnmatch
import logging
import re
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


# Calendar versions start with a 4-digit year (YYYY[.MM[.DD[...]]], e.g.
# 2024.05.01 or 2024-05-01). Every dot/dash/underscore-separated numeric
# field is part of the date sequence.
_CALENDAR_RE = re.compile(r"[^0-9]*(\d{4}(?:[._-]\d{1,4})*)")
# SemVer-style cores are dot-separated numbers; a hyphen introduces a
# prerelease, never another core field.
_SEMVER_CORE_RE = re.compile(r"(\d+(?:\.\d+)*)")


def _version_key(tag):
    """Parse a version-like tag into a comparable ``(fields, prerelease)`` key.

    Two schemes are supported, parsed separately because they are semantically
    different:

    * **SemVer** — ``v1.2.3``, ``v1.2.3-rc.1``. Dot-separated core; a hyphen
      starts a prerelease that sorts before the final release.
    * **Calendar (CalVer)** — ``2024.05.01``, ``2024-05-01``, ``2024.05``.
      Date-based: a leading 4-digit year marks it, and every numeric field
      (dot *or* dash separated) belongs to the (year, month, day, ...)
      sequence, compared as plain numbers. No major/minor/patch or SemVer
      prerelease semantics apply to the date fields themselves.

    Returns ``None`` when the tag carries no parseable version.
    """
    if not isinstance(tag, str):
        return None
    calendar = _CALENDAR_RE.match(tag)
    if calendar:
        fields = tuple(int(part) for part in re.split(r"[._-]", calendar.group(1)))
        rest = tag[calendar.end():].split("+", 1)[0]
    else:
        match = _SEMVER_CORE_RE.search(tag)
        if not match:
            return None
        fields = tuple(int(part) for part in match.group(1).split("."))
        rest = tag[match.end():].split("+", 1)[0]
    # Anything remaining after the version fields is a prerelease; build
    # metadata ("+...") is ignored for ordering, per SemVer.
    ids = tuple(re.findall(r"[0-9A-Za-z]+", rest))
    return fields, (ids or None)


def _cmp_version_key(a, b):
    """Compare two ``_version_key`` results: -1/0/1 (SemVer-ish ordering)."""
    (ca, pa), (cb, pb) = a, b
    width = max(len(ca), len(cb))
    ca += (0,) * (width - len(ca))
    cb += (0,) * (width - len(cb))
    if ca != cb:
        return 1 if ca > cb else -1
    # Same core: a final release outranks any prerelease of it
    if pa is None and pb is None:
        return 0
    if pa is None:
        return 1
    if pb is None:
        return -1
    # Compare prerelease identifiers: numeric < alphanumeric, identifiers
    # compare numerically when both numeric, lexically when both alphanumeric
    for x, y in zip(pa, pb):
        xn, yn = x.isdigit(), y.isdigit()
        if xn and yn:
            xi, yi = int(x), int(y)
            if xi != yi:
                return 1 if xi > yi else -1
        elif xn != yn:
            return -1 if xn else 1
        elif x != y:
            return 1 if x > y else -1
    return 1 if len(pa) > len(pb) else (-1 if len(pa) < len(pb) else 0)


def _newer_than_version(tag, cutoff):
    """True when *tag* parses as a version strictly newer than *cutoff*."""
    key = _version_key(tag)
    return key is not None and _cmp_version_key(key, cutoff) > 0


def _filter_from_sync_point(releases, sync_from):
    """Filter to releases at or after the sync_from tag.

    Releases must already be sorted oldest-first. If sync_from is empty return
    all releases. When the exact tag is present, everything from it onwards is
    included; when it is missing (renamed, deleted, or never released), releases
    whose version is strictly newer than sync_from are synced instead. Tags that
    cannot be compared (no version digits) are skipped rather than silently
    backfilling history.
    """
    if not sync_from:
        return releases
    for i, rel in enumerate(releases):
        if rel["tag_name"] == sync_from:
            return releases[i:]

    # Exact tag not found — fall back to releases with a newer version.
    cutoff = _version_key(sync_from)
    if cutoff is None:
        logger.warning(
            "sync_from '%s' not found and is not version-like — syncing nothing",
            sync_from,
        )
        return []
    newer = [r for r in releases if _newer_than_version(r.get("tag_name"), cutoff)]
    if not newer:
        logger.warning(
            "sync_from tag '%s' not found and no releases are newer — syncing nothing",
            sync_from,
        )
    else:
        logger.info(
            "sync_from tag '%s' not found — syncing %d release(s) newer than it",
            sync_from,
            len(newer),
        )
    return newer


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
            Path(cache_dir) / config.mirror_dir_name("source"),
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
