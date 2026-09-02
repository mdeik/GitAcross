"""Lint/fix report models and the YAML dump helper.

Kept free of validation logic so both the validator and the fixer can use
them without circular imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import yaml


class LintSeverity(Enum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    REDUNDANT = "REDUNDANT"


@dataclass
class LintIssue:
    severity: LintSeverity
    message: str
    project_name: str | None = None
    key: str | None = None

    def __str__(self) -> str:
        prefix = f"[{self.severity.value}]"
        if self.project_name:
            return f"  {prefix} (project '{self.project_name}'): {self.message}"
        return f"  {prefix} {self.message}"


@dataclass
class LintReport:
    issues: list[LintIssue]

    @property
    def errors(self) -> list[LintIssue]:
        return [i for i in self.issues if i.severity == LintSeverity.ERROR]

    @property
    def warnings(self) -> list[LintIssue]:
        return [i for i in self.issues if i.severity == LintSeverity.WARNING]

    @property
    def redundant(self) -> list[LintIssue]:
        return [i for i in self.issues if i.severity == LintSeverity.REDUNDANT]

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    def __repr__(self):
        return (
            f"LintReport(is_valid={self.is_valid}, "
            f"errors={len(self.errors)}, warnings={len(self.warnings)}, "
            f"redundant={len(self.redundant)})"
        )

    def format_text(self) -> str:
        out = []
        # Group issues by project (None first)
        by_proj: dict[str | None, list[LintIssue]] = {}
        for issue in self.issues:
            by_proj.setdefault(issue.project_name, []).append(issue)

        if None in by_proj:
            for issue in by_proj[None]:
                out.append(f"[{issue.severity.value}] {issue.message}")

        for proj, issues in by_proj.items():
            if proj is None:
                continue
            out.append(f"\nProject '{proj}':")
            for issue in issues:
                out.append(f"  [{issue.severity.value}] {issue.message}")

        summary = f"\nSummary: {len(self.errors)} error(s), {len(self.warnings)} warning(s), {len(self.redundant)} redundant option(s)."
        out.append(summary)
        return "\n".join(out)



class _CleanDumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_CleanDumper.add_representer(str, _str_presenter)


@dataclass
class FixIssue:
    message: str
    project_name: str | None = None

    def __str__(self) -> str:
        if self.project_name:
            return f"  ✓ (project '{self.project_name}'): {self.message}"
        return f"  ✓ {self.message}"


@dataclass
class FixReport:
    fixes: list[FixIssue]
    content: str
    is_valid: bool
    error: str | None = None

    def format_text(self) -> str:
        if not self.is_valid:
            return f"[ERROR] Could not fix configuration: {self.error}"
        if not self.fixes:
            return "No fixes needed. Config is already clean and optimal."
        out = [f"Applied {len(self.fixes)} fix(es):"]
        by_proj: dict[str | None, list[FixIssue]] = {}
        for fix in self.fixes:
            by_proj.setdefault(fix.project_name, []).append(fix)

        if None in by_proj:
            for fix in by_proj[None]:
                out.append(f"  ✓ {fix.message}")

        for proj, fixes in by_proj.items():
            if proj is None:
                continue
            out.append(f"\nProject '{proj}':")
            for fix in fixes:
                out.append(f"  ✓ {fix.message}")

        return "\n".join(out)

    def __repr__(self) -> str:
        return (
            f"FixReport(is_valid={self.is_valid}, fixes={len(self.fixes)}, "
            f"error={self.error!r})"
        )


