"""Tests for config parsing — split from the original single-file suite."""
from __future__ import annotations

import io
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, cast
from unittest import mock

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_source_target():
    from gitacross.config import Config

    yaml_text = """
projects:
  - name: myapp
    source:
      type: gitea
      repo: owner/repo
      api: https://gitea.example.com/api/v1
      token: ${GITEA_TOKEN}
      include_prereleases: true
    target:
      type: github
      repo: owner/repo
      api: https://api.github.com
      token: ${GITHUB_TOKEN}
      branch: develop
    retry:
      max_attempts: 5
      backoff_seconds: 3
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        _ = f.write(yaml_text)
        config_path = f.name

    try:
        cfg = Config(config_path)
        assert len(cfg.projects) == 1
        proj = cfg.projects[0]
        assert proj.name == "myapp"

        # Source
        assert proj.source.type == "gitea"
        assert proj.source.repo == "owner/repo"
        assert proj.source.is_remote is True
        assert proj.source.include_prereleases is True
        assert proj.source.include_drafts is False

        # Target
        assert proj.target.type == "github"
        assert proj.target.repo == "owner/repo"
        assert proj.target.branch == "develop"

        # Retry
        assert proj.retry.max_attempts == 5
        assert proj.retry.backoff_seconds == 3

        # Token resolution
        os.environ["GITEA_TOKEN"] = "tkn"
        cfg2 = Config(config_path)
        assert cfg2.projects[0].source.token == "tkn"
        del os.environ["GITEA_TOKEN"]

        print("  ✓ config: source/target")
    finally:
        os.unlink(config_path)


def test_config_local():
    from gitacross.config import Config

    yaml_text = """
projects:
  - name: local-pub
    source:
      type: local
      path: /tmp/myrepo
      tag_pattern: "v*"
    target:
      type: local
      path: /tmp/mirror
      branch: releases
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        _ = f.write(yaml_text)
        config_path = f.name

    try:
        cfg = Config(config_path)
        proj = cfg.projects[0]
        assert proj.source.type == "local"
        assert proj.source.path == "/tmp/myrepo"
        assert proj.source.tag_pattern == "v*"
        assert proj.source.is_remote is False

        assert proj.target.type == "local"
        assert proj.target.path == "/tmp/mirror"
        assert proj.target.branch == "releases"
        assert proj.target.is_remote is False

        print("  ✓ config: local")
    finally:
        os.unlink(config_path)


def test_config_token_resolution():
    from gitacross.config import Config

    yaml_text = """
projects:
  - name: test
    source:
      type: gitea
      repo: a/b
      api: https://gitea.example.com
      token: ${MY_VAR}
    target:
      type: github
      repo: a/b
      api: https://api.github.com
      token: inline-token
      branch: main
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        _ = f.write(yaml_text)
        config_path = f.name

    try:
        cfg = Config(config_path)
        proj = cfg.projects[0]
        assert proj.target.token == "inline-token"
        assert proj.source.token == ""

        os.environ["MY_VAR"] = "resolved-token"
        cfg2 = Config(config_path)
        assert cfg2.projects[0].source.token == "resolved-token"
        del os.environ["MY_VAR"]

        print("  ✓ config: token resolution")
    finally:
        os.unlink(config_path)


def test_config_enabled():
    from gitacross.config import Config

    yaml_text = """
projects:
  - name: active-app
    source:
      type: local
      path: /tmp/a
    target:
      type: local
      path: /tmp/b
  - name: disabled-app
    enabled: false
    source:
      type: local
      path: /tmp/c
    target:
      type: local
      path: /tmp/d
  - name: explicitly-enabled-app
    enabled: true
    source:
      type: local
      path: /tmp/e
    target:
      type: local
      path: /tmp/f
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        _ = f.write(yaml_text)
        config_path = f.name

    try:
        cfg = Config(config_path)
        assert len(cfg.projects) == 3
        assert cfg.projects[0].enabled is True
        assert cfg.projects[1].enabled is False
        assert cfg.projects[2].enabled is True
        print("  ✓ config: enabled field")
    finally:
        os.unlink(config_path)


def test_config_warnings_for_misplaced_keys(caplog):

    from gitacross.config import Config

    yaml_text = """
projects:
  - name: misplaced-keys-app
    mode: commit
    sync_from: v1.0.0
    unknown_key: foo
    source:
      type: local
      path: /tmp/a
    target:
      type: local
      path: /tmp/b
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        _ = f.write(yaml_text)
        config_path = f.name

    try:
        with caplog.at_level(logging.WARNING):
            cfg = Config(config_path)
            assert len(cfg.projects) == 1
            log_text = caplog.text
            assert "Project 'misplaced-keys-app': 'mode' was specified at the project level" in log_text
            assert "Project 'misplaced-keys-app': 'sync_from' was specified at the project level" in log_text
            assert "Project 'misplaced-keys-app': unrecognized configuration key 'unknown_key'" in log_text
            print("  ✓ config: warning on misplaced keys")
    finally:
        os.unlink(config_path)


def test_config_from_file_object():
    from gitacross.config import Config
    from gitacross.linter import lint_config

    yaml_text = """
projects:
  - name: stream-app
    source:
      type: local
      path: /tmp/a
    target:
      type: local
      path: /tmp/b
"""

    # Config accepts an in-memory stream (no temp file needed)
    cfg = Config(io.StringIO(yaml_text))
    assert len(cfg.projects) == 1
    assert cfg.projects[0].name == "stream-app"

    # An opened file handle works too
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        _ = f.write(yaml_text)
        config_path = f.name
    try:
        with open(config_path) as fh:
            assert Config(fh).projects[0].name == "stream-app"
        # lint_config accepts a stream as well
        report = lint_config(io.StringIO(yaml_text), print_output=False)
        assert report.is_valid, [i.message for i in report.issues]
    finally:
        os.unlink(config_path)

    # from_yaml_string parses raw YAML text directly — no file or stream
    cfg = Config.from_yaml_string(yaml_text)
    assert len(cfg.projects) == 1
    assert cfg.projects[0].name == "stream-app"
    # str and bytes are both accepted
    assert Config.from_yaml_string(yaml_text.encode()).projects[0].name == "stream-app"

    print("  ✓ config: from file object / stream")


def test_config_invalid_inputs():
    """Config() fails loudly and clearly on invalid inputs."""
    import yaml

    from gitacross.config import Config

    # Missing path
    try:
        _ = Config("/nonexistent/x.yml")
        assert False, "should have raised FileNotFoundError"
    except FileNotFoundError:
        pass

    # YAML content passed as a path still raises, but hints at the string API
    try:
        _ = Config("projects: []")
        assert False, "should have raised FileNotFoundError"
    except FileNotFoundError as e:
        assert "from_yaml_string" in str(e), str(e)

    # Garbage types -> clear TypeError, not a cryptic PyYAML error
    for bad in (None, 123, object()):
        try:
            _ = Config(bad)
            assert False, f"Config({bad!r}) should have raised TypeError"
        except TypeError as e:
            assert "config_source" in str(e), str(e)

    # Invalid YAML propagates as yaml.YAMLError (loader must fail loudly)
    try:
        _ = Config(io.StringIO("projects: [oops: {"))
        assert False, "should have raised yaml.YAMLError"
    except yaml.YAMLError:
        pass

    # Binary streams parse too (same duck-typed path as text streams)
    assert Config(io.BytesIO(b"projects: []\n")).projects == []

    # from_yaml_string rejects non-text and malformed YAML loudly
    try:
        _ = Config.from_yaml_string(None)
        assert False, "from_yaml_string(None) should have raised TypeError"
    except TypeError as e:
        assert "content" in str(e), str(e)
    try:
        _ = Config.from_yaml_string("projects: [oops: {")
        assert False, "should have raised yaml.YAMLError"
    except yaml.YAMLError:
        pass

    print("  ✓ config: invalid inputs raise clearly")


def test_run_input_forms_equivalent():
    """run() accepts path / stream / Config instance with identical results."""
    import gitacross
    from gitacross.config import Config

    yaml_text = """projects:
  - name: proj-a
    source: {type: local, path: /tmp/a}
    target: {type: local, path: /tmp/b}
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        _ = f.write(yaml_text)
        config_path = f.name
    try:
        with mock.patch("gitacross.main.sync_project", return_value=[]):
            by_path = gitacross.run(config_path, work_dir=tempfile.mkdtemp())
            by_stream = gitacross.run(io.StringIO(yaml_text), work_dir=tempfile.mkdtemp())
            by_instance = gitacross.run(
                Config(io.StringIO(yaml_text)), work_dir=tempfile.mkdtemp()
            )
            by_string = gitacross.run(
                Config.from_yaml_string(yaml_text), work_dir=tempfile.mkdtemp()
            )
        assert by_path == by_stream == by_instance == by_string
        assert [r["project"] for r in by_path] == ["proj-a"]
    finally:
        os.unlink(config_path)

    print("  ✓ run: path / stream / Config instance are equivalent")


def test_run_invalid_config_inputs():
    """run() fails loudly on invalid YAML and garbage config types."""
    import yaml

    import gitacross

    # Invalid YAML via path and via stream both raise yaml.YAMLError
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        _ = f.write("projects: [oops: {")
        bad_path = f.name
    try:
        for source in (bad_path, io.StringIO("projects: [oops: {")):
            try:
                _ = gitacross.run(source, work_dir=tempfile.mkdtemp())
                assert False, "should have raised yaml.YAMLError"
            except yaml.YAMLError:
                pass
    finally:
        os.unlink(bad_path)

    # Garbage types -> TypeError with a clear message
    for bad in (None, 123):
        try:
            _ = gitacross.run(bad, work_dir=tempfile.mkdtemp())
            assert False, f"run({bad!r}) should have raised TypeError"
        except TypeError as e:
            assert "config_source" in str(e), str(e)

    print("  ✓ run: invalid config inputs raise")


def test_lint_config_input_forms():
    """lint_config() accepts paths, text/binary streams, and handles alike."""
    from gitacross.linter import lint_config

    yaml_text = """projects:
  - name: lint-app
    source: {type: local, path: /a}
    target: {type: local, path: /b}
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        _ = f.write(yaml_text)
        config_path = f.name
    try:
        for source in (
            config_path,                      # path
            io.StringIO(yaml_text),           # text stream
            io.BytesIO(yaml_text.encode()),   # binary stream
        ):
            report = lint_config(source, print_output=False)
            assert report.is_valid, [i.message for i in report.issues]
        with open(config_path) as fh:         # opened handle
            assert lint_config(fh, print_output=False).is_valid
    finally:
        os.unlink(config_path)

    # Missing path -> reported, not raised
    report = lint_config("/nonexistent/x.yml", print_output=False)
    assert not report.is_valid
    assert any("File not found" in e.message for e in report.errors)

    # Invalid YAML stream -> reported, not raised
    report = lint_config(io.StringIO("projects: [oops: {"), print_output=False)
    assert not report.is_valid
    assert any("YAML syntax error" in e.message for e in report.errors)

    # Garbage type -> reported as a stream read failure, not raised
    report = lint_config(cast(Any, None), print_output=False)
    assert not report.is_valid
    assert any("Cannot read config stream" in e.message for e in report.errors)

    print("  ✓ lint_config: path / stream / handle / binary / invalid inputs")


def test_fix_config_missing_file():
    """fix_config() reports a missing file in the FixReport instead of raising."""
    from gitacross.linter import fix_config

    report = fix_config("/nonexistent/x.yml", write_back=False, print_output=False)
    assert not report.is_valid
    assert report.error and "File not found" in report.error

    print("  ✓ fix_config: missing file reported, not raised")


def test_config_from_yaml_string_none():
    """An empty YAML string yields zero projects (raw=None path)."""
    from gitacross.config import Config

    assert Config.from_yaml_string("").projects == []
    print("  ✓ config: empty YAML string → zero projects")

def test_config_project_level_endpoint_key_warning(caplog):
    """Project-level endpoint keys (repo/api/token/branch) warn with their section."""
    from gitacross.config import Config

    with caplog.at_level("WARNING"):
        _ = Config.from_yaml_string(
            "- name: p\n"
            + "  repo: owner/x\n"
            + "  source: {type: local, path: /a}\n"
            + "  target: {type: local, path: /b}\n"
        )
    assert "belongs under 'source or target:'" in caplog.text
    print("  ✓ config: project-level endpoint keys warn")

def test_package_version_fallback_no_pyproject():
    """_package_version returns '0.0.0' when neither installed nor a pyproject exists."""
    from importlib.metadata import PackageNotFoundError

    import gitacross

    with mock.patch(
        "gitacross._installed_version", side_effect=PackageNotFoundError
    ), mock.patch.object(Path, "is_file", return_value=False):
        assert gitacross._package_version() == "0.0.0"
    print("  ✓ package: __version__ falls back to '0.0.0'")

def test_package_version_no_version_match():
    """_package_version returns '0.0.0' when pyproject has no version line."""
    from importlib.metadata import PackageNotFoundError

    import gitacross

    # Mock _installed_version to raise so the pyproject fallback runs; patching
    # Path.read_text globally would otherwise break importlib.metadata itself.
    with mock.patch(
        "gitacross._installed_version", side_effect=PackageNotFoundError
    ), mock.patch("gitacross.Path.is_file", return_value=True), mock.patch(
        "gitacross.Path.read_text", return_value="no version here"
    ):
        assert gitacross._package_version() == "0.0.0"
    print("  ✓ package: __version__ falls back when pyproject lacks a version")
