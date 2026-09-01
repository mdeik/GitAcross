"""GitAcross – mirror releases and git commits across platforms.

Quickstart (Python API)::

    import gitacross

    # Sync every enabled project in a config file:
    results = gitacross.run("config.yml")

    # Sync a single project with dry-run mode:
    results = gitacross.run("config.yml", project="my-mirror", dry_run=True)

    # Lower-level: build objects yourself for full control:
    config = gitacross.Config.from_path("config.yml")
    for project in config.projects:
        if project.enabled:
            gitacross.sync_project(project, ".gitsync")

    # Lint / fix a config file:
    report = gitacross.lint_config("config.yml")
    if not report.is_valid:
        gitacross.fix_config("config.yml")
"""

__version__ = "0.1.0"

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
from .main import run, sync_project, main

__all__ = [
    "__version__",
    # Config
    "Config",
    "ProjectConfig",
    # High-level API
    "run",
    # Lower-level API
    "sync_project",
    # CLI entry point (exposed for completeness)
    "main",
    # Linter
    "ConfigFixer",
    "ConfigLinter",
    "FixIssue",
    "FixReport",
    "LintIssue",
    "LintReport",
    "LintSeverity",
    "fix_config",
    "lint_config",
]
