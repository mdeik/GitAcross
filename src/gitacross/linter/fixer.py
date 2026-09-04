"""Config fixer — rewrites configs to remove redundant/misplaced options.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..config import KNOWN_SOURCE_KEYS, VALID_PROJECT_KEYS
from .models import FixIssue, FixReport, _CleanDumper


class ConfigFixer:
    def __init__(self):
        self.fixes: list[FixIssue] = []

    def __repr__(self):
        return f"ConfigFixer(fixes={len(self.fixes)})"

    def _add(self, message: str, project_name: str | None = None):
        self.fixes.append(FixIssue(message=message, project_name=project_name))

    def fix_yaml_string(self, content: str) -> FixReport:
        # Fresh accumulator — reusing an instance must not leak prior fixes
        self.fixes = []
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

        for _idx, p in enumerate(projects_list):
            if isinstance(p, dict):
                self._fix_project(p)

        fixed_yaml = yaml.dump(
            data,
            Dumper=_CleanDumper,
            sort_keys=False,
            default_flow_style=False,
        )
        return FixReport(fixes=self.fixes, content=fixed_yaml, is_valid=True)

    def _fix_project(self, p: dict[str, Any]):
        p_name = p.get("name") or "<unnamed>"

        # 1. Misplaced endpoint keys at project level -> move to source
        for k in list(p.keys()):
            if k in KNOWN_SOURCE_KEYS:
                val = p.pop(k)
                if "source" not in p or not isinstance(p["source"], dict):
                    p["source"] = {}
                if k not in p["source"]:
                    p["source"][k] = val
                    self._add(
                        f"Moved '{k}: {val}' from project level into 'source:'",
                        project_name=p_name,
                    )
                else:
                    self._add(
                        f"Removed duplicate project-level '{k}' (already present under source)",
                        project_name=p_name,
                    )

        # 2. Misplaced project-level keys inside source -> move to project level
        if isinstance(p.get("source"), dict):
            for k in list(p["source"].keys()):
                if k in VALID_PROJECT_KEYS and k != "type":
                    val = p["source"].pop(k)
                    if k not in p:
                        p[k] = val
                        self._add(
                            f"Moved '{k}' from 'source:' to project level",
                            project_name=p_name,
                        )
                    else:
                        self._add(
                            f"Removed duplicate '{k}' from 'source:' (already present at project level)",
                            project_name=p_name,
                        )

        # 3. Misplaced project keys in target -> move to project level
        #    and misplaced source keys in target -> move to source
        if isinstance(p.get("target"), dict):
            for k in list(p["target"].keys()):
                if k in VALID_PROJECT_KEYS and k != "type":
                    val = p["target"].pop(k)
                    if k not in p:
                        p[k] = val
                        self._add(
                            f"Moved '{k}' from 'target:' to project level",
                            project_name=p_name,
                        )
                    else:
                        self._add(
                            f"Removed duplicate '{k}' from 'target:' (already present at project level)",
                            project_name=p_name,
                        )
                elif k in KNOWN_SOURCE_KEYS:
                    val = p["target"].pop(k)
                    if "source" not in p or not isinstance(p["source"], dict):
                        p["source"] = {}
                    if k not in p["source"]:
                        p["source"][k] = val
                        self._add(
                            f"Moved '{k}: {val}' from 'target:' into 'source:'",
                            project_name=p_name,
                        )
                    else:
                        self._add(
                            f"Removed duplicate '{k}' from 'target:' (already present under source)",
                            project_name=p_name,
                        )

        # 4. Redundant project-level options
        if p.get("enabled") is True:
            del p["enabled"]
            self._add("Removed redundant 'enabled: true'", project_name=p_name)

        for key in ("preserve_description",):
            if p.get(key) is True:
                del p[key]
                self._add(f"Removed redundant '{key}: true'", project_name=p_name)

        for key in ("sync_assets",):
            if p.get(key) is False:
                del p[key]
                self._add(f"Removed redundant '{key}: false'", project_name=p_name)

        if p.get("stream_assets") is False:
            del p["stream_assets"]
            self._add("Removed redundant 'stream_assets: false'", project_name=p_name)

        # 3. Source endpoint fixes
        if isinstance(p.get("source"), dict):
            src = p["source"]
            if src.get("mode") == "release":
                del src["mode"]
                self._add(
                    "Removed redundant 'source.mode: release'", project_name=p_name
                )
            if src.get("include_prereleases") is False:
                del src["include_prereleases"]
                self._add(
                    "Removed redundant 'source.include_prereleases: false'",
                    project_name=p_name,
                )
            if src.get("include_drafts") is False:
                del src["include_drafts"]
                self._add(
                    "Removed redundant 'source.include_drafts: false'",
                    project_name=p_name,
                )
            if src.get("sync_from") == "":
                del src["sync_from"]
                self._add(
                    "Removed redundant 'source.sync_from: \"\"'",
                    project_name=p_name,
                )
            if src.get("tag_pattern") == "*":
                del src["tag_pattern"]
                self._add(
                    "Removed redundant 'source.tag_pattern: \"*\"'",
                    project_name=p_name,
                )

        # 4. Target endpoint fixes
        if isinstance(p.get("target"), dict):
            tgt = p["target"]
            if tgt.get("branch") == "main":
                del tgt["branch"]
                self._add(
                    "Removed redundant 'target.branch: main'", project_name=p_name
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
                    project_name=p_name,
                )
            else:
                self._add(
                    "Removed redundant retry defaults", project_name=p_name
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
                                        project_name=p_name,
                                    )
                                if op_type == "replace":
                                    if item.get("case_sensitive") is True:
                                        del item["case_sensitive"]
                                        self._add(
                                            "Removed redundant 'case_sensitive: true' in replace operation",
                                            project_name=p_name,
                                        )
                                    if item.get("match_case") is False:
                                        del item["match_case"]
                                        self._add(
                                            "Removed redundant 'match_case: false' in replace operation",
                                            project_name=p_name,
                                        )
                                if op_type == "validate" and item.get(
                                    "case_sensitive"
                                ) is True:
                                    del item["case_sensitive"]
                                    self._add(
                                        "Removed redundant 'case_sensitive: true' in validate operation",
                                        project_name=p_name,
                                    )
                                if (
                                    op_type == "add"
                                    and item.get("content") is not None
                                    and item.get("src")
                                ):
                                    del item["src"]
                                    self._add(
                                        "Removed redundant 'src' in add operation ('content' takes precedence)",
                                        project_name=p_name,
                                    )



def fix_config(
    config: str | Path,
    write_back: bool = True,
    print_output: bool = True,
    dry_run: bool = False,
) -> FixReport:
    """Fix misplaced and redundant options in config file.

    Path-only: unlike :func:`lint_config`, this writes the fixed YAML back to
    the file (when *write_back* is true), so it requires a real file path.
    Pass ``dry_run=True`` to preview the fixes without writing anything.
    """
    path = Path(config)
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
    if report.is_valid and write_back and not dry_run and report.fixes:
        _ = path.write_text(report.content, encoding="utf-8")
    if print_output:
        if dry_run and report.fixes:
            print(f"[DRY-RUN] Would apply {len(report.fixes)} fix(es):")
        print(report.format_text())
    return report


