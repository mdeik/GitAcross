"""Command-line interface for GitAcross.

The sync logic lives in :mod:`gitacross.main`; this module only parses
``argv`` and maps results to process exit codes.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import cast


def _setup_logging(verbose):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def main():
    from .main import DEFAULT_WORK_DIR, run

    parser = argparse.ArgumentParser(description="Sync releases from source to target")
    _ = parser.add_argument("--config", required=True, help="Path to config.yml")
    _ = parser.add_argument("--project-name", help="Sync only this project (by name)")
    _ = parser.add_argument(
        "--dry-run", action="store_true", help="Print changes without pushing"
    )
    _ = parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear state and cache before running (fresh start)",
    )
    _ = parser.add_argument(
        "--lint",
        action="store_true",
        help="Lint config file for YAML errors, invalid settings, and redundant options",
    )
    _ = parser.add_argument(
        "--fix",
        action="store_true",
        help="Automatically fix misplaced keys and remove redundant options in config file",
    )
    _ = parser.add_argument(
        "--workdir",
        default=DEFAULT_WORK_DIR,
        help="Directory for state.yml and cache/ (default: .gitsync)",
    )
    _ = parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    # Convert argparse's Any values into concrete types (cast is the escape hatch).
    verbose = cast(bool, args.verbose)
    config_path = Path(cast(str, args.config))
    fix_flag = cast(bool, args.fix)
    lint_flag = cast(bool, args.lint)
    project_name = None if args.project_name is None else cast(str, args.project_name)
    dry_run_flag = cast(bool, args.dry_run)
    reset_flag = cast(bool, args.reset)
    workdir = cast(str, args.workdir)

    _setup_logging(verbose)

    if fix_flag:
        if not config_path.exists():
            sys.exit(f"Config file not found: {config_path}")
        from .linter import fix_config, lint_config

        fix_report = fix_config(
            config_path, write_back=not dry_run_flag, dry_run=dry_run_flag
        )
        if not fix_report.is_valid:
            sys.exit(1)
        if lint_flag:
            print()
            lint_report = lint_config(config_path)
            sys.exit(0 if lint_report.is_valid else 1)
        sys.exit(0)

    if lint_flag:
        if not config_path.exists():
            sys.exit(f"Config file not found: {config_path}")
        from .linter import lint_config

        report = lint_config(config_path)
        sys.exit(0 if report.is_valid else 1)

    try:
        results = run(
            config_path,
            project_name=project_name,
            dry_run=dry_run_flag,
            reset=reset_flag,
            work_dir=workdir,
        )
    except (FileNotFoundError, ValueError) as exc:
        sys.exit(str(exc))

    if any(not r["synced"] for r in results):
        sys.exit(1)

