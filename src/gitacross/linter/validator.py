"""Config validation — the linter's core check pass.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import yaml

from ..config import KNOWN_ENDPOINT_KEYS, KNOWN_SOURCE_KEYS, VALID_PROJECT_KEYS
from ..providers import ENDPOINT_TYPES, REMOTE_TYPES
from .keys import (
    _VALID_SOURCE_KEYS_LOCAL,
    _VALID_SOURCE_KEYS_REMOTE,
    _VALID_TARGET_KEYS_LOCAL,
    _VALID_TARGET_KEYS_REMOTE,
)
from .models import LintIssue, LintReport, LintSeverity


class ConfigLinter:
    def __init__(self):
        self.issues: list[LintIssue] = []

    def __repr__(self):
        return f"ConfigLinter(issues={len(self.issues)})"

    def _add(
        self,
        severity: LintSeverity,
        message: str,
        project_name: str | None = None,
        key: str | None = None,
    ):
        self.issues.append(
            LintIssue(
                severity=severity,
                message=message,
                project_name=project_name,
                key=key,
            )
        )

    def lint_file(
        self, config: str | Path | io.TextIOBase | io.BufferedIOBase
    ) -> LintReport:
        """Lint a config given by path (str/Path) or an open file-like object."""
        # Fresh accumulator — reusing an instance must not leak prior results
        self.issues = []
        if isinstance(config, (str, Path)):
            path = Path(config)
            if not path.exists():
                self._add(LintSeverity.ERROR, f"File not found: {path}")
                return LintReport(self.issues)

            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, ValueError) as e:
                self._add(LintSeverity.ERROR, f"Cannot read file '{path}': {e}")
                return LintReport(self.issues)

            return self.lint_yaml_string(content, _filename=str(path))

        # File-like input (io.StringIO, an open() handle, BytesIO, ...)
        try:
            content = config.read()
        except (OSError, ValueError, AttributeError) as e:
            self._add(LintSeverity.ERROR, f"Cannot read config stream: {e}")
            return LintReport(self.issues)
        return self.lint_yaml_string(content, _filename=getattr(config, "name", None))

    def lint_yaml_string(
        self, content: str | bytes, _filename: str | None = None
    ) -> LintReport:
        # Fresh accumulator — reusing an instance must not leak prior results
        self.issues = []
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            loc = (
                f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
            )
            self._add(
                LintSeverity.ERROR,
                f"YAML syntax error{loc}: {getattr(e, 'problem', None) or str(e)}",
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

    def _lint_project(self, p: dict[str, Any], idx: int, seen_names: set[str]):
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
                    project_name=p_name,
                )
            seen_names.add(name)

        # 1. Project-level keys validation
        for k, value in p.items():
            if k in VALID_PROJECT_KEYS:
                continue
            if k in KNOWN_SOURCE_KEYS:
                self._add(
                    LintSeverity.ERROR,
                    f"'{k}: {value}' was specified at project level, but belongs under 'source:' (e.g. source.{k}: {value}).",
                    project_name=p_name,
                    key=k,
                )
            elif k in KNOWN_ENDPOINT_KEYS:
                self._add(
                    LintSeverity.ERROR,
                    f"'{k}: {value}' was specified at project level, but belongs under '{KNOWN_ENDPOINT_KEYS[k]}:'.",
                    project_name=p_name,
                    key=k,
                )
            else:
                self._add(
                    LintSeverity.WARNING,
                    f"Unrecognized project-level option '{k}'.",
                    project_name=p_name,
                    key=k,
                )

        # 2. Redundant project-level options
        if "enabled" in p:
            val = p["enabled"]
            if not isinstance(val, bool):
                self._add(
                    LintSeverity.ERROR,
                    f"'enabled' must be a boolean (got {type(val).__name__}).",
                    project_name=p_name,
                    key="enabled",
                )
            elif val is True:
                self._add(
                    LintSeverity.REDUNDANT,
                    "'enabled: true' is redundant (default is true).",
                    project_name=p_name,
                    key="enabled",
                )

        if "preserve_description" in p:
            val = p["preserve_description"]
            if not isinstance(val, bool):
                self._add(
                    LintSeverity.ERROR,
                    f"'preserve_description' must be a boolean (got {type(val).__name__}).",
                    project_name=p_name,
                )
            elif val is True:
                self._add(
                    LintSeverity.REDUNDANT,
                    "'preserve_description: true' is redundant (default is true).",
                    project_name=p_name,
                )

        if "sync_assets" in p:
            val = p["sync_assets"]
            if isinstance(val, bool):
                if val is False:
                    self._add(
                        LintSeverity.REDUNDANT,
                        "'sync_assets: false' is redundant (default is false).",
                        project_name=p_name,
                    )
            elif isinstance(val, str):
                pass
            elif isinstance(val, (list, tuple)):
                if not all(isinstance(x, str) for x in val):
                    self._add(
                        LintSeverity.ERROR,
                        "'sync_assets' list must only contain string glob patterns.",
                        project_name=p_name,
                    )
            else:
                self._add(
                    LintSeverity.ERROR,
                    f"'sync_assets' must be a boolean, string, or list of strings (got {type(val).__name__}).",
                    project_name=p_name,
                )

        if "stream_assets" in p:
            val = p["stream_assets"]
            if not isinstance(val, bool):
                self._add(
                    LintSeverity.ERROR,
                    f"'stream_assets' must be a boolean (got {type(val).__name__}).",
                    project_name=p_name,
                )
            elif val is False:
                self._add(
                    LintSeverity.REDUNDANT,
                    "'stream_assets: false' is redundant (default is false).",
                    project_name=p_name,
                )

        for key in ("commit_message", "release_description"):
            if key in p:
                val = p[key]
                if not isinstance(val, str):
                    self._add(
                        LintSeverity.ERROR,
                        f"'{key}' must be a string (got {type(val).__name__}).",
                        project_name=p_name,
                        key=key,
                    )

        # 3. Source endpoint checks
        source = p.get("source")
        if source is None:
            self._add(
                LintSeverity.ERROR,
                "Missing required 'source' endpoint configuration.",
                project_name=p_name,
            )
        elif not isinstance(source, dict):
            self._add(
                LintSeverity.ERROR,
                f"'source' must be a dictionary (got {type(source).__name__}).",
                project_name=p_name,
            )
        else:
            self._lint_source(source, p_name)

        # 4. Target endpoint checks
        target = p.get("target")
        if target is None:
            self._add(
                LintSeverity.ERROR,
                "Missing required 'target' endpoint configuration.",
                project_name=p_name,
            )
        elif not isinstance(target, dict):
            self._add(
                LintSeverity.ERROR,
                f"'target' must be a dictionary (got {type(target).__name__}).",
                project_name=p_name,
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
                    project_name=p_name,
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
                    project_name=p_name,
                )
            else:
                self._lint_renderer(renderer, p_name)

    def _lint_source(self, s: dict[str, Any], p_name: str):
        src_type = s.get("type", "gitea")
        if src_type not in ENDPOINT_TYPES:
            self._add(
                LintSeverity.ERROR,
                f"Invalid source type '{src_type}'. "
                + f"Expected one of {sorted(ENDPOINT_TYPES)}.",
                project_name=p_name,
            )
            return

        is_remote = src_type in REMOTE_TYPES
        valid_keys = _VALID_SOURCE_KEYS_REMOTE if is_remote else _VALID_SOURCE_KEYS_LOCAL
        for k in s:
            if k in VALID_PROJECT_KEYS and k != "type":
                self._add(
                    LintSeverity.WARNING,
                    f"'{k}' was specified inside 'source:', but belongs at the project level.",
                    project_name=p_name,
                    key=k,
                )
            elif k not in valid_keys:
                self._add(
                    LintSeverity.WARNING,
                    f"Unrecognized option '{k}' under source ({src_type}).",
                    project_name=p_name,
                )

        if is_remote:
            if not s.get("repo"):
                self._add(
                    LintSeverity.ERROR,
                    "Source is missing required 'repo' string (e.g. 'owner/repo').",
                    project_name=p_name,
                )
            if not s.get("api"):
                self._add(
                    LintSeverity.ERROR,
                    "Source is missing required 'api' URL.",
                    project_name=p_name,
                )
            if not s.get("token"):
                self._add(
                    LintSeverity.ERROR,
                    "Source is missing required 'token'.",
                    project_name=p_name,
                )

            mode = s.get("mode", "release")
            if mode not in ("release", "tag", "commit"):
                self._add(
                    LintSeverity.ERROR,
                    f"Invalid source mode '{mode}'. Expected 'release', 'tag', or 'commit'.",
                    project_name=p_name,
                )
            elif "mode" in s and s["mode"] == "release":
                self._add(
                    LintSeverity.REDUNDANT,
                    "'source.mode: release' is redundant (default is release).",
                    project_name=p_name,
                )

            if "include_prereleases" in s:
                val = s["include_prereleases"]
                if not isinstance(val, bool):
                    self._add(
                        LintSeverity.ERROR,
                        f"'source.include_prereleases' must be a boolean (got {type(val).__name__}).",
                        project_name=p_name,
                    )
                elif val is False:
                    self._add(
                        LintSeverity.REDUNDANT,
                        "'source.include_prereleases: false' is redundant (default is false).",
                        project_name=p_name,
                    )

            if "include_drafts" in s:
                val = s["include_drafts"]
                if not isinstance(val, bool):
                    self._add(
                        LintSeverity.ERROR,
                        f"'source.include_drafts' must be a boolean (got {type(val).__name__}).",
                        project_name=p_name,
                    )
                elif val is False:
                    self._add(
                        LintSeverity.REDUNDANT,
                        "'source.include_drafts: false' is redundant (default is false).",
                        project_name=p_name,
                    )

            if "sync_from" in s and s["sync_from"] == "":
                self._add(
                    LintSeverity.REDUNDANT,
                    "'source.sync_from: \"\"' is redundant (default is empty).",
                    project_name=p_name,
                )
        else:
            # Local source
            if not s.get("path"):
                self._add(
                    LintSeverity.ERROR,
                    "Local source is missing required 'path'.",
                    project_name=p_name,
                )
            if "tag_pattern" in s and s["tag_pattern"] == "*":
                self._add(
                    LintSeverity.REDUNDANT,
                    "'source.tag_pattern: \"*\"' is redundant (default is \"*\").",
                        project_name=p_name,
                    )

    def _lint_target(self, t: dict[str, Any], p_name: str):
        tgt_type = t.get("type", "github")
        if tgt_type not in ENDPOINT_TYPES:
            self._add(
                LintSeverity.ERROR,
                f"Invalid target type '{tgt_type}'. "
                + f"Expected one of {sorted(ENDPOINT_TYPES)}.",
                project_name=p_name,
            )
            return

        is_remote = tgt_type in REMOTE_TYPES
        valid_keys = _VALID_TARGET_KEYS_REMOTE if is_remote else _VALID_TARGET_KEYS_LOCAL
        for k in t:
            if k in VALID_PROJECT_KEYS and k != "type":
                self._add(
                    LintSeverity.WARNING,
                    f"'{k}' was specified inside 'target:', but belongs at the project level.",
                    project_name=p_name,
                    key=k,
                )
            elif k in KNOWN_SOURCE_KEYS:
                self._add(
                    LintSeverity.ERROR,
                    f"'{k}' was specified inside 'target:', but belongs under 'source:'.",
                    project_name=p_name,
                    key=k,
                )
            elif k not in valid_keys:
                self._add(
                    LintSeverity.WARNING,
                    f"Unrecognized option '{k}' under target ({tgt_type}).",
                    project_name=p_name,
                )

        if is_remote:
            if not t.get("repo"):
                self._add(
                    LintSeverity.ERROR,
                    "Target is missing required 'repo' string (e.g. 'owner/repo').",
                    project_name=p_name,
                )
            if not t.get("api"):
                self._add(
                    LintSeverity.ERROR,
                    "Target is missing required 'api' URL.",
                    project_name=p_name,
                )
            if not t.get("token"):
                self._add(
                    LintSeverity.ERROR,
                    "Target is missing required 'token'.",
                    project_name=p_name,
                )

        if "branch" in t and t["branch"] == "main":
            self._add(
                LintSeverity.REDUNDANT,
                "'target.branch: main' is redundant (default is main).",
                project_name=p_name,
            )

    def _lint_retry(self, r: dict[str, Any], p_name: str):
        valid_retry_keys = {"max_attempts", "backoff_seconds"}
        for k in r:
            if k not in valid_retry_keys:
                self._add(
                    LintSeverity.WARNING,
                    f"Unrecognized option '{k}' under retry.",
                    project_name=p_name,
                )

        max_att = r.get("max_attempts")
        backoff = r.get("backoff_seconds")

        if max_att is not None:
            if not isinstance(max_att, int) or max_att < 1:
                self._add(
                    LintSeverity.ERROR,
                    f"'retry.max_attempts' must be an integer >= 1 (got {max_att}).",
                    project_name=p_name,
                )
            elif max_att == 3:
                self._add(
                    LintSeverity.REDUNDANT,
                    "'retry.max_attempts: 3' is redundant (default is 3).",
                    project_name=p_name,
                )

        if backoff is not None:
            if not isinstance(backoff, (int, float)) or backoff < 0:
                self._add(
                    LintSeverity.ERROR,
                    f"'retry.backoff_seconds' must be a number >= 0 (got {backoff}).",
                    project_name=p_name,
                )
            elif backoff == 2:
                self._add(
                    LintSeverity.REDUNDANT,
                    "'retry.backoff_seconds: 2' is redundant (default is 2).",
                    project_name=p_name,
                )

    def _lint_renderer(self, ren: dict[str, Any], p_name: str):
        valid_renderer_keys = {"ignore", "operations", "author"}
        for k in ren:
            if k not in valid_renderer_keys:
                self._add(
                    LintSeverity.WARNING,
                    f"Unrecognized option '{k}' under renderer.",
                    project_name=p_name,
                )

        if "ignore" in ren:
            ig = ren["ignore"]
            if not isinstance(ig, (list, tuple)):
                self._add(
                    LintSeverity.ERROR,
                    f"'renderer.ignore' must be a list of patterns (got {type(ig).__name__}).",
                    project_name=p_name,
                )

        if "author" in ren:
            auth = ren["author"]
            if not isinstance(auth, dict):
                self._add(
                    LintSeverity.ERROR,
                    f"'renderer.author' must be a dictionary (got {type(auth).__name__}).",
                    project_name=p_name,
                )

        if "operations" in ren:
            ops = ren["operations"]
            if not isinstance(ops, list):
                self._add(
                    LintSeverity.ERROR,
                    f"'renderer.operations' must be a list of operation maps (got {type(ops).__name__}).",
                    project_name=p_name,
                )
            else:
                self._lint_operations(ops, p_name)

    def _lint_operations(self, ops: list[Any], p_name: str):
        valid_op_types = {"remove", "rename", "replace", "add", "validate"}
        for idx, op in enumerate(ops):
            if not isinstance(op, dict):
                self._add(
                    LintSeverity.ERROR,
                    f"Operation at index {idx} must be a dictionary (got {type(op).__name__}).",
                    project_name=p_name,
                )
                continue
            for op_type, items in op.items():
                if op_type not in valid_op_types:
                    self._add(
                        LintSeverity.ERROR,
                        f"Unknown renderer operation '{op_type}' at index {idx}. Valid operations: {', '.join(sorted(valid_op_types))}.",
                        project_name=p_name,
                    )
                    continue
                if not isinstance(items, list):
                    self._add(
                        LintSeverity.ERROR,
                        f"Renderer operation '{op_type}' must contain a list of actions.",
                        project_name=p_name,
                    )
                    continue

                if op_type == "remove":
                    for item in items:
                        if not isinstance(item, dict) or not item.get("path"):
                            self._add(
                                LintSeverity.ERROR,
                                "Remove operation item requires 'path'.",
                                project_name=p_name,
                            )
                        pat = item.get("pattern")
                        if pat == "literal":
                            self._add(
                                LintSeverity.REDUNDANT,
                                "'pattern: literal' in remove operation is redundant (default is literal).",
                                project_name=p_name,
                            )
                        elif pat and pat not in ("literal", "glob", "regex"):
                            self._add(
                                LintSeverity.ERROR,
                                f"Invalid remove pattern mode '{pat}'. Expected 'literal', 'glob', or 'regex'.",
                                project_name=p_name,
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
                                project_name=p_name,
                            )
                        pat = item.get("pattern")
                        if pat == "literal":
                            self._add(
                                LintSeverity.REDUNDANT,
                                "'pattern: literal' in rename operation is redundant (default is literal).",
                                project_name=p_name,
                            )
                        elif pat and pat != "literal":
                            self._add(
                                LintSeverity.ERROR,
                                f"Rename pattern mode '{pat}' is not supported — only 'literal' is supported.",
                                project_name=p_name,
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
                                project_name=p_name,
                            )
                        if not isinstance(item, dict):
                            continue
                        pat = item.get("pattern")
                        if pat == "literal":
                            self._add(
                                LintSeverity.REDUNDANT,
                                "'pattern: literal' in replace operation is redundant (default is literal).",
                                project_name=p_name,
                            )
                        elif pat and pat not in ("literal", "regex"):
                            self._add(
                                LintSeverity.ERROR,
                                f"Invalid replace pattern mode '{pat}'. Expected 'literal' or 'regex'.",
                                project_name=p_name,
                            )
                        if item.get("case_sensitive") is True:
                            self._add(
                                LintSeverity.REDUNDANT,
                                "'case_sensitive: true' in replace operation is redundant (default is true).",
                                project_name=p_name,
                            )
                        if item.get("match_case") is False:
                            self._add(
                                LintSeverity.REDUNDANT,
                                "'match_case: false' in replace operation is redundant (default is false).",
                                project_name=p_name,
                            )
                        for key in ("case_sensitive", "match_case"):
                            if key in item and not isinstance(item[key], bool):
                                self._add(
                                    LintSeverity.ERROR,
                                    f"Replace option '{key}' must be a boolean (true/false).",
                                    project_name=p_name,
                                )

                elif op_type == "add":
                    for item in items:
                        if not isinstance(item, dict):
                            self._add(
                                LintSeverity.ERROR,
                                "Add operation item requires 'path' and either 'content' or 'src'.",
                                project_name=p_name,
                            )
                            continue
                        if not item.get("path") or (
                            item.get("content") is None and not item.get("src")
                        ):
                            self._add(
                                LintSeverity.ERROR,
                                "Add operation item requires 'path' and either 'content' or 'src'.",
                                project_name=p_name,
                            )
                        if item.get("content") is not None and not isinstance(
                            item["content"], str
                        ):
                            self._add(
                                LintSeverity.ERROR,
                                "Add option 'content' must be a string.",
                                project_name=p_name,
                            )
                        if item.get("src") is not None and not isinstance(
                            item["src"], str
                        ):
                            self._add(
                                LintSeverity.ERROR,
                                "Add option 'src' must be a string path to an existing file.",
                                project_name=p_name,
                            )
                        if item.get("content") is not None and item.get("src"):
                            self._add(
                                LintSeverity.REDUNDANT,
                                "'src' is ignored when 'content' is present in add operation.",
                                project_name=p_name,
                            )

                elif op_type == "validate":  # pragma: no branch — reaching here implies validate (dead false-arc)
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
                                project_name=p_name,
                            )
                        if not isinstance(item, dict):
                            continue
                        asrt = item.get("assert")
                        if asrt not in valid_asserts:
                            self._add(
                                LintSeverity.ERROR,
                                f"Invalid validate assert '{asrt}'. Expected one of {', '.join(sorted(valid_asserts))}.",
                                project_name=p_name,
                            )
                        if asrt in ("string_exists", "string_absent") and not item.get(
                            "pattern"
                        ):
                            self._add(
                                LintSeverity.ERROR,
                                f"Validate assert '{asrt}' requires 'pattern'.",
                                project_name=p_name,
                            )
                        if item.get("case_sensitive") is True:
                            self._add(
                                LintSeverity.REDUNDANT,
                                "'case_sensitive: true' in validate operation is redundant (default is true).",
                                project_name=p_name,
                            )
                        if "case_sensitive" in item and not isinstance(
                            item["case_sensitive"], bool
                        ):
                            self._add(
                                LintSeverity.ERROR,
                                "Validate option 'case_sensitive' must be a boolean (true/false).",
                                project_name=p_name,
                            )
                        if asrt not in ("string_exists", "string_absent") and (
                            "case_sensitive" in item
                        ):
                            self._add(
                                LintSeverity.WARNING,
                                f"'case_sensitive' only applies to string_exists/string_absent (ignored for '{asrt}').",
                                project_name=p_name,
                            )



def lint_config(
    config: str | Path | io.TextIOBase | io.BufferedIOBase,
    print_output: bool = True,
) -> LintReport:
    """Lint a config (path or open text file-like object) and optionally print the results."""
    linter = ConfigLinter()
    report = linter.lint_file(config)
    if print_output:
        print(report.format_text())
    return report



