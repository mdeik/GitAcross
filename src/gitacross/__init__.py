"""GitAcross – mirror releases and git commits across platforms.

Quickstart (Python API)::

    import gitacross

    # Sync every enabled project in a config file:
    results = gitacross.run("config.yml")

    # Sync a single project with dry-run mode:
    results = gitacross.run("config.yml", project="my-mirror", dry_run=True)

    # Lower-level: build objects yourself for full control:
    config = gitacross.Config("config.yml")
    for project in config.projects:
        if project.enabled:
            gitacross.sync_project(project, ".gitsync")

    # Lint / fix a config file:
    report = gitacross.lint_config("config.yml")
    if not report.is_valid:
        gitacross.fix_config("config.yml")
"""
from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version
from pathlib import Path

from .config import Config, ProjectConfig
from .linter import (
    ConfigFixer,
    ConfigLinter,
    FixIssue,
    FixReport,
    LintIssue,
    LintReport,
    LintSeverity,
    fix_config,
    lint_config,
)
from .main import main, run, sync_project


def _package_version() -> str:
    """Return the package version, stored only in pyproject.toml.

    Installed: read from the distribution metadata, which setuptools populates
    from ``[project].version``. Source checkout (package not installed): parse
    the literal out of ``pyproject.toml``. Falls back to ``"0.0.0"`` if neither
    is available.
    """
    try:
        return _installed_version("gitacross")
    except PackageNotFoundError:
        pass
    pyproject = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
    if pyproject.is_file():
        match = re.search(
            r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.MULTILINE
        )
        if match:
            return match.group(1)
    return "0.0.0"


__version__ = _package_version()

__all__ = [
    "Config",
    "ConfigFixer",
    "ConfigLinter",
    "FixIssue",
    "FixReport",
    "LintIssue",
    "LintReport",
    "LintSeverity",
    "ProjectConfig",
    "__version__",
    "fix_config",
    "lint_config",
    "main",
    "run",
    "sync_project",
]
