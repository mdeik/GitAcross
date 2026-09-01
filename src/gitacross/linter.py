"""Config linter for GitAcross.

Validates YAML syntax, detects invalid or misplaced configurations,
and flags redundant options that match default values.
"""

from dataclasses import dataclass
from enum import Enum
import io
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
import yaml


class LintSeverity(Enum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    REDUNDANT = "REDUNDANT"


@dataclass
class LintIssue:
    severity: LintSeverity
    message: str
    project: Optional[str] = None
    key: Optional[str] = None

    def __str__(self) -> str:
        prefix = f"[{self.severity.value}]"
        if self.project:
            return f"  {prefix} (project '{self.project}'): {self.message}"
        return f"  {prefix} {self.message}"


@dataclass
class LintReport:
    issues: List[LintIssue]

    @property
    def errors(self) -> List[LintIssue]:
        return [i for i in self.issues if i.severity == LintSeverity.ERROR]

    @property
    def warnings(self) -> List[LintIssue]:
        return [i for i in self.issues if i.severity == LintSeverity.WARNING]

    @property
    def redundant(self) -> List[LintIssue]:
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
        by_proj: Dict[Optional[str], List[LintIssue]] = {}
        for issue in self.issues:
            by_proj.setdefault(issue.project, []).append(issue)

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


_VALID_PROJECT_KEYS = {
    "name",
    "enabled",
    "source",
    "target",
    "renderer",
    "retry",
    "preserve_description",
    "preserve_release_description",
    "sync_assets",
    "preserve_assets",
    "include_assets",
    "stream_assets",
    "commit_message",
    "commit_template",
    "release_description",
    "release_notes_template",
    "description_template",
}

_KNOWN_SOURCE_KEYS = {
    "mode": "source",
    "sync_from": "source",
    "include_prereleases": "source",
    "include_drafts": "source",
    "tag_pattern": "source",
}

_KNOWN_ENDPOINT_KEYS = {
    "repo": "source or target",
    "api": "source or target",
    "token": "source or target",
    "branch": "source or target",
}

_VALID_SOURCE_KEYS_REMOTE = {
    "type",
    "repo",
    "api",
    "token",
    "mode",
    "branch",
    "include_prereleases",
    "include_drafts",
    "sync_from",
}

_VALID_SOURCE_KEYS_LOCAL = {
    "type",
    "path",
    "tag_pattern",
    "sync_from",
}

_VALID_TARGET_KEYS_REMOTE = {
    "type",
    "repo",
    "api",
    "token",
    "branch",
}

_VALID_TARGET_KEYS_LOCAL = {
    "type",
    "path",
    "branch",
}


class ConfigLinter:
    def __init__(self):
        self.issues: List[LintIssue] = []

    def __repr__(self):
        return f"ConfigLinter(issues={len(self.issues)})"

    def _add(
        self,
        severity: LintSeverity,
        message: str,
        project: Optional[str] = None,
        key: Optional[str] = None,
    ):
        self.issues.append(
            LintIssue(severity=severity, message=message, project=project, key=key)
        )

    def lint_file(self, config_path: Union[str, Path]) -> LintReport:
        path = Path(config_path)
        if not path.exists():
            self._add(LintSeverity.ERROR, f"File not found: {path}")
            return LintReport(self.issues)

        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            self._add(LintSeverity.ERROR, f"Cannot read file '{path}': {e}")
            return LintReport(self.issues)

        return self.lint_yaml_string(content, filename=str(path))

    def lint_yaml_string(
        self, content: str, filename: Optional[str] = None
    ) -> LintReport:
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            loc = (
                f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
            )
            self._add(
                LintSeverity.ERROR,
                f"YAML syntax error{loc}: {e.problem if hasattr(e, 'problem') and e.problem else str(e)}",
            )
            return LintReport(self.issues)

        if data is None:
            self._add(LintSeverity.WARNING, "Configuration file is empty")
            return LintReport(self.issues)

        projects_raw = []
        if isinstance(data, list):
            projects_raw = data
        elif isinstance(data, dict):
            if "projects" in data:
                if not isinstance(data["projects"], list):
                    self._add(
                        LintSeverity.ERROR,
                        "'projects' key must contain a list of project configurations.",
                    )
                    return LintReport(self.issues)
                projects_raw = data["projects"]
            else:
                self._add(
                    LintSeverity.ERROR,
                    "Top-level YAML object must be a list of projects or a map with a 'projects' list.",
                )
                return LintReport(self.issues)
        else:
            self._add(
                LintSeverity.ERROR,
                f"Invalid top-level YAML type: expected list or dict, got {type(data).__name__}.",
            )
            return LintReport(self.issues)

        seen_names = set()
        for idx, p in enumerate(projects_raw):
            if not isinstance(p, dict):
                self._add(
                    LintSeverity.ERROR,
                    f"Project at index {idx} is not a valid map/dict (got {type(p).__name__}).",
                )
                continue
            self._lint_project(p, idx, seen_names)

        return LintReport(self.issues)

    def _lint_project(self, p: Dict[str, Any], idx: int, seen_names: set):
        name = p.get("name")
        if not name or not isinstance(name, str):
            self._add(
                LintSeverity.ERROR,
                f"Project at index {idx} is missing required 'name' string field.",
            )
            p_name = f"<unnamed project #{idx}>"
        else:
            p_name = name
            if name in seen_names:
                self._add(
                    LintSeverity.ERROR,
                    f"Duplicate project name '{name}' found.",
                    project=p_name,
                )
            seen_names.add(name)

        # 1. Project-level keys validation
        for k in p:
            if k in _VALID_PROJECT_KEYS:
                continue
            if k in _KNOWN_SOURCE_KEYS:
                self._add(
                    LintSeverity.ERROR,
                    f"'{k}: {p[k]}' was specified at project level, but belongs under 'source:' (e.g. source.{k}: {p[k]}).",
                    project=p_name,
                    key=k,
                )
            elif k in _KNOWN_ENDPOINT_KEYS:
                self._add(
                    LintSeverity.ERROR,
                    f"'{k}: {p[k]}' was specified at project level, but belongs under '{_KNOWN_ENDPOINT_KEYS[k]}:'.",
                    project=p_name,
                    key=k,
                )
            else:
                self._add(
                    LintSeverity.WARNING,
                    f"Unrecognized project-level option '{k}'.",
                    project=p_name,
                    key=k,
                )

        # 2. Redundant project-level options
        if "enabled" in p:
            val = p["enabled"]
            if not isinstance(val, bool):
                self._add(
                    LintSeverity.ERROR,
                    f"'enabled' must be a boolean (got {type(val).__name__}).",
                    project=p_name,
                    key="enabled",
                )
            elif val is True:
                self._add(
                    LintSeverity.REDUNDANT,
                    "'enabled: true' is redundant (default is true).",
                    project=p_name,
                    key="enabled",
                )

        if "preserve_description" in p:
            val = p["preserve_description"]
            if not isinstance(val, bool):
                self._add(
                    LintSeverity.ERROR,
                    f"'preserve_description' must be a boolean (got {type(val).__name__}).",
                    project=p_name,
                )
            elif val is True:
                self._add(
                    LintSeverity.REDUNDANT,
                    "'preserve_description: true' is redundant (default is true).",
                    project=p_name,
                )

        if "sync_assets" in p:
            val = p["sync_assets"]
            if isinstance(val, bool):
                if val is False:
                    self._add(
                        LintSeverity.REDUNDANT,
                        "'sync_assets: false' is redundant (default is false).",
                        project=p_name,
                    )
            elif isinstance(val, str):
                pass
            elif isinstance(val, (list, tuple)):
                if not all(isinstance(x, str) for x in val):
                    self._add(
                        LintSeverity.ERROR,
                        "'sync_assets' list must only contain string glob patterns.",
                        project=p_name,
                    )
            else:
                self._add(
                    LintSeverity.ERROR,
                    f"'sync_assets' must be a boolean, string, or list of strings (got {type(val).__name__}).",
                    project=p_name,
                )

        if "stream_assets" in p:
            val = p["stream_assets"]
            if not isinstance(val, bool):
                self._add(
                    LintSeverity.ERROR,
                    f"'stream_assets' must be a boolean (got {type(val).__name__}).",
                    project=p_name,
                )
            elif val is False:
                self._add(
                    LintSeverity.REDUNDANT,
                    "'stream_assets: false' is redundant (default is false).",
                    project=p_name,
                )

        for key in ("commit_message", "commit_template"):
            if key in p:
                val = p[key]
                if not isinstance(val, str):
                    self._add(
                        LintSeverity.ERROR,
                        f"'{key}' must be a string (got {type(val).__name__}).",
                        project=p_name,
                        key=key,
                    )

        for key in ("release_description", "release_notes_template", "description_template"):
            if key in p:
                val = p[key]
                if not isinstance(val, str):
                    self._add(
                        LintSeverity.ERROR,
                        f"'{key}' must be a string (got {type(val).__name__}).",
                        project=p_name,
                        key=key,
                    )

        # 3. Source endpoint checks
        source = p.get("source")
        if source is None:
            self._add(
                LintSeverity.ERROR,
                "Missing required 'source' endpoint configuration.",
                project=p_name,
            )
        elif not isinstance(source, dict):
            self._add(
                LintSeverity.ERROR,
                f"'source' must be a dictionary (got {type(source).__name__}).",
                project=p_name,
            )
        else:
            self._lint_source(source, p_name)

        # 4. Target endpoint checks
        target = p.get("target")
        if target is None:
            self._add(
                LintSeverity.ERROR,
                "Missing required 'target' endpoint configuration.",
                project=p_name,
            )
        elif not isinstance(target, dict):
            self._add(
                LintSeverity.ERROR,
                f"'target' must be a dictionary (got {type(target).__name__}).",
                project=p_name,
            )
        else:
            self._lint_target(target, p_name)

        # 5. Retry checks
        if "retry" in p:
            retry = p["retry"]
            if not isinstance(retry, dict):
                self._add(
                    LintSeverity.ERROR,
                    f"'retry' must be a dictionary (got {type(retry).__name__}).",
                    project=p_name,
                )
            else:
                self._lint_retry(retry, p_name)

        # 6. Renderer checks
        if "renderer" in p:
            renderer = p["renderer"]
            if not isinstance(renderer, dict):
                self._add(
                    LintSeverity.ERROR,
                    f"'renderer' must be a dictionary (got {type(renderer).__name__}).",
                    project=p_name,
                )
            else:
                self._lint_renderer(renderer, p_name)

    def _lint_source(self, s: Dict[str, Any], p_name: str):
        src_type = s.get("type", "gitea")
        if src_type not in ("gitea", "github", "local"):
            self._add(
                LintSeverity.ERROR,
                f"Invalid source type '{src_type}'. Expected 'gitea', 'github', or 'local'.",
                project=p_name,
            )
            return

        is_remote = src_type in ("gitea", "github")
        valid_keys = _VALID_SOURCE_KEYS_REMOTE if is_remote else _VALID_SOURCE_KEYS_LOCAL
        for k in s:
            if k in _VALID_PROJECT_KEYS and k != "type":
                self._add(
                    LintSeverity.WARNING,
                    f"'{k}' was specified inside 'source:', but belongs at the project level.",
                    project=p_name,
                    key=k,
                )
            elif k not in valid_keys:
                self._add(
                    LintSeverity.WARNING,
                    f"Unrecognized option '{k}' under source ({src_type}).",
                    project=p_name,
                )

        if is_remote:
            if not s.get("repo"):
                self._add(
                    LintSeverity.ERROR,
                    "Source is missing required 'repo' string (e.g. 'owner/repo').",
                    project=p_name,
                )
            if not s.get("api"):
                self._add(
                    LintSeverity.ERROR,
                    "Source is missing required 'api' URL.",
                    project=p_name,
                )
            if not s.get("token"):
                self._add(
                    LintSeverity.ERROR,
                    "Source is missing required 'token'.",
                    project=p_name,
                )

            mode = s.get("mode", "release")
            if mode not in ("release", "tag", "commit"):
                self._add(
                    LintSeverity.ERROR,
                    f"Invalid source mode '{mode}'. Expected 'release', 'tag', or 'commit'.",
                    project=p_name,
                )
            elif "mode" in s and s["mode"] == "release":
                self._add(
                    LintSeverity.REDUNDANT,
                    "'source.mode: release' is redundant (default is release).",
                    project=p_name,
                )

            if "include_prereleases" in s:
                val = s["include_prereleases"]
                if not isinstance(val, bool):
                    self._add(
                        LintSeverity.ERROR,
                        f"'source.include_prereleases' must be a boolean (got {type(val).__name__}).",
                        project=p_name,
                    )
                elif val is False:
                    self._add(
                        LintSeverity.REDUNDANT,
                        "'source.include_prereleases: false' is redundant (default is false).",
                        project=p_name,
                    )

            if "include_drafts" in s:
                val = s["include_drafts"]
                if not isinstance(val, bool):
                    self._add(
                        LintSeverity.ERROR,
                        f"'source.include_drafts' must be a boolean (got {type(val).__name__}).",
                        project=p_name,
                    )
                elif val is False:
                    self._add(
                        LintSeverity.REDUNDANT,
                        "'source.include_drafts: false' is redundant (default is false).",
                        project=p_name,
                    )

            if "sync_from" in s and s["sync_from"] == "":
                self._add(
                    LintSeverity.REDUNDANT,
                    "'source.sync_from: \"\"' is redundant (default is empty).",
                    project=p_name,
                )
        else:
            # Local source
            if not s.get("path"):
                self._add(
                    LintSeverity.ERROR,
                    "Local source is missing required 'path'.",
                    project=p_name,
                )
            if "tag_pattern" in s:
                if s["tag_pattern"] == "*":
                    self._add(
                        LintSeverity.REDUNDANT,
                        "'source.tag_pattern: \"*\"' is redundant (default is \"*\").",
                        project=p_name,
                    )

    def _lint_target(self, t: Dict[str, Any], p_name: str):
        tgt_type = t.get("type", "github")
        if tgt_type not in ("github", "gitea", "local"):
            self._add(
                LintSeverity.ERROR,
                f"Invalid target type '{tgt_type}'. Expected 'github', 'gitea', or 'local'.",
                project=p_name,
            )
            return

        is_remote = tgt_type in ("github", "gitea")
        valid_keys = _VALID_TARGET_KEYS_REMOTE if is_remote else _VALID_TARGET_KEYS_LOCAL
        for k in t:
            if k in _VALID_PROJECT_KEYS and k != "type":
                self._add(
                    LintSeverity.WARNING,
                    f"'{k}' was specified inside 'target:', but belongs at the project level.",
                    project=p_name,
                    key=k,
                )
            elif k in _KNOWN_SOURCE_KEYS:
                self._add(
                    LintSeverity.ERROR,
                    f"'{k}' was specified inside 'target:', but belongs under 'source:'.",
                    project=p_name,
                    key=k,
                )
            elif k not in valid_keys:
                self._add(
                    LintSeverity.WARNING,
                    f"Unrecognized option '{k}' under target ({tgt_type}).",
                    project=p_name,
                )

        if is_remote:
            if not t.get("repo"):
                self._add(
                    LintSeverity.ERROR,
                    "Target is missing required 'repo' string (e.g. 'owner/repo').",
                    project=p_name,
                )
            if not t.get("api"):
                self._add(
                    LintSeverity.ERROR,
                    "Target is missing required 'api' URL.",
                    project=p_name,
                )
            if not t.get("token"):
                self._add(
                    LintSeverity.ERROR,
                    "Target is missing required 'token'.",
                    project=p_name,
                )

        if "branch" in t and t["branch"] == "main":
            self._add(
                LintSeverity.REDUNDANT,
                "'target.branch: main' is redundant (default is main).",
                project=p_name,
            )

    def _lint_retry(self, r: Dict[str, Any], p_name: str):
        valid_retry_keys = {"max_attempts", "backoff_seconds"}
        for k in r:
            if k not in valid_retry_keys:
                self._add(
                    LintSeverity.WARNING,
                    f"Unrecognized option '{k}' under retry.",
                    project=p_name,
                )

        max_att = r.get("max_attempts")
        backoff = r.get("backoff_seconds")

        if max_att is not None:
            if not isinstance(max_att, int) or max_att < 1:
                self._add(
                    LintSeverity.ERROR,
                    f"'retry.max_attempts' must be an integer >= 1 (got {max_att}).",
                    project=p_name,
                )
            elif max_att == 3:
                self._add(
                    LintSeverity.REDUNDANT,
                    "'retry.max_attempts: 3' is redundant (default is 3).",
                    project=p_name,
                )

        if backoff is not None:
            if not isinstance(backoff, (int, float)) or backoff < 0:
                self._add(
                    LintSeverity.ERROR,
                    f"'retry.backoff_seconds' must be a number >= 0 (got {backoff}).",
                    project=p_name,
                )
            elif backoff == 2:
                self._add(
                    LintSeverity.REDUNDANT,
                    "'retry.backoff_seconds: 2' is redundant (default is 2).",
                    project=p_name,
                )

    def _lint_renderer(self, ren: Dict[str, Any], p_name: str):
        valid_renderer_keys = {"ignore", "operations", "author"}
        for k in ren:
            if k not in valid_renderer_keys:
                self._add(
                    LintSeverity.WARNING,
                    f"Unrecognized option '{k}' under renderer.",
                    project=p_name,
                )

        if "ignore" in ren:
            ig = ren["ignore"]
            if not isinstance(ig, (list, tuple)):
                self._add(
                    LintSeverity.ERROR,
                    f"'renderer.ignore' must be a list of patterns (got {type(ig).__name__}).",
                    project=p_name,
                )

        if "author" in ren:
            auth = ren["author"]
            if not isinstance(auth, dict):
                self._add(
                    LintSeverity.ERROR,
                    f"'renderer.author' must be a dictionary (got {type(auth).__name__}).",
                    project=p_name,
                )

        if "operations" in ren:
            ops = ren["operations"]
            if not isinstance(ops, list):
                self._add(
                    LintSeverity.ERROR,
                    f"'renderer.operations' must be a list of operation maps (got {type(ops).__name__}).",
                    project=p_name,
                )
            else:
                self._lint_operations(ops, p_name)

    def _lint_operations(self, ops: List[Any], p_name: str):
        valid_op_types = {"remove", "rename", "replace", "add", "validate"}
        for idx, op in enumerate(ops):
            if not isinstance(op, dict):
                self._add(
                    LintSeverity.ERROR,
                    f"Operation at index {idx} must be a dictionary (got {type(op).__name__}).",
                    project=p_name,
                )
                continue
            for op_type, items in op.items():
                if op_type not in valid_op_types:
                    self._add(
                        LintSeverity.ERROR,
                        f"Unknown renderer operation '{op_type}' at index {idx}. Valid operations: {', '.join(sorted(valid_op_types))}.",
                        project=p_name,
                    )
                    continue
                if not isinstance(items, list):
                    self._add(
                        LintSeverity.ERROR,
                        f"Renderer operation '{op_type}' must contain a list of actions.",
                        project=p_name,
                    )
                    continue

                if op_type == "remove":
                    for item in items:
                        if not isinstance(item, dict) or not item.get("path"):
                            self._add(
                                LintSeverity.ERROR,
                                "Remove operation item requires 'path'.",
                                project=p_name,
                            )
                        pat = item.get("pattern")
                        if pat == "literal":
                            self._add(
                                LintSeverity.REDUNDANT,
                                "'pattern: literal' in remove operation is redundant (default is literal).",
                                project=p_name,
                            )
                        elif pat and pat not in ("literal", "glob", "regex"):
                            self._add(
                                LintSeverity.ERROR,
                                f"Invalid remove pattern mode '{pat}'. Expected 'literal', 'glob', or 'regex'.",
                                project=p_name,
                            )

                elif op_type == "rename":
                    for item in items:
                        if (
                            not isinstance(item, dict)
                            or not item.get("from")
                            or not item.get("to")
                        ):
                            self._add(
                                LintSeverity.ERROR,
                                "Rename operation item requires 'from' and 'to'.",
                                project=p_name,
                            )
                        pat = item.get("pattern")
                        if pat == "literal":
                            self._add(
                                LintSeverity.REDUNDANT,
                                "'pattern: literal' in rename operation is redundant (default is literal).",
                                project=p_name,
                            )
                        elif pat and pat != "literal":
                            self._add(
                                LintSeverity.ERROR,
                                f"Rename pattern mode '{pat}' is not supported — only 'literal' is supported.",
                                project=p_name,
                            )

                elif op_type == "replace":
                    for item in items:
                        if (
                            not isinstance(item, dict)
                            or "search" not in item
                            or "replace" not in item
                        ):
                            self._add(
                                LintSeverity.ERROR,
                                "Replace operation item requires 'search' and 'replace'.",
                                project=p_name,
                            )
                        if not isinstance(item, dict):
                            continue
                        pat = item.get("pattern")
                        if pat == "literal":
                            self._add(
                                LintSeverity.REDUNDANT,
                                "'pattern: literal' in replace operation is redundant (default is literal).",
                                project=p_name,
                            )
                        elif pat and pat not in ("literal", "regex"):
                            self._add(
                                LintSeverity.ERROR,
                                f"Invalid replace pattern mode '{pat}'. Expected 'literal' or 'regex'.",
                                project=p_name,
                            )
                        if item.get("case_sensitive") is True:
                            self._add(
                                LintSeverity.REDUNDANT,
                                "'case_sensitive: true' in replace operation is redundant (default is true).",
                                project=p_name,
                            )
                        if item.get("match_case") is False:
                            self._add(
                                LintSeverity.REDUNDANT,
                                "'match_case: false' in replace operation is redundant (default is false).",
                                project=p_name,
                            )
                        for key in ("case_sensitive", "match_case"):
                            if key in item and not isinstance(item[key], bool):
                                self._add(
                                    LintSeverity.ERROR,
                                    f"Replace option '{key}' must be a boolean (true/false).",
                                    project=p_name,
                                )

                elif op_type == "add":
                    for item in items:
                        if (
                            not isinstance(item, dict)
                            or not item.get("path")
                            or "content" not in item
                        ):
                            self._add(
                                LintSeverity.ERROR,
                                "Add operation item requires 'path' and 'content'.",
                                project=p_name,
                            )

                elif op_type == "validate":
                    valid_asserts = {
                        "file_exists",
                        "file_absent",
                        "string_exists",
                        "string_absent",
                    }
                    for item in items:
                        if not isinstance(item, dict) or not item.get("path"):
                            self._add(
                                LintSeverity.ERROR,
                                "Validate operation item requires 'path'.",
                                project=p_name,
                            )
                        if not isinstance(item, dict):
                            continue
                        asrt = item.get("assert")
                        if asrt not in valid_asserts:
                            self._add(
                                LintSeverity.ERROR,
                                f"Invalid validate assert '{asrt}'. Expected one of {', '.join(sorted(valid_asserts))}.",
                                project=p_name,
                            )
                        if asrt in ("string_exists", "string_absent") and not item.get(
                            "pattern"
                        ):
                            self._add(
                                LintSeverity.ERROR,
                                f"Validate assert '{asrt}' requires 'pattern'.",
                                project=p_name,
                            )
                        if item.get("case_sensitive") is True:
                            self._add(
                                LintSeverity.REDUNDANT,
                                "'case_sensitive: true' in validate operation is redundant (default is true).",
                                project=p_name,
                            )
                        if "case_sensitive" in item and not isinstance(
                            item["case_sensitive"], bool
                        ):
                            self._add(
                                LintSeverity.ERROR,
                                "Validate option 'case_sensitive' must be a boolean (true/false).",
                                project=p_name,
                            )
                        if asrt not in ("string_exists", "string_absent") and (
                            "case_sensitive" in item
                        ):
                            self._add(
                                LintSeverity.WARNING,
                                f"'case_sensitive' only applies to string_exists/string_absent (ignored for '{asrt}').",
                                project=p_name,
                            )


def lint_config(
    config_path: Union[str, Path], print_output: bool = True
) -> LintReport:
    """Lint a config file and optionally print the results."""
    linter = ConfigLinter()
    report = linter.lint_file(config_path)
    if print_output:
        print(report.format_text())
    return report


# ---------------------------------------------------------------------------
# Config Fixer
# ---------------------------------------------------------------------------


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
    project: Optional[str] = None

    def __str__(self) -> str:
        if self.project:
            return f"  ✓ (project '{self.project}'): {self.message}"
        return f"  ✓ {self.message}"


@dataclass
class FixReport:
    fixes: List[FixIssue]
    content: str
    is_valid: bool
    error: Optional[str] = None

    def format_text(self) -> str:
        if not self.is_valid:
            return f"[ERROR] Could not fix configuration: {self.error}"
        if not self.fixes:
            return "No fixes needed. Config is already clean and optimal."
        out = [f"Applied {len(self.fixes)} fix(es):"]
        by_proj: Dict[Optional[str], List[FixIssue]] = {}
        for fix in self.fixes:
            by_proj.setdefault(fix.project, []).append(fix)

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


class ConfigFixer:
    def __init__(self):
        self.fixes: List[FixIssue] = []

    def __repr__(self):
        return f"ConfigFixer(fixes={len(self.fixes)})"

    def _add(self, message: str, project: Optional[str] = None):
        self.fixes.append(FixIssue(message=message, project=project))

    def fix_yaml_string(self, content: str) -> FixReport:
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as e:
            return FixReport(fixes=[], content=content, is_valid=False, error=str(e))

        if data is None:
            return FixReport(fixes=[], content=content, is_valid=True)

        is_dict_wrapper = isinstance(data, dict) and "projects" in data
        projects_list = data["projects"] if is_dict_wrapper else data

        if not isinstance(projects_list, list):
            return FixReport(
                fixes=[],
                content=content,
                is_valid=False,
                error="Top-level YAML structure must be a list of projects or a map with a 'projects' list.",
            )

        for idx, p in enumerate(projects_list):
            if isinstance(p, dict):
                self._fix_project(p)

        fixed_yaml = yaml.dump(
            data,
            Dumper=_CleanDumper,
            sort_keys=False,
            default_flow_style=False,
        )
        return FixReport(fixes=self.fixes, content=fixed_yaml, is_valid=True)

    def _fix_project(self, p: Dict[str, Any]):
        p_name = p.get("name") or "<unnamed>"

        # 1. Misplaced endpoint keys at project level -> move to source
        for k in list(p.keys()):
            if k in _KNOWN_SOURCE_KEYS:
                val = p.pop(k)
                if "source" not in p or not isinstance(p["source"], dict):
                    p["source"] = {}
                if k not in p["source"]:
                    p["source"][k] = val
                    self._add(
                        f"Moved '{k}: {val}' from project level into 'source:'",
                        project=p_name,
                    )
                else:
                    self._add(
                        f"Removed duplicate project-level '{k}' (already present under source)",
                        project=p_name,
                    )

        # 2. Misplaced project-level keys inside source -> move to project level
        if isinstance(p.get("source"), dict):
            for k in list(p["source"].keys()):
                if k in _VALID_PROJECT_KEYS and k != "type":
                    val = p["source"].pop(k)
                    if k not in p:
                        p[k] = val
                        self._add(
                            f"Moved '{k}' from 'source:' to project level",
                            project=p_name,
                        )
                    else:
                        self._add(
                            f"Removed duplicate '{k}' from 'source:' (already present at project level)",
                            project=p_name,
                        )

        # 3. Misplaced project keys in target -> move to project level
        #    and misplaced source keys in target -> move to source
        if isinstance(p.get("target"), dict):
            for k in list(p["target"].keys()):
                if k in _VALID_PROJECT_KEYS and k != "type":
                    val = p["target"].pop(k)
                    if k not in p:
                        p[k] = val
                        self._add(
                            f"Moved '{k}' from 'target:' to project level",
                            project=p_name,
                        )
                    else:
                        self._add(
                            f"Removed duplicate '{k}' from 'target:' (already present at project level)",
                            project=p_name,
                        )
                elif k in _KNOWN_SOURCE_KEYS:
                    val = p["target"].pop(k)
                    if "source" not in p or not isinstance(p["source"], dict):
                        p["source"] = {}
                    if k not in p["source"]:
                        p["source"][k] = val
                        self._add(
                            f"Moved '{k}: {val}' from 'target:' into 'source:'",
                            project=p_name,
                        )
                    else:
                        self._add(
                            f"Removed duplicate '{k}' from 'target:' (already present under source)",
                            project=p_name,
                        )

        # 4. Redundant project-level options
        if p.get("enabled") is True:
            del p["enabled"]
            self._add("Removed redundant 'enabled: true'", project=p_name)

        for key in ("preserve_description", "preserve_release_description"):
            if p.get(key) is True:
                del p[key]
                self._add(f"Removed redundant '{key}: true'", project=p_name)

        for key in ("sync_assets", "preserve_assets", "include_assets"):
            if p.get(key) is False:
                del p[key]
                self._add(f"Removed redundant '{key}: false'", project=p_name)

        if p.get("stream_assets") is False:
            del p["stream_assets"]
            self._add("Removed redundant 'stream_assets: false'", project=p_name)

        # 3. Source endpoint fixes
        if isinstance(p.get("source"), dict):
            src = p["source"]
            if src.get("mode") == "release":
                del src["mode"]
                self._add(
                    "Removed redundant 'source.mode: release'", project=p_name
                )
            if src.get("include_prereleases") is False:
                del src["include_prereleases"]
                self._add(
                    "Removed redundant 'source.include_prereleases: false'",
                    project=p_name,
                )
            if src.get("include_drafts") is False:
                del src["include_drafts"]
                self._add(
                    "Removed redundant 'source.include_drafts: false'",
                    project=p_name,
                )
            if src.get("sync_from") == "":
                del src["sync_from"]
                self._add(
                    "Removed redundant 'source.sync_from: \"\"'",
                    project=p_name,
                )
            if src.get("tag_pattern") == "*":
                del src["tag_pattern"]
                self._add(
                    "Removed redundant 'source.tag_pattern: \"*\"'",
                    project=p_name,
                )

        # 4. Target endpoint fixes
        if isinstance(p.get("target"), dict):
            tgt = p["target"]
            if tgt.get("branch") == "main":
                del tgt["branch"]
                self._add(
                    "Removed redundant 'target.branch: main'", project=p_name
                )

        # 5. Retry fixes
        if isinstance(p.get("retry"), dict):
            ret = p["retry"]
            if ret.get("max_attempts") == 3:
                del ret["max_attempts"]
            if ret.get("backoff_seconds") == 2:
                del ret["backoff_seconds"]
            if len(ret) == 0:
                del p["retry"]
                self._add(
                    "Removed default 'retry' configuration block",
                    project=p_name,
                )
            else:
                self._add(
                    "Removed redundant retry defaults", project=p_name
                )

        # 6. Renderer fixes
        if isinstance(p.get("renderer"), dict):
            ren = p["renderer"]
            if isinstance(ren.get("operations"), list):
                for op in ren["operations"]:
                    if not isinstance(op, dict):
                        continue
                    for op_type, items in op.items():
                        if isinstance(items, list):
                            for item in items:
                                if not isinstance(item, dict):
                                    continue
                                if item.get("pattern") == "literal":
                                    del item["pattern"]
                                    self._add(
                                        f"Removed redundant 'pattern: literal' in {op_type} operation",
                                        project=p_name,
                                    )
                                if op_type == "replace":
                                    if item.get("case_sensitive") is True:
                                        del item["case_sensitive"]
                                        self._add(
                                            f"Removed redundant 'case_sensitive: true' in replace operation",
                                            project=p_name,
                                        )
                                    if item.get("match_case") is False:
                                        del item["match_case"]
                                        self._add(
                                            f"Removed redundant 'match_case: false' in replace operation",
                                            project=p_name,
                                        )
                                if op_type == "validate" and item.get(
                                    "case_sensitive"
                                ) is True:
                                    del item["case_sensitive"]
                                    self._add(
                                        f"Removed redundant 'case_sensitive: true' in validate operation",
                                        project=p_name,
                                    )


def fix_config(
    config_path: Union[str, Path],
    write_back: bool = True,
    print_output: bool = True,
) -> FixReport:
    """Fix misplaced and redundant options in config file."""
    path = Path(config_path)
    if not path.exists():
        report = FixReport(
            fixes=[], content="", is_valid=False, error=f"File not found: {path}"
        )
        if print_output:
            print(report.format_text())
        return report

    content = path.read_text(encoding="utf-8")
    fixer = ConfigFixer()
    report = fixer.fix_yaml_string(content)
    if report.is_valid and write_back and report.fixes:
        path.write_text(report.content, encoding="utf-8")
    if print_output:
        print(report.format_text())
    return report

