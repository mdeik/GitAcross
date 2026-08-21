"""GitAcross – mirror releases and git commits across platforms."""

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
from .main import sync_project, main

__all__ = [
    "__version__",
    "Config",
    "ProjectConfig",
    "ConfigFixer",
    "ConfigLinter",
    "FixIssue",
    "FixReport",
    "LintIssue",
    "LintReport",
    "LintSeverity",
    "fix_config",
    "lint_config",
    "sync_project",
    "main",
]
