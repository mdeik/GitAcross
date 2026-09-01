"""Tests for config linting and fixing — split from the original single-file suite."""
from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path
from unittest import mock

# ---------------------------------------------------------------------------
# Linter Tests
# ---------------------------------------------------------------------------


def test_linter_bare_projects_key():
    from gitacross.linter import ConfigLinter

    linter = ConfigLinter()
    report = linter.lint_yaml_string("projects:\n")
    assert not report.is_valid
    assert any("must contain a list" in e.message for e in report.errors)

    print("  ✓ linter: bare 'projects:' key reported without crashing")


def test_linter_unknown_operation():
    from gitacross.linter import ConfigLinter

    linter = ConfigLinter()
    report = linter.lint_yaml_string("""
- name: bad-op
  renderer:
    operations:
      - explode: [{path: x}]
""")
    assert not report.is_valid
    assert any("explode" in e.message for e in report.errors)

    print("  ✓ linter: flags unknown renderer operations")


def test_linter_top_level_and_project_shape_errors():
    """Linter flags empty configs, bad top-level shapes, and non-dict projects."""
    from gitacross.linter import ConfigLinter

    linter = ConfigLinter()

    # Empty config → warning only
    report = linter.lint_yaml_string("")
    assert report.is_valid
    assert len(report.warnings) == 1
    assert "empty" in report.warnings[0].message

    # Scalar top level → error
    report = linter.lint_yaml_string("42")
    assert not report.is_valid
    assert "top-level" in report.errors[0].message.lower()

    # Map without a 'projects' key → error
    report = linter.lint_yaml_string("foo: bar")
    assert not report.is_valid

    # 'projects' not a list → error
    report = linter.lint_yaml_string("projects: nope")
    assert not report.is_valid

    # Project entry that is not a map → error
    report = linter.lint_yaml_string("projects:\n  - just-a-string")
    assert not report.is_valid
    assert "not a valid map/dict" in report.errors[0].message

    print("  ✓ linter: flags empty configs, bad top-level shapes, and non-dict projects")


def test_linter_project_level_endpoint_keys_and_enabled_type():
    """Endpoint keys at project level and non-boolean 'enabled' are flagged."""
    from gitacross.linter import ConfigLinter

    linter = ConfigLinter()
    report = linter.lint_yaml_string("""
- name: p
  repo: owner/repo
  enabled: "yes"
  source: {type: local, path: /a}
  target: {type: local, path: /b}
""")
    assert not report.is_valid
    msgs = " | ".join(e.message for e in report.errors)
    assert "belongs under 'source or target:'" in msgs
    assert "'enabled' must be a boolean" in msgs
    print("  ✓ linter: flags project-level endpoint keys and non-boolean enabled")


def test_linter_validate_operation_errors():
    """Validate ops must have a path, a known assert, and pattern where required."""
    from gitacross.linter import ConfigLinter

    linter = ConfigLinter()
    report = linter.lint_yaml_string("""
- name: p
  source: {type: local, path: /a}
  target: {type: local, path: /b}
  renderer:
    operations:
      - validate: [{assert: file_exists}]
      - validate: [{path: x, assert: banana}]
      - validate: [{path: x, assert: string_exists}]
""")
    assert not report.is_valid
    msgs = " | ".join(e.message for e in report.errors)
    assert "requires 'path'" in msgs
    assert "Invalid validate assert 'banana'" in msgs
    assert "requires 'pattern'" in msgs
    print("  ✓ linter: validate ops require path/assert/pattern correctly")


def test_lint_issue_and_report_formatting():
    """LintIssue strings and LintReport.format_text group by project."""
    from gitacross.linter import LintIssue, LintReport, LintSeverity

    issue_plain = LintIssue(severity=LintSeverity.ERROR, message="boom")
    assert "[ERROR] boom" in str(issue_plain)

    issue_proj = LintIssue(
        severity=LintSeverity.WARNING, message="careful", project="p1"
    )
    assert "(project 'p1')" in str(issue_proj)

    text = LintReport(issues=[issue_proj, issue_plain]).format_text()
    assert "Project 'p1'" in text
    assert "[WARNING] careful" in text
    assert "[ERROR] boom" in text
    assert "Summary:" in text
    print("  ✓ linter: issue strings and report formatting group by project")


def test_fixer_duplicate_keys_and_redundant_tag_pattern():
    """Fixer removes project/source duplicates and redundant tag_pattern."""
    from gitacross.linter import ConfigFixer

    fixer = ConfigFixer()
    report = fixer.fix_yaml_string("""
- name: p
  sync_from: v1
  tag_pattern: "*"
  source:
    type: local
    path: /a
    tag_pattern: "*"
    sync_from: v2
  target:
    type: local
    path: /b
""")
    assert report.is_valid
    msgs = " | ".join(f.message for f in report.fixes)
    assert "Removed duplicate project-level 'sync_from'" in msgs
    assert "Removed duplicate project-level 'tag_pattern'" in msgs
    assert "Removed redundant 'source.tag_pattern: \"*\"'" in msgs
    print("  ✓ fixer: removes duplicate keys and redundant tag_pattern")


def test_linter_and_fixer_instances_are_reusable():
    """Reusing a ConfigLinter/ConfigFixer must not leak prior results."""
    from gitacross.linter import ConfigFixer, ConfigLinter

    linter = ConfigLinter()
    r1 = linter.lint_yaml_string("42")  # invalid top-level → error
    assert not r1.is_valid and len(r1.errors) == 1
    r2 = linter.lint_yaml_string("projects: []")  # clean config
    assert r2.is_valid
    assert len(r2.errors) == 0 and len(r2.warnings) == 0

    fixer = ConfigFixer()
    f1 = fixer.fix_yaml_string(
        "- name: p\n"
        + "  enabled: true\n"
        + "  source: {type: local, path: /a}\n"
        + "  target: {type: local, path: /b}\n"
    )
    assert len(f1.fixes) >= 1  # removes redundant 'enabled: true'
    f2 = fixer.fix_yaml_string("projects: []")
    assert len(f2.fixes) == 0  # would have leaked without the reset
    print("  ✓ linter/fixer: instances are reusable without state leaking")


def test_linter_invalid_yaml():
    from gitacross.linter import ConfigLinter

    linter = ConfigLinter()
    report = linter.lint_yaml_string("projects: [invalid yaml: {")
    assert not report.is_valid
    assert len(report.errors) == 1
    assert "YAML syntax error" in report.errors[0].message
    print("  ✓ linter: detects invalid YAML syntax")


def test_linter_empty_entry_and_duplicate_names():
    from gitacross.linter import ConfigLinter

    linter = ConfigLinter()
    report = linter.lint_yaml_string("""
- {}
- name: dup
  source: {type: local, path: /a}
  target: {type: local, path: /b}
- name: dup
  source: {type: local, path: /c}
  target: {type: local, path: /d}
""")
    assert not report.is_valid
    msgs = [e.message for e in report.errors]
    assert any("missing required 'name'" in m for m in msgs)
    assert any("Duplicate project name 'dup'" in m for m in msgs)

    print("  ✓ linter: flags empty entries and duplicate project names")


def test_linter_missing_required_fields():
    from gitacross.linter import ConfigLinter

    linter = ConfigLinter()
    report = linter.lint_yaml_string("""
- name: missing-endpoints
- name: bad-source
  source:
    type: gitea
  target:
    type: github
""")
    assert not report.is_valid
    error_messages = [e.message.lower() for e in report.errors]
    assert any("missing required 'source'" in m for m in error_messages)
    assert any("missing required 'target'" in m for m in error_messages)
    assert any("missing required 'repo'" in m for m in error_messages)
    print("  ✓ linter: detects missing required fields")


def test_linter_misplaced_and_unknown_keys():
    from gitacross.linter import ConfigLinter

    linter = ConfigLinter()
    report = linter.lint_yaml_string("""
- name: misplaced-app
  mode: commit
  unknown_project_opt: true
  source:
    type: gitea
    repo: a/b
    api: https://git.example.com
    token: tok
  target:
    type: github
    repo: a/b
    api: https://api.github.com
    token: tok
""")
    assert not report.is_valid
    assert any("mode: commit" in e.message and "belongs under 'source:'" in e.message for e in report.errors)
    assert any("Unrecognized project-level option 'unknown_project_opt'" in w.message for w in report.warnings)
    print("  ✓ linter: detects misplaced and unrecognized keys")


def test_linter_redundant_options():
    from gitacross.linter import ConfigLinter

    linter = ConfigLinter()
    report = linter.lint_yaml_string("""
- name: redundant-app
  enabled: true
  preserve_description: true
  sync_assets: false
  stream_assets: false
  source:
    type: gitea
    repo: a/b
    api: https://git.example.com
    token: tok
    mode: release
    include_prereleases: false
    include_drafts: false
    sync_from: ""
  target:
    type: github
    repo: a/b
    api: https://api.github.com
    token: tok
    branch: main
  retry:
    max_attempts: 3
    backoff_seconds: 2
  renderer:
    operations:
      - remove:
          - path: .gitea
            pattern: literal
      - rename:
          - from: old.txt
            to: new.txt
            pattern: literal
      - replace:
          - search: foo
            replace: bar
            pattern: literal
""")
    assert report.is_valid  # redundant options are not errors
    red_msgs = [r.message for r in report.redundant]
    assert any("'enabled: true' is redundant" in m for m in red_msgs)
    assert any("'preserve_description: true' is redundant" in m for m in red_msgs)
    assert any("'sync_assets: false' is redundant" in m for m in red_msgs)
    assert any("'stream_assets: false' is redundant" in m for m in red_msgs)
    assert any("'source.mode: release' is redundant" in m for m in red_msgs)
    assert any("'source.include_prereleases: false' is redundant" in m for m in red_msgs)
    assert any("'source.include_drafts: false' is redundant" in m for m in red_msgs)
    assert any("'target.branch: main' is redundant" in m for m in red_msgs)
    assert any("'retry.max_attempts: 3' is redundant" in m for m in red_msgs)
    assert any("'retry.backoff_seconds: 2' is redundant" in m for m in red_msgs)
    assert any("'pattern: literal' in remove operation is redundant" in m for m in red_msgs)
    assert any("'pattern: literal' in rename operation is redundant" in m for m in red_msgs)
    assert any("'pattern: literal' in replace operation is redundant" in m for m in red_msgs)
    print("  ✓ linter: flags redundant defaults")


def test_linter_replace_case_options():
    from gitacross.linter import ConfigFixer, ConfigLinter

    linter = ConfigLinter()
    report = linter.lint_yaml_string("""
- name: case-app
  source:
    type: gitea
    repo: a/b
    api: https://git.example.com
    token: tok
  target:
    type: github
    repo: a/b
    api: https://api.github.com
    token: tok
  renderer:
    operations:
      - replace:
          - search: Gitea
            replace: GitHub
            case_sensitive: true
            match_case: false
          - search: gitea
            replace: github
            case_sensitive: "yes"
          - search: gitea
            replace: github
            match_case: "on"
""")

    assert not report.is_valid
    msgs = [i.message for i in report.errors]
    assert any(
        "Replace option 'case_sensitive' must be a boolean" in m for m in msgs
    )
    assert any("Replace option 'match_case' must be a boolean" in m for m in msgs)

    red_msgs = [r.message for r in report.redundant]
    assert any("'case_sensitive: true' in replace operation is redundant" in m for m in red_msgs)
    assert any("'match_case: false' in replace operation is redundant" in m for m in red_msgs)

    fixer = ConfigFixer()
    fix_report = fixer.fix_yaml_string("""
- name: case-app
  source:
    type: gitea
    repo: a/b
    api: https://git.example.com
    token: tok
  target:
    type: github
    repo: a/b
    api: https://api.github.com
    token: tok
  renderer:
    operations:
      - replace:
          - search: Gitea
            replace: GitHub
            case_sensitive: true
            match_case: false
""")
    assert fix_report.is_valid
    assert any(
        "Removed redundant 'case_sensitive: true' in replace operation"
        in f.message
        for f in fix_report.fixes
    )
    assert any(
        "Removed redundant 'match_case: false' in replace operation"
        in f.message
        for f in fix_report.fixes
    )
    assert "case_sensitive" not in fix_report.content
    assert "match_case" not in fix_report.content

    print("  ✓ linter: replace case_sensitive/match_case validation")


def test_linter_validate_case_options():
    from gitacross.linter import ConfigFixer, ConfigLinter

    linter = ConfigLinter()
    report = linter.lint_yaml_string("""
- name: validate-app
  source:
    type: gitea
    repo: a/b
    api: https://git.example.com
    token: tok
  target:
    type: github
    repo: a/b
    api: https://api.github.com
    token: tok
  renderer:
    operations:
      - validate:
          - assert: string_exists
            path: README.md
            pattern: MIT
            case_sensitive: true
          - assert: string_absent
            path: LICENSE
            pattern: gitea
            case_sensitive: "no"
          - assert: file_exists
            path: README.md
            case_sensitive: false
""")

    assert not report.is_valid
    msgs = [i.message for i in report.errors]
    assert any(
        "Validate option 'case_sensitive' must be a boolean" in m for m in msgs
    )

    red_msgs = [r.message for r in report.redundant]
    assert any(
        "'case_sensitive: true' in validate operation is redundant" in m
        for m in red_msgs
    )

    warn_msgs = [w.message for w in report.warnings]
    assert any(
        "'case_sensitive' only applies to string_exists/string_absent" in m
        for m in warn_msgs
    )

    fixer = ConfigFixer()
    fix_report = fixer.fix_yaml_string("""
- name: validate-app
  source:
    type: gitea
    repo: a/b
    api: https://git.example.com
    token: tok
  target:
    type: github
    repo: a/b
    api: https://api.github.com
    token: tok
  renderer:
    operations:
      - validate:
          - assert: string_exists
            path: README.md
            pattern: MIT
            case_sensitive: true
""")
    assert fix_report.is_valid
    assert any(
        "Removed redundant 'case_sensitive: true' in validate operation"
        in f.message
        for f in fix_report.fixes
    )
    assert "case_sensitive" not in fix_report.content

    print("  ✓ linter: validate case_sensitive validation")


def test_linter_cli_flag():
    from gitacross.linter import lint_config

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        _ = f.write("""
- name: test-app
  source:
    type: gitea
    repo: a/b
    api: https://git.example.com
    token: tok
  target:
    type: github
    repo: a/b
    api: https://api.github.com
    token: tok
""")
        config_path = f.name

    try:
        report = lint_config(config_path, print_output=False)
        assert report.is_valid
        assert len(report.errors) == 0
        print("  ✓ linter: lint_config CLI helper")
    finally:
        os.unlink(config_path)


def test_config_fixer():
    import yaml

    from gitacross.linter import fix_config, lint_config

    raw_yaml = """
- name: messy-app
  mode: commit
  sync_from: ""
  enabled: true
  preserve_description: true
  sync_assets: false
  stream_assets: false
  source:
    type: gitea
    repo: a/b
    api: https://git.example.com
    token: tok
    include_prereleases: false
    include_drafts: false
  target:
    type: github
    repo: a/b
    api: https://api.github.com
    token: tok
    branch: main
  retry:
    max_attempts: 3
    backoff_seconds: 2
  renderer:
    operations:
      - remove:
          - path: .gitea
            pattern: literal
      - rename:
          - from: old.txt
            to: new.txt
            pattern: literal
      - replace:
          - search: foo
            replace: bar
            pattern: literal
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        _ = f.write(raw_yaml)
        config_path = f.name

    try:
        report = fix_config(config_path, write_back=True, print_output=False)
        assert report.is_valid
        assert len(report.fixes) > 0

        # Verify the file is fixed
        fixed_data = yaml.safe_load(Path(config_path).read_text())
        p = fixed_data[0]

        # 1. mode was moved into source
        assert "mode" not in p
        assert p["source"]["mode"] == "commit"

        # 2. redundant fields are stripped
        assert "enabled" not in p
        assert "preserve_description" not in p
        assert "sync_assets" not in p
        assert "stream_assets" not in p
        assert "sync_from" not in p
        assert "include_prereleases" not in p["source"]
        assert "include_drafts" not in p["source"]
        assert "branch" not in p["target"]
        assert "retry" not in p
        assert "pattern" not in p["renderer"]["operations"][0]["remove"][0]
        assert "pattern" not in p["renderer"]["operations"][1]["rename"][0]
        assert "pattern" not in p["renderer"]["operations"][2]["replace"][0]

        # 3. Linter on fixed file should report 0 errors, 0 warnings, 0 redundant
        lint_rep = lint_config(config_path, print_output=False)
        assert lint_rep.is_valid
        assert len(lint_rep.errors) == 0
        assert len(lint_rep.warnings) == 0
        assert len(lint_rep.redundant) == 0

        print("  ✓ linter: ConfigFixer automatically cleans and fixes configs")
    finally:
        os.unlink(config_path)


def test_config_fixer_vice_versa():
    """Test that options misplaced in source or target are moved to project level or correct endpoint."""
    import yaml

    from gitacross.linter import fix_config, lint_config

    raw_yaml = """
- name: misplaced-in-endpoints
  source:
    type: gitea
    repo: a/b
    api: https://git.example.com
    token: tok
    sync_assets:
      - "*.tar.gz"
    stream_assets: true
  target:
    type: github
    repo: a/b
    api: https://api.github.com
    token: tok
    mode: commit
    retry:
      max_attempts: 5
      backoff_seconds: 4
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        _ = f.write(raw_yaml)
        config_path = f.name

    try:
        report = fix_config(config_path, write_back=True, print_output=False)
        assert report.is_valid
        assert len(report.fixes) > 0

        # Verify the file is fixed
        fixed_data = yaml.safe_load(Path(config_path).read_text())
        p = fixed_data[0]

        # sync_assets and stream_assets moved from source to project level
        assert "sync_assets" not in p["source"]
        assert "stream_assets" not in p["source"]
        assert p["sync_assets"] == ["*.tar.gz"]
        assert p["stream_assets"] is True

        # retry moved from target to project level
        assert "retry" not in p["target"]
        assert p["retry"]["max_attempts"] == 5
        assert p["retry"]["backoff_seconds"] == 4

        # mode: commit moved from target to source
        assert "mode" not in p["target"]
        assert p["source"]["mode"] == "commit"

        # Linter passes
        lint_rep = lint_config(config_path, print_output=False)
        assert lint_rep.is_valid
        assert len(lint_rep.errors) == 0
        assert len(lint_rep.warnings) == 0

        print("  ✓ linter: ConfigFixer vice-versa moves endpoint-misplaced options")
    finally:
        os.unlink(config_path)


def test_fix_config_missing_file_prints():
    """fix_config on a missing file prints the error report."""
    from gitacross.linter import fix_config

    captured = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "nope.yml"
        with mock.patch("sys.stdout", new=captured):
            report = fix_config(str(missing))
    assert not report.is_valid
    assert "File not found" in captured.getvalue()
    print("  ✓ fix: fix_config prints the missing-file error")

def test_lint_file_unreadable():
    """lint_file on an unreadable path (e.g. a directory) reports an error."""
    from gitacross.linter import ConfigLinter

    linter = ConfigLinter()
    with tempfile.TemporaryDirectory() as tmp:
        report = linter.lint_file(tmp)  # a directory → read_text raises OSError
    assert not report.is_valid
    assert "Cannot read file" in report.errors[0].message
    print("  ✓ linter: unreadable lint_file path reports an error")

def test_linter_project_option_types():
    """Project-level option type validation (non-bool, non-string, etc.)."""
    from gitacross.linter import ConfigLinter

    linter = ConfigLinter()
    report = linter.lint_yaml_string("""
- name: p
  enabled: false
  preserve_description: "yes"
  sync_assets: 42
  stream_assets: "no"
  commit_message: 5
  release_description: 7
  source: [not-a-dict]
  target: "not-a-dict"
  retry: [x]
  renderer: [x]
""")
    assert not report.is_valid
    msgs = " | ".join(e.message for e in report.errors)
    assert "'preserve_description' must be a boolean" in msgs
    assert "'sync_assets' must be a boolean, string, or list" in msgs
    assert "'stream_assets' must be a boolean" in msgs
    assert "'commit_message' must be a string" in msgs
    assert "'release_description' must be a string" in msgs
    assert "'source' must be a dictionary" in msgs
    assert "'target' must be a dictionary" in msgs
    assert "'retry' must be a dictionary" in msgs
    assert "'renderer' must be a dictionary" in msgs

    # sync_assets as string and as bad list
    r2 = linter.lint_yaml_string(
        "- name: p\n  sync_assets: '*.zip'\n  source: {type: local, path: /a}\n  target: {type: local, path: /b}\n"
    )
    assert r2.is_valid  # string is allowed
    r3 = linter.lint_yaml_string(
        "- name: p\n  sync_assets: [1, 2]\n  source: {type: local, path: /a}\n  target: {type: local, path: /b}\n"
    )
    assert not r3.is_valid
    assert "must only contain string glob patterns" in r3.errors[0].message

    # sync_assets: true is valid (no redundant flag)
    r4 = linter.lint_yaml_string(
        "- name: p\n  sync_assets: true\n  source: {type: local, path: /a}\n  target: {type: local, path: /b}\n"
    )
    assert r4.is_valid
    print("  ✓ linter: project option type validation")

def test_linter_source_target_retry_renderer_errors():
    """Endpoint/retry/renderer validation errors."""
    from gitacross.linter import ConfigLinter

    linter = ConfigLinter()
    # Invalid types short-circuit the rest of endpoint validation
    report = linter.lint_yaml_string("""
- name: p
  source:
    type: banana
  target:
    type: pear
  retry:
    max_attempts: 0
    backoff_seconds: -1
    bogus: 1
  renderer:
    bogus: 1
    ignore: not-a-list
    author: not-a-dict
    operations: not-a-list
""")
    assert not report.is_valid
    msgs = " | ".join(e.message for e in report.errors)
    assert "Invalid source type 'banana'" in msgs
    assert "Invalid target type 'pear'" in msgs
    assert "'retry.max_attempts' must be an integer >= 1" in msgs
    assert "'retry.backoff_seconds' must be a number >= 0" in msgs
    assert "'renderer.ignore' must be a list of patterns" in msgs
    assert "'renderer.author' must be a dictionary" in msgs
    assert "'renderer.operations' must be a list of operation maps" in msgs
    warns = " | ".join(w.message for w in report.warnings)
    assert "Unrecognized option 'bogus' under retry" in warns
    assert "Unrecognized option 'bogus' under renderer" in warns

    # Valid types → deeper validation (mode, booleans, misplaced keys)
    report2 = linter.lint_yaml_string("""
- name: p
  source:
    type: gitea
    repo: a/b
    api: https://x
    token: t
    sync_assets: true
    bogus_key: 1
    mode: frobnicate
    include_prereleases: "yes"
    include_drafts: "yes"
  target:
    type: github
    repo: a/b
    api: https://api.github.com
    token: t
    sync_assets: true
    mode: tag
    bogus_key: 2
""")
    assert not report2.is_valid
    msgs2 = " | ".join(e.message for e in report2.errors)
    assert "Invalid source mode 'frobnicate'" in msgs2
    assert "'source.include_prereleases' must be a boolean" in msgs2
    assert "'source.include_drafts' must be a boolean" in msgs2
    assert "'mode' was specified inside 'target:', but belongs under 'source:'" in msgs2
    warns2 = " | ".join(w.message for w in report2.warnings)
    assert "inside 'source:', but belongs at the project level" in warns2
    assert "Unrecognized option 'bogus_key' under source" in warns2
    assert "Unrecognized option 'bogus_key' under target" in warns2

    # skips: enabled/preserve_description false, prereleases true, retry with one field
    r2 = linter.lint_yaml_string("""
- name: p
  enabled: false
  preserve_description: false
  source:
    type: gitea
    repo: a/b
    api: https://x
    token: t
    include_prereleases: true
    include_drafts: true
  target: {type: local, path: /y}
""")
    assert r2.is_valid
    r3 = linter.lint_yaml_string(
        "- name: p\n  retry: {backoff_seconds: 5}\n  source: {type: local, path: /a}\n  target: {type: local, path: /b}\n"
    )
    assert r3.is_valid
    r4 = linter.lint_yaml_string(
        "- name: p\n  retry: {max_attempts: 5}\n  source: {type: local, path: /a}\n  target: {type: local, path: /b}\n"
    )
    assert r4.is_valid
    # renderer with only ignore → operations absent
    r5 = linter.lint_yaml_string(
        "- name: p\n  renderer: {ignore: ['*.md']}\n  source: {type: local, path: /a}\n  target: {type: local, path: /b}\n"
    )
    assert r5.is_valid
    # local source missing path + redundant tag_pattern
    r6 = linter.lint_yaml_string(
        "- name: p\n  source: {type: local, tag_pattern: '*'}\n  target: {type: local, path: /y}\n"
    )
    assert not r6.is_valid
    assert any("missing required 'path'" in e.message for e in r6.errors)
    print("  ✓ linter: source/target/retry/renderer validation errors")

def test_linter_operations_kitchen_sink():
    """Renderer operation validation: bad shapes, bad patterns, non-dict items."""
    from gitacross.linter import ConfigLinter

    linter = ConfigLinter()
    report = linter.lint_yaml_string("""
- name: p
  source: {type: local, path: /a}
  target: {type: local, path: /b}
  renderer:
    operations:
      - not-a-dict
      - remove: x
      - remove: [{}]
      - remove: [{path: x, pattern: banana}]
      - rename: [{}]
      - rename: [{from: a, to: b, pattern: glob}]
      - replace: [{}]
      - replace: [search, replace]
      - replace: [{search: a, replace: b, pattern: banana}]
      - add: [{}]
      - validate: [not-a-dict]
""")
    assert not report.is_valid
    msgs = " | ".join(e.message for e in report.errors)
    assert "Operation at index 0 must be a dictionary" in msgs
    assert "must contain a list of actions" in msgs
    assert "Remove operation item requires 'path'" in msgs
    assert "Invalid remove pattern mode 'banana'" in msgs
    assert "Rename operation item requires 'from' and 'to'" in msgs
    assert "Rename pattern mode 'glob' is not supported" in msgs
    assert "Replace operation item requires 'search' and 'replace'" in msgs
    assert "Invalid replace pattern mode 'banana'" in msgs
    assert "Add operation item requires 'path' and 'content'" in msgs
    assert "Validate operation item requires 'path'" in msgs
    print("  ✓ linter: operation kitchen-sink validation")

def test_fix_report_and_issue_formatting():
    """FixIssue strings and FixReport.format_text variants."""
    from gitacross.linter import FixIssue, FixReport

    assert str(FixIssue("m")) == "  ✓ m"
    assert str(FixIssue("m", project="p")) == "  ✓ (project 'p'): m"
    assert (
        FixReport([], "", True).format_text()
        == "No fixes needed. Config is already clean and optimal."
    )
    assert FixReport([], "", False, error="boom").format_text() == (
        "[ERROR] Could not fix configuration: boom"
    )
    text = FixReport([FixIssue("a"), FixIssue("b", project="p")], "x", True).format_text()
    assert "  ✓ a" in text and "Project 'p'" in text
    print("  ✓ fixer: FixIssue/FixReport formatting variants")

def test_fixer_edge_cases():
    """Fixer edge cases: invalid/empty YAML, non-list top level, moved keys."""
    from gitacross.linter import ConfigFixer

    fixer = ConfigFixer()

    # invalid YAML → invalid report
    r = fixer.fix_yaml_string("projects: [bad: {")
    assert not r.is_valid and r.error

    # empty → valid, no fixes
    r = fixer.fix_yaml_string("")
    assert r.is_valid and r.fixes == []

    # top-level not a list/map-with-projects → invalid
    r = fixer.fix_yaml_string("just a string")
    assert not r.is_valid

    # non-dict project entries are skipped
    r = fixer.fix_yaml_string(
        "projects:\n"
        + "  - name: ok\n    source: {type: local, path: /a}\n    target: {type: local, path: /b}\n"
        + "  - just-a-string\n"
    )
    assert r.is_valid

    # project-level key moved into a newly-created source section
    r = fixer.fix_yaml_string(
        "- name: p\n" + "  mode: commit\n" + "  target: {type: local, path: /b}\n"
    )
    assert any("Moved 'mode" in f.message for f in r.fixes)

    # key under target that belongs to source → moved into source
    r = fixer.fix_yaml_string(
        "- name: p\n"
        + "  source: {type: local, path: /a}\n"
        + "  target: {type: local, path: /b, mode: tag}\n"
    )
    assert any("from 'target:' into 'source:'" in f.message for f in r.fixes)

    # duplicates at project level and inside source/target
    r = fixer.fix_yaml_string(
        "- name: p\n"
        + "  sync_assets: true\n"
        + "  source: {type: local, path: /a, sync_assets: true}\n"
        + "  target: {type: local, path: /b, sync_assets: true}\n"
    )
    assert any("Removed duplicate" in f.message for f in r.fixes)

    # redundant retry defaults + default retry block removal
    r = fixer.fix_yaml_string(
        "- name: p\n"
        + "  retry: {max_attempts: 3, backoff_seconds: 2}\n"
        + "  source: {type: local, path: /a}\n"
        + "  target: {type: local, path: /b}\n"
    )
    assert any("retry" in f.message for f in r.fixes)

    # multiline value dumped with block style
    r = fixer.fix_yaml_string(
        "- name: p\n"
        + "  commit_message: |\n    line1\n    line2\n"
        + "  source: {type: local, path: /a}\n"
        + "  target: {type: local, path: /b}\n"
    )
    assert r.is_valid and "|" in r.content
    print("  ✓ fixer: edge cases (invalid YAML, moved/duplicate keys, retry defaults)")

def test_linter_valid_renderer_details():
    """Valid author dict, add op, and validate op take the happy paths."""
    from gitacross.linter import ConfigLinter

    linter = ConfigLinter()
    report = linter.lint_yaml_string("""
- name: p
  source: {type: local, path: /a}
  target: {type: local, path: /b}
  renderer:
    author: {name: N, email: E}
    operations:
      - add: [{path: new.txt, content: hi}]
      - validate: [{path: a.txt, assert: file_exists}]
        remove: [{path: b.txt}]
""")
    assert report.is_valid
    print("  ✓ linter: valid author/add/validate pass cleanly")

def test_fixer_missing_endpoint_sections():
    """Fixer handles projects missing source/target sections and junk operations."""
    from gitacross.linter import ConfigFixer

    fixer = ConfigFixer()

    # no source section → source→project loop skipped
    assert fixer.fix_yaml_string("- name: p\n  target: {type: local, path: /b}\n").is_valid

    # no target section → target loop skipped
    assert fixer.fix_yaml_string("- name: p\n  source: {type: local, path: /a}\n").is_valid

    # source-key under target with NO source → moved into a new source section
    r = fixer.fix_yaml_string(
        "- name: p\n  target: {type: local, path: /b, mode: tag}\n"
    )
    assert any("from 'target:' into 'source:'" in f.message for f in r.fixes)

    # source-key under target already present under source → duplicate removed
    r = fixer.fix_yaml_string(
        "- name: p\n"
        + "  source: {type: local, path: /a, sync_from: v1}\n"
        + "  target: {type: local, path: /b, sync_from: v2}\n"
    )
    assert any("already present under source" in f.message for f in r.fixes)

    # renderer operations with junk → skipped safely, redundant pattern removed
    r = fixer.fix_yaml_string(
        "- name: p\n"
        + "  renderer:\n"
        + "    operations:\n"
        + "      - not-a-dict\n"
        + "      - remove: x\n"
        + "      - remove: [not-a-dict]\n"
        + "      - remove: [{path: a, pattern: literal}]\n"
        + "  source: {type: local, path: /a}\n"
        + "  target: {type: local, path: /b}\n"
    )
    assert r.is_valid
    assert any("pattern: literal" in f.message for f in r.fixes)

    # renderer without operations → operations loop skipped
    assert fixer.fix_yaml_string(
        "- name: p\n"
        + "  renderer: {ignore: ['*.md']}\n"
        + "  source: {type: local, path: /a}\n"
        + "  target: {type: local, path: /b}\n"
    ).is_valid
    print("  ✓ fixer: missing endpoint sections and junk operations handled")
