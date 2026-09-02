#!/usr/bin/env python3
"""GitAcross – mirror releases from any configured source to any target.

This module holds the sync engine (:func:`sync_project`, :func:`run`); the
``gitacross`` command-line entry point lives in :mod:`gitacross.cli`.
"""
from __future__ import annotations

import fnmatch
import logging
import shutil
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from .config import Config, ProjectConfig
from .renderer import apply_operations
from .retry import retry
from .source import create_source
from .state import State
from .target import create_target

logger = logging.getLogger(__name__)

# Default directory for state.yml and cache/ — used by run(), sync_project(),
# and the CLI --workdir flag so they can't drift apart.
DEFAULT_WORK_DIR = ".gitsync"



def _clean_text(text):
    """Normalize text by converting escaped newlines (\\r\\n, \\n) into actual linebreaks."""
    if not text or not isinstance(text, str):
        return text or ""
    if r"\n" in text or r"\r\n" in text:
        return text.replace(r"\r\n", "\n").replace(r"\n", "\n")
    return text


class _SafeFormatter:
    """Minimal mapping for ``format_map`` — unknown keys render literally as ``{key}``."""

    def __init__(self, **values: str):
        self._values: dict[str, str] = values

    def __getitem__(self, key):
        return self._values.get(key, "{" + key + "}")


def _render_template(template: str, context: Mapping[str, Any]) -> str:
    if not template:
        return ""
    try:
        return template.format_map(_SafeFormatter(**context))
    except (IndexError, KeyError, TypeError, ValueError):
        return template


def _release_context(rel, source, project_config):
    """Build the per-release values shared by the dry-run and real sync loops.

    Returns ``(tag, commit_mode, source_commit, short_sha, source_date,
    raw_body, context)``. ``commit_mode`` is ``True`` when the release has no
    tag (a raw commit snapshot).
    """
    tag = rel["tag_name"]
    commit_mode = tag is None
    source_commit = rel.get("commit_sha") or ""
    if not source_commit and not commit_mode and hasattr(source, "resolve_commit"):
        source_commit = source.resolve_commit(tag)
    short_sha = str(source_commit or (rel.get("commit_sha") or ""))[:12]
    source_date = rel.get("source_date") or rel.get("published_at") or ""
    raw_body = _clean_text(rel.get("body", ""))
    release_name = _clean_text(rel.get("name") or tag or "")
    context = {
        "tag": tag or "",
        "commit_sha": source_commit,
        "short_sha": short_sha,
        "project_name": project_config.name,
        "name": project_config.name,
        "source_date": source_date,
        "body": raw_body,
        "description": raw_body,
        "release_name": release_name,
    }
    return tag, commit_mode, source_commit, short_sha, source_date, raw_body, context


def _matches_asset_filter(asset_name, filter_spec):
    """Check if an asset name matches the filter specification."""
    if not filter_spec:
        return False
    if filter_spec is True:
        return True
    if isinstance(filter_spec, str):
        return fnmatch.fnmatch(asset_name, filter_spec)
    if isinstance(filter_spec, (list, tuple, set)):
        return any(fnmatch.fnmatch(asset_name, pat) for pat in filter_spec)
    return False


def _sync_release_assets(
    source,
    target,
    rel,
    tag,
    target_release,
    sync_assets,
    tmpdir,
    retry_max=3,
    retry_backoff=2,
    dry_run=False,
    stream_assets=False,
):
    """Sync release assets (prebuilts/files) from source to target."""
    assets = rel.get("assets")
    if assets is None and hasattr(source, "list_release_assets"):
        assets = source.list_release_assets(rel.get("id") or tag)
    assets = assets or []

    matching = [
        a for a in assets if _matches_asset_filter(a.get("name", ""), sync_assets)
    ]
    if not matching:
        logger.debug("No matching release assets to sync for %s", tag)
        return

    logger.info("Found %d matching asset(s) to sync for release %s", len(matching), tag)

    for asset in matching:
        name = asset.get("name")
        size = asset.get("size", 0)
        if dry_run:
            logger.info(
                "[DRY-RUN] Would sync release asset: %s (%s bytes)", name, size
            )
            continue

        asset_dir = tmpdir / "assets"
        asset_dir.mkdir(parents=True, exist_ok=True)
        asset_path = asset_dir / name

        logger.info("Downloading asset %s (%s bytes) from source", name, size)
        _ = retry(
            lambda a=asset, p=asset_path: source.download_asset(a, p),
            max_attempts=retry_max,
            backoff_seconds=retry_backoff,
        )

        logger.info(
            "Uploading asset %s to target release %s%s",
            name, tag, " (streaming)" if stream_assets else "",
        )
        target.upload_release_asset(
            tag, asset_path, name=name, release=target_release, stream=stream_assets
        )


def sync_project(
    project_config: ProjectConfig,
    work_dir: str | Path = DEFAULT_WORK_DIR,
    dry_run: bool = False,
    _state: State | None = None,
) -> list[dict[str, str | None]]:
    """Sync all new releases for a single project from source to target.

    This is the core sync primitive. For most use-cases, prefer the higher-level
    :func:`run` which loads config and state automatically.

    Respects ``project_config.enabled``: if the project is disabled in config this
    function logs a message and returns immediately without syncing anything,
    matching the behaviour of the CLI.

    Releases are processed and returned in chronological order (oldest first,
    newest last).

    Args:
        project_config: A :class:`~gitacross.ProjectConfig` instance (from
            ``Config(config_source).projects``).
        work_dir: Directory for ``state.yml`` and ``cache/`` (default
            ``.gitsync``).
        dry_run: When ``True``, log what *would* happen but make no changes
            to the target repository, the state file, or ``work_dir`` (mirror
            clones go to a temporary cache dir instead).
        _state: Internal — a shared :class:`~gitacross.State` for callers that
            sync multiple projects in one run (avoids re-reading ``state.yml``
            once per project). Normally ``None``.

    Returns:
        A list of dicts for the releases synced (or that would be synced if
        ``dry_run`` is ``True``), ordered chronologically from oldest to newest:

        * ``"tag"`` (``str | None``) — release tag name (``None`` in commit mode).
        * ``"source_commit"`` (``str``) — commit SHA from the source repository.
        * ``"target_commit"`` (``str | None``) — commit SHA created on the target
          repository (``None`` in dry-run mode).
        * ``"source_date"`` (``str``) — ISO timestamp or date of the source release/commit.
    """
    if not project_config.enabled:
        logger.info("Skipping disabled project: %s", project_config.name)
        return []

    logger.info(
        "Syncing project: %s  (%s → %s)",
        project_config.name,
        project_config.source.type,
        project_config.target.type,
    )

    work_dir = Path(work_dir)
    state = _state if _state is not None else State(work_dir)

    # Dry-run must not write to work_dir, so mirror clones go into a temporary
    # cache dir that is removed when the dry-run finishes.
    temp_cache_dir: Path | None = None
    if dry_run:
        temp_cache_dir = Path(tempfile.mkdtemp(prefix="gitsync-cache-"))
        cache_dir = temp_cache_dir
    else:
        cache_dir = work_dir / "cache"

    try:
        source = create_source(project_config.source, cache_dir, dry_run=dry_run)

        # Dry-run never commits/tags/pushes, so the target is not built at all:
        # building it would clone a mirror into the cache dir and may create the
        # repo on the target platform via ensure_repo_exists().
        if dry_run:
            target = None
        else:
            target = create_target(
                project_config.target,
                cache_dir,
                retry_max=project_config.retry.max_attempts,
                retry_backoff=project_config.retry.backoff_seconds,
                author=project_config.renderer.author,
            )

        # Fetch releases from source (chronologically ordered oldest-first)
        all_releases = source.fetch_releases()
        new_releases = [
            r for r in all_releases
            if not state.has_release(
                project_config.name,
                # Commit mode: key by SHA; release/tag mode: key by tag name
                r["commit_sha"] if r.get("tag_name") is None else r["tag_name"],
            )
        ]

        if not new_releases:
            logger.info("No new releases to sync")
            return []

        logger.info("Found %d new release(s)", len(new_releases))

        # Setup target branch (no-op in dry-run: there is no target)
        if target is not None:
            target.setup(project_config.target.branch)

        synced_releases = []

        if dry_run:
            for rel in new_releases:
                (
                    tag,
                    commit_mode,
                    source_commit,
                    short_sha,
                    source_date,
                    raw_body,
                    context,
                ) = _release_context(rel, source, project_config)

                tmpdir = Path(tempfile.mkdtemp(prefix="gitsync-"))
                try:
                    if hasattr(source, "export_release"):
                        source.export_release(rel, tmpdir)
                    else:
                        source.export_tag(tag, tmpdir)
                    apply_operations(tmpdir, project_config)
                    files = sorted(tmpdir.rglob("*"))
                    if project_config.commit_message:
                        dry_msg = _render_template(project_config.commit_message, context)
                    else:
                        dry_msg = f"commit {short_sha}" if commit_mode else f"Release {tag}"
                    logger.info(
                        "[DRY-RUN] Would commit and push: %s (%d files)",
                        dry_msg,
                        len(files),
                    )
                    if project_config.sync_assets and not commit_mode:
                        _sync_release_assets(
                            source=source,
                            target=target,
                            rel=rel,
                            tag=tag,
                            target_release=None,
                            sync_assets=project_config.sync_assets,
                            tmpdir=tmpdir,
                            retry_max=project_config.retry.max_attempts,
                            retry_backoff=project_config.retry.backoff_seconds,
                            dry_run=True,
                            stream_assets=project_config.stream_assets,
                        )
                    synced_releases.append({
                        "tag": tag,
                        "source_commit": source_commit,
                        "target_commit": None,
                        "source_date": source_date,
                    })
                finally:
                    shutil.rmtree(tmpdir, ignore_errors=True)
            return synced_releases

        # Dry-run returned above; from here on the target always exists.
        assert target is not None
        for rel in new_releases:
            (
                tag,
                commit_mode,
                source_commit,
                short_sha,
                source_date,
                raw_body,
                context,
            ) = _release_context(rel, source, project_config)
            state_key = rel["commit_sha"] if commit_mode else tag

            if commit_mode:
                logger.info("Processing commit snapshot: %s", short_sha)
            else:
                logger.info("Processing release: %s", tag)

            tmpdir = Path(tempfile.mkdtemp(prefix="gitsync-"))
            try:
                # Export source release
                if hasattr(source, "export_release"):
                    source.export_release(rel, tmpdir)
                else:
                    source.export_tag(tag, tmpdir)
                logger.debug(
                    "Exported %s to work directory",
                    f"commit {short_sha}" if commit_mode else f"release {tag}",
                )

                # Apply render pipeline
                apply_operations(tmpdir, project_config)

                # Commit to target (linear history on target branch)
                # Use the source release/commit date so commits appear chronologically
                if project_config.commit_message:
                    commit_message = _render_template(project_config.commit_message, context)
                else:
                    commit_message = (
                        f"Sync commit {short_sha}" if commit_mode else f"Release {tag}"
                    )
                _ = target.commit(tmpdir, commit_message, date=source_date)

                target_release = None
                if not commit_mode:
                    # Annotated tag
                    target.tag(tag, f"Release {tag}")

                    # Push (no-op for local targets)
                    target.push(project_config.target.branch, tag)

                    # Create release on target platform (no-op for local targets)
                    if project_config.release_description is not None:
                        release_body = _render_template(project_config.release_description, context)
                    elif project_config.preserve_description:
                        release_body = raw_body
                    else:
                        release_body = ""
                    target_release = target.create_release(
                        tag=tag,
                        name=context["release_name"],
                        body=release_body,
                        prerelease=bool(rel.get("prerelease", False)),
                    )
                else:
                    # Commit mode: push the branch only (no tag, no release)
                    target.push(project_config.target.branch)

                # Sync release assets/packages (prebuilts)
                if project_config.sync_assets and not commit_mode:
                    _sync_release_assets(
                        source=source,
                        target=target,
                        rel=rel,
                        tag=tag,
                        target_release=target_release,
                        sync_assets=project_config.sync_assets,
                        tmpdir=tmpdir,
                        retry_max=project_config.retry.max_attempts,
                        retry_backoff=project_config.retry.backoff_seconds,
                        dry_run=False,
                        stream_assets=project_config.stream_assets,
                    )

                # Persist state (commit mode: keyed by SHA; release/tag: by tag name)
                target_commit_sha = cast(str, target.head_sha())

                state.add_release(
                    project_config.name,
                    state_key,
                    {
                        "tag": tag,
                        "source_commit": source_commit,
                        "target_commit": target_commit_sha,
                        "source_date": source_date,
                        "sync_date": datetime.now().astimezone().isoformat(),
                    },
                )
                state.save()

                synced_releases.append({
                    "tag": tag,
                    "source_commit": source_commit,
                    "target_commit": target_commit_sha,
                    "source_date": source_date,
                })

                logger.info(
                    "Synced %s (target commit %s)",
                    f"commit {short_sha}" if commit_mode else f"release {tag}",
                    target_commit_sha,
                )

            except Exception:
                logger.exception("Failed to sync release %s", tag)
                raise
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        return synced_releases
    finally:
        if temp_cache_dir is not None:
            shutil.rmtree(temp_cache_dir, ignore_errors=True)


def run(
    config,
    project_name: str | None = None,
    dry_run: bool = False,
    reset: bool = False,
    work_dir: str | Path = DEFAULT_WORK_DIR,
):
    """Sync releases from a config — the primary Python API entry point.

    Loads configuration and state, then syncs every enabled project (or just
    the one named by *project_name*). Unlike :func:`main`, this function raises
    exceptions instead of calling ``sys.exit`` and returns a summary dict for
    each project that was processed.

    Args:
        config:     The configuration to sync from — a :class:`~gitacross.Config`
            instance, a path to a YAML config file (``str`` or
            :class:`~pathlib.Path`), or an already-open file-like object
            (e.g. ``io.StringIO`` holding YAML or an ``open()`` handle).
            For YAML held in a variable, pass
            ``Config.from_yaml_string(content)``.
        project_name: Optional project name to sync. When ``None`` all enabled
            projects in the config are synced.
        dry_run:     When ``True``, log what *would* happen but make no changes
            to the target repository, the state file, or ``work_dir``.
        reset:       When ``True``, delete ``work_dir`` (state + cache) before
            syncing so that all releases are treated as new.
        work_dir:    Directory path for ``state.yml`` and ``cache/``.
            Defaults to ``.gitsync``.

    Returns:
        A list of dicts — one per enabled project that was attempted — with
        the following keys:

        * ``"project"`` (`str`) — project name.
        * ``"synced"`` (`bool`) — ``True`` when :func:`sync_project` completed
          without raising, ``False`` otherwise.
        * ``"releases_synced"`` (`int`) — count of releases synced (or would be
          synced in dry-run mode). Equal to ``len(releases)``.
        * ``"releases"`` (`list[dict]`) — list of release detail dicts in
          chronological order (oldest first, newest last):
          ``[{"tag": str|None, "source_commit": str, "target_commit": str|None, "source_date": str}, ...]``
        * ``"error"`` (`str | None`) — ``None`` on success; ``str(exc)`` on
          failure so callers can inspect the error without log capture.

        Disabled projects are silently omitted from the list.

    Raises:
        FileNotFoundError: If *config* is a path that does not exist.
        ValueError:        If *project_name* is specified but not found in the config.
        yaml.YAMLError:    If the config contains invalid YAML.

    Example::

        import gitacross

        results = gitacross.run("config.yml", project_name="my-mirror", dry_run=True, work_dir=".gitsync")
        for r in results:
            latest = r["releases"][-1] if r["releases"] else None
            print(r["project"], f"synced {r['releases_synced']} releases", latest)
    """
    if not isinstance(config, Config):
        # Path or open stream — Config() decides which.
        config = Config(config)

    work_dir = Path(work_dir)

    if reset and work_dir.exists():
        if dry_run:
            logger.info("[DRY-RUN] Would clear %s/ (state + cache)", work_dir)
        else:
            shutil.rmtree(work_dir)
            logger.info("Cleared %s/ (state + cache)", work_dir)

    if not dry_run:
        work_dir.mkdir(parents=True, exist_ok=True)
    state = State(work_dir)

    projects = [p for p in config.projects if not project_name or p.name == project_name]
    if project_name and not projects:
        raise ValueError(f"Project '{project_name}' not found in config")

    results = []
    for proj in projects:
        if not proj.enabled:
            # sync_project also checks enabled, but we skip here so disabled
            # projects are not included in the returned results list.
            continue
        try:
            synced_releases = sync_project(
                proj, work_dir=work_dir, dry_run=dry_run, _state=state
            )
            results.append({
                "project": proj.name,
                "synced": True,
                "releases_synced": len(synced_releases),
                "releases": synced_releases,
                "error": None,
            })
        except Exception as exc:
            logger.exception("Project %s failed", proj.name)
            results.append({
                "project": proj.name,
                "synced": False,
                "releases_synced": 0,
                "releases": [],
                "error": str(exc),
            })

    return results




# Imported at the bottom to avoid a circular import (cli.py imports .main.run).
from .cli import main  # noqa: I001


if __name__ == "__main__":
    main()
