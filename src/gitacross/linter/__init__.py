"""Config linter and fixer for GitAcross.

``lint_config`` validates a config; ``fix_config`` rewrites it in place to
remove redundant and misplaced options.

Implementation lives in three modules:

* :mod:`gitacross.linter.models` — report/issue data types and YAML dump helper
* :mod:`gitacross.linter.validator` — :class:`ConfigLinter` (the checks)
* :mod:`gitacross.linter.fixer` — :class:`ConfigFixer` (the rewrites)
"""
from .fixer import ConfigFixer, fix_config
from .models import FixIssue, FixReport, LintIssue, LintReport, LintSeverity
from .validator import ConfigLinter, lint_config

__all__ = [
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
