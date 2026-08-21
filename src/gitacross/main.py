#!/usr/bin/env python3
"""GitAcross – mirror releases from any configured source to any target.

Usage:
    gitacross --config config.yml [--project NAME] [--dry-run]
    python -m gitacross --config config.yml [--project NAME] [--dry-run]
"""

import argparse
from datetime import datetime
import fnmatch
import logging
import shutil
import sys
import tempfile
from pathlib import Path

from .config import Config
from .renderer import apply_operations
from .retry import retry
from .source import create_source
from .state import State
from .target import create_target

logger = logging.getLogger(__name__)


def _setup_logging(verbose):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def _clean_text(text):
    """Normalize text by converting escaped newlines (\\r\\n, \\n) into actual linebreaks."""
    if not text or not isinstance(text, str):
        return text or ""
    if r"\n" in text or r"\r\n" in text:
        return text.replace(r"\r\n", "\n").replace(r"\n", "\n")
    return text


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
        retry(
            lambda: source.download_asset(asset, asset_path),
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


def sync_project(project, state, dry_run=False):
    """Sync all new releases from project.source to project.target."""
    logger.info(
        "Syncing project: %s  (%s → %s)",
        project.name,
        project.source.type,
        project.target.type,
    )

    cache_dir = Path(".gitsync") / "cache"
    source = create_source(project.source, cache_dir)
    target = create_target(
        project.target,
        cache_dir,
        retry_max=project.retry.max_attempts,
        retry_backoff=project.retry.backoff_seconds,
        author=project.renderer.author,
    )

    # Fetch releases from source
    all_releases = source.fetch_releases()
    new_releases = [
        r for r in all_releases
        if not state.has_release(
            project.name,
            # Commit mode: key by SHA; release/tag mode: key by tag name
            r["commit_sha"] if r.get("tag_name") is None else r["tag_name"],
        )
    ]

    if not new_releases:
        logger.info("No new releases to sync")
        return

    logger.info("Found %d new release(s)", len(new_releases))

    # Setup target branch
    target.setup(project.target.branch)

    for rel in new_releases:
        tag = rel["tag_name"]
        commit_mode = tag is None  # commit mode: no tag, keyed by SHA
        state_key = rel["commit_sha"] if commit_mode else tag
        short_sha = (rel.get("commit_sha") or "")[:12]

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
            apply_operations(tmpdir, project)

            if dry_run:
                files = sorted(tmpdir.rglob("*"))
                logger.info(
                    "[DRY-RUN] Would commit and push: %s (%d files)",
                    f"commit {short_sha}" if commit_mode else f"Release {tag}",
                    len(files),
                )
                if project.sync_assets:
                    _sync_release_assets(
                        source=source,
                        target=target,
                        rel=rel,
                        tag=tag,
                        target_release=None,
                        sync_assets=project.sync_assets,
                        tmpdir=tmpdir,
                        retry_max=project.retry.max_attempts,
                        retry_backoff=project.retry.backoff_seconds,
                        dry_run=True,
                        stream_assets=project.stream_assets,
                    )
                continue

            # Commit to target (linear history on target branch)
            # Use the source release/commit date so commits appear chronologically
            source_date = rel.get("source_date") or rel.get("published_at") or ""
            commit_message = (
                f"Sync commit {short_sha}" if commit_mode else f"Release {tag}"
            )
            target.commit(tmpdir, commit_message, date=source_date)

            target_release = None
            if not commit_mode:
                # Annotated tag
                target.tag(tag, f"Release {tag}")

                # Push (no-op for local targets)
                target.push(project.target.branch, tag)

                # Create release on target platform (no-op for local targets)
                release_name = _clean_text(rel.get("name") or tag)
                release_body = (
                    _clean_text(rel.get("body", ""))
                    if project.preserve_description
                    else ""
                )
                target_release = target.create_release(
                    tag=tag,
                    name=release_name,
                    body=release_body,
                    prerelease=rel.get("prerelease", False),
                )
            else:
                # Commit mode: push the branch only (no tag, no release)
                target.push(project.target.branch)

            # Sync release assets/packages (prebuilts)
            if project.sync_assets and not commit_mode:
                _sync_release_assets(
                    source=source,
                    target=target,
                    rel=rel,
                    tag=tag,
                    target_release=target_release,
                    sync_assets=project.sync_assets,
                    tmpdir=tmpdir,
                    retry_max=project.retry.max_attempts,
                    retry_backoff=project.retry.backoff_seconds,
                    dry_run=False,
                    stream_assets=project.stream_assets,
                )

            # Persist state (commit mode: keyed by SHA; release/tag: by tag name)
            source_commit = rel.get("commit_sha") or ""
            if not source_commit and not commit_mode and hasattr(source, "resolve_commit"):
                source_commit = source.resolve_commit(tag)

            state.add_release(
                project.name,
                state_key,
                {
                    "tag": tag,
                    "source_commit": source_commit,
                    "target_commit": target.head_sha(),
                    "source_date": source_date,
                    "sync_date": datetime.now().astimezone().isoformat(),
                },
            )
            state.save()

            logger.info(
                "Synced %s (target commit %s)",
                f"commit {short_sha}" if commit_mode else f"release {tag}",
                target.head_sha(),
            )

        except Exception:
            logger.exception("Failed to sync release %s", tag)
            raise
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="Sync releases from source to target")
    parser.add_argument("--config", required=True, help="Path to config.yml")
    parser.add_argument("--project", help="Sync only this project (by name)")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print changes without pushing"
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear state and cache before running (fresh start)",
    )
    parser.add_argument(
        "--lint",
        action="store_true",
        help="Lint config file for YAML errors, invalid settings, and redundant options",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Automatically fix misplaced keys and remove redundant options in config file",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    config_path = Path(args.config)
    if not config_path.exists():
        sys.exit(f"Config file not found: {config_path}")

    if args.fix:
        from .linter import fix_config, lint_config

        fix_report = fix_config(config_path)
        if not fix_report.is_valid:
            sys.exit(1)
        if args.lint:
            print()
            lint_report = lint_config(config_path)
            sys.exit(0 if lint_report.is_valid else 1)
        sys.exit(0)

    if args.lint:
        from .linter import lint_config

        report = lint_config(config_path)
        sys.exit(0 if report.is_valid else 1)

    if args.reset:
        gitsync = Path(".gitsync")
        if gitsync.exists():
            shutil.rmtree(gitsync)
            logger.info("Cleared .gitsync/ (state + cache)")

    config = Config.from_path(str(config_path))
    state = State(".gitsync/state.yml")

    projects = [
        p for p in config.projects if not args.project or p.name == args.project
    ]
    if args.project and not projects:
        sys.exit(f"Project '{args.project}' not found in config")

    for project in projects:
        if not project.enabled:
            logger.info("Skipping disabled project: %s", project.name)
            continue
        try:
            sync_project(project, state, dry_run=args.dry_run)
        except Exception:
            logger.exception("Project %s failed", project.name)
            sys.exit(1)


if __name__ == "__main__":
    main()
