"""Tests for the render pipeline — split from the original single-file suite."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from tests.conftest import (
    _make_file,
)

# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def _make_project_raw(ops=None) -> dict[str, Any]:
    """Minimal config dict for renderer tests."""
    raw = {
        "name": "test",
        "source": {
            "type": "gitea",
            "repo": "a/b",
            "api": "https://x.com",
            "token": "x",
        },
        "target": {
            "type": "github",
            "repo": "a/b",
            "api": "https://x.com",
            "token": "x",
            "branch": "main",
        },
        "renderer": {
            "operations": ops or [],
        },
    }
    return raw


def test_renderer_ignore():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = []
    raw = _make_project_raw(ops)
    raw["renderer"]["ignore"] = ["node_modules/**", ".env"]
    project = ProjectConfig(raw)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _ = _make_file(root, "README.md")
        _ = _make_file(root, ".env", "SECRET=1")
        _ = _make_file(root, "node_modules/foo/index.js")
        _ = _make_file(root, "src/main.py")

        apply_operations(root, project)

        assert (root / "README.md").exists()
        assert (root / "src/main.py").exists()
        assert not (root / ".env").exists()
        assert not (root / "node_modules").exists()

    print("  \u2713 renderer ignore")


def test_renderer_remove():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = [{"remove": [{"path": "docs"}, {"path": "*.secret", "pattern": "glob"}]}]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _ = _make_file(root, "README.md")
        _ = _make_file(root, "docs/guide.md")
        _ = _make_file(root, "docs/api/ref.md")
        _ = _make_file(root, "config.secret")
        _ = _make_file(root, ".env.secret")

        apply_operations(root, project)

        assert (root / "README.md").exists()
        assert not (root / "docs").exists()
        assert not (root / "config.secret").exists()
        assert not (root / ".env.secret").exists()

    print("  ✓ renderer remove")


def test_renderer_rename():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = [{"rename": [{"from": ".gitea", "to": ".github"}]}]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _ = _make_file(root, "README.md")
        _ = _make_file(root, ".gitea/workflows/build.yml")

        apply_operations(root, project)

        assert (root / "README.md").exists()
        assert not (root / ".gitea").exists()
        assert (root / ".github/workflows/build.yml").exists()

    print("  ✓ renderer rename")


def test_renderer_rename_conflict():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = [{"rename": [{"from": "a.txt", "to": "b.txt"}]}]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _ = _make_file(root, "a.txt", "content-a")
        _ = _make_file(root, "b.txt", "content-b")

        try:
            apply_operations(root, project)
            assert False, "should have raised RuntimeError"
        except RuntimeError as e:
            assert "already exists" in str(e)

        # Source file untouched on conflict
        assert (root / "a.txt").read_text() == "content-a"

    print("  ✓ renderer: rename to an existing path raises a conflict error")


def test_renderer_replace():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = [
        {
            "replace": [
                {
                    "search": r"https://gitea\.example\.com",
                    "replace": "https://github.com",
                    "pattern": "regex",
                    "glob": "*.md",
                }
            ]
        }
    ]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _ = _make_file(root, "README.md", "Visit https://gitea.example.com for code.")
        _ = _make_file(root, "setup.py", "url = 'https://gitea.example.com'")

        apply_operations(root, project)

        assert "https://github.com" in (root / "README.md").read_text()
        assert "gitea.example.com" in (root / "setup.py").read_text()

    print("  ✓ renderer replace")


def test_renderer_replace_literal():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = [
        {
            "replace": [
                {"search": "old-text", "replace": "new-text", "pattern": "literal"}
            ]
        }
    ]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _ = _make_file(root, "file.txt", "prefix old-text suffix")

        apply_operations(root, project)

        assert "new-text" in (root / "file.txt").read_text()

    print("  ✓ renderer replace literal")


def test_renderer_replace_case_insensitive():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = [
        {
            "replace": [
                {"search": "gitea", "replace": "github", "case_sensitive": False}
            ]
        }
    ]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _ = _make_file(root, "file.txt", "gitea Gitea GITEA")

        apply_operations(root, project)

        assert (root / "file.txt").read_text() == "github github github"

    print("  ✓ renderer replace case-insensitive")


def test_renderer_replace_match_case():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = [
        {
            "replace": [
                {
                    "search": "gitea",
                    "replace": "github",
                    "case_sensitive": False,
                    "match_case": True,
                }
            ]
        }
    ]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _ = _make_file(root, "file.txt", "gitea Gitea GITEA")

        apply_operations(root, project)

        assert (root / "file.txt").read_text() == "github Github GITHUB"

    print("  ✓ renderer replace match-case")


def test_renderer_replace_regex_case_options():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    # Backreferences are expanded before match-case adaptation, and
    # case-insensitive search works in regex mode too.
    ops = [
        {
            "replace": [
                {
                    "search": r"gitea(\s*)",
                    "replace": "github\\1",
                    "pattern": "regex",
                    "case_sensitive": False,
                    "match_case": True,
                }
            ]
        }
    ]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _ = _make_file(root, "file.txt", "gitea Gitea GITEA")

        apply_operations(root, project)

        assert (root / "file.txt").read_text() == "github Github GITHUB"

    print("  ✓ renderer replace regex case options")


def test_renderer_replace_path_takes_precedence_over_glob():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = [{"replace": [{"search": "gitea", "replace": "github", "glob": "*.txt", "path": "SPECIAL.md"}]}]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _ = _make_file(root, "a.txt", "gitea")
        _ = _make_file(root, "SPECIAL.md", "gitea")

        apply_operations(root, project)

        # path wins over glob: only SPECIAL.md is modified
        assert (root / "SPECIAL.md").read_text() == "github"
        assert (root / "a.txt").read_text() == "gitea"

    print("  ✓ renderer: replace `path` takes precedence over `glob`")


def test_renderer_replace_skips_binary_files():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = [{"replace": [{"search": "hello", "replace": "world"}]}]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _ = _make_file(root, "README.md", "hello text")
        binary = root / "bin.dat"
        _ = binary.write_bytes(b"\x00\x01\xff\xfehello\x00")

        apply_operations(root, project)

        assert (root / "README.md").read_text() == "world text"
        assert binary.read_bytes() == b"\x00\x01\xff\xfehello\x00"

    print("  ✓ renderer: replace skips binary (non-UTF-8) files")


def test_renderer_operation_order():
    """Blocks and items run top-to-bottom; later operations see earlier output."""
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = [
        {"rename": [{"from": "OLD.md", "to": "NEW.md"}]},
        {"replace": [{"search": "gitea", "replace": "github", "glob": "NEW.md"}]},
        {"replace": [{"search": "github.example.com", "replace": "github.com", "glob": "*.md"}]},
    ]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _ = _make_file(root, "OLD.md", "host: gitea.example.com")

        apply_operations(root, project)

        assert not (root / "OLD.md").exists()
        assert (root / "NEW.md").read_text() == "host: github.com"

    print("  ✓ renderer: operations run top-to-bottom (rename -> replace -> replace)")


def test_renderer_rename_glob_not_implemented():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = [{"rename": [{"from": "*.md", "to": "*.txt", "pattern": "glob"}]}]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _ = _make_file(root, "a.md")
        try:
            apply_operations(root, project)
            assert False, "should have raised NotImplementedError"
        except NotImplementedError as e:
            assert "glob" in str(e)

    print("  ✓ renderer: rename with a non-literal pattern raises NotImplementedError")


def test_renderer_replace_invalid_regex_raises():
    import re

    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = [{"replace": [{"search": "([unclosed", "replace": "x", "pattern": "regex"}]}]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _ = _make_file(root, "a.md", "content")
        try:
            apply_operations(root, project)
            assert False, "should have raised re.error"
        except re.error:
            pass

    print("  ✓ renderer: invalid regex raises re.error (project fails loudly)")


def test_renderer_validate_ok():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = [
        {
            "validate": [
                {"assert": "file_exists", "path": "README.md"},
                {"assert": "file_absent", "path": "SECRETS.md"},
            ]
        }
    ]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _ = _make_file(root, "README.md")
        apply_operations(root, project)

    print("  ✓ renderer validate (pass)")


def test_renderer_add():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = [
        {
            "add": [
                {"path": "NEW_FILE.md", "content": "# New\n\nAdded content."},
                {"path": "nested/deep/file.txt", "content": "deep"},
            ]
        }
    ]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _ = _make_file(root, "README.md")
        apply_operations(root, project)

        assert (root / "NEW_FILE.md").exists()
        assert (root / "NEW_FILE.md").read_text() == "# New\n\nAdded content."
        assert (root / "nested/deep/file.txt").exists()
        assert (root / "nested/deep/file.txt").read_text() == "deep"
        assert (root / "README.md").exists()  # untouched

    print("  ✓ renderer add")


def test_renderer_validate_fail():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    ops = [{"validate": [{"assert": "file_exists", "path": "MISSING.md"}]}]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _ = _make_file(root, "README.md")

        try:
            apply_operations(root, project)
            assert False, "Should have raised"
        except RuntimeError as e:
            assert "MISSING.md" in str(e)

    print("  ✓ renderer validate (fail)")


def test_renderer_validate_case_insensitive():
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    # Default (case-sensitive) search misses, case-insensitive search hits.
    ops = [
        {
            "validate": [
                {"assert": "string_exists", "path": "README.md", "pattern": "GITEA", "case_sensitive": False},
                {"assert": "string_absent", "path": "README.md", "pattern": "GITHUB", "case_sensitive": False},
            ]
        }
    ]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _ = _make_file(root, "README.md", "Welcome to Gitea!")
        apply_operations(root, project)  # no raise = pass

    # The same patterns are case-sensitive by default and must fail.
    ops = [
        {
            "validate": [
                {"assert": "string_exists", "path": "README.md", "pattern": "GITEA"},
            ]
        }
    ]
    project = ProjectConfig(_make_project_raw(ops))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _ = _make_file(root, "README.md", "Welcome to Gitea!")
        try:
            apply_operations(root, project)
            assert False, "Should have raised"
        except RuntimeError as e:
            assert "'GITEA' not found" in str(e)

    print("  ✓ renderer validate case-insensitive")


def test_renderer_coverage_branches():
    """Renderer defensive branches: glob/regex remove, cleanup, rename/add/validate edges."""
    from gitacross.renderer import (
        _clean_empty_dirs,
        _contains,
        _iter_files,
        _op_add,
        _op_remove,
        _op_rename,
        _op_replace,
        _op_validate,
        _remove_glob,
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "sub").mkdir(parents=True)
        (root / "keep.txt").write_text("hi")
        (root / "sub" / "drop.txt").write_text("d")
        (root / "sub" / "keep2.txt").write_text("k")
        (root / "emptydir").mkdir()
        (root / "sub2").mkdir()
        os.symlink(root / "nonexistent-target", root / "sub2" / "broken")

        # broken symlink → exists() False → skipped (46)
        _remove_glob(root, "sub2/*")

        # glob removal keeps other files (46→51 loop)
        _remove_glob(root, "sub/drop.txt")
        assert (root / "sub" / "keep2.txt").exists()

        # regex removal via _op_remove (87-91) — matches the full relative path,
        # followed by a second op so the regex branch loops back to the next op
        _op_remove(
            root,
            [
                {"path": r"keep2", "pattern": "regex"},
                {"path": "no-such-file", "pattern": "literal"},
            ],
        )
        assert not (root / "sub" / "keep2.txt").exists()

        # literal directory removal → rmtree (81-82)
        _op_remove(root, [{"path": "sub", "pattern": "literal"}])
        assert not (root / "sub").exists()

        # literal file removal → unlink (83-84)
        _op_remove(root, [{"path": "keep.txt", "pattern": "literal"}])
        assert not (root / "keep.txt").exists()

        # empty-dir cleanup (56-57)
        _clean_empty_dirs(root)
        assert not (root / "emptydir").exists()

        # _iter_files skips directories (62->61)
        (root / "adir").mkdir()
        (root / "top.txt").write_text("t")
        assert (root / "top.txt") in list(_iter_files(root))

        # rename with missing source → no-op (109->100)
        _op_rename(root, [{"from": "missing.txt", "to": "x.txt"}])

        # replace with no match → no write (146->130)
        (root / "r.txt").write_text("original")
        _op_replace(root, [{"search": "not-there", "replace": "x", "path": "r.txt"}])
        assert (root / "r.txt").read_text() == "original"

        # add conflict (207)
        try:
            _op_add(root, [{"path": "top.txt", "content": "x"}])
            assert False, "should raise"
        except RuntimeError as e:
            assert "Add conflict" in str(e)

        # validate: string_exists on missing file (230) and case-insensitive (233, 241)
        try:
            _op_validate(
                root, [{"assert": "string_exists", "path": "missing.txt", "pattern": "x"}]
            )
            assert False, "should raise"
        except RuntimeError as e:
            assert "not found" in str(e)
        _op_validate(
            root, [{"assert": "string_exists", "path": "r.txt", "pattern": "ORIG", "case_sensitive": False}]
        )

        # _contains case-sensitive and case-insensitive (244, 255)
        assert _contains("Hello World", "WORLD", False) is True
        assert _contains("Hello World", "world", True) is False

        # invalid pattern mode → falls through the elif chain to the next op
        (root / "m.txt").write_text("m")
        _op_remove(
            root,
            [
                {"path": "m.txt", "pattern": "banana"},
                {"path": "gone.txt", "pattern": "literal"},
            ],
        )
        assert (root / "m.txt").exists()  # invalid mode silently ignored
    print("  ✓ renderer: defensive branch coverage")

def test_renderer_unknown_op_ignored():
    """apply_operations silently ignores unknown operation types."""
    from gitacross.config import ProjectConfig
    from gitacross.renderer import apply_operations

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "f.txt").write_text("hi")
        project = ProjectConfig(
            {
                "name": "p",
                "source": {"type": "local", "path": "/x"},
                "target": {"type": "local", "path": "/y"},
                "renderer": {"operations": [{"explode": []}]},
            }
        )
        apply_operations(root, project)
        assert (root / "f.txt").exists()
    print("  ✓ renderer: unknown operations are ignored")

def test_renderer_validate_absent_asserts():
    """file_absent with an existing file raises; string_absent checks content."""
    from gitacross.renderer import _op_validate

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "f.txt").write_text("contains-secret")

        try:
            _op_validate(root, [{"assert": "file_absent", "path": "f.txt"}])
            assert False, "should raise"
        except RuntimeError as e:
            assert "must not exist" in str(e)

        try:
            _op_validate(
                root, [{"assert": "string_absent", "path": "f.txt", "pattern": "secret"}]
            )
            assert False, "should raise"
        except RuntimeError as e:
            assert "found" in str(e)

        # no matching content → no raise
        _op_validate(
            root, [{"assert": "string_absent", "path": "f.txt", "pattern": "zzz"}]
        )
        # string_absent on a missing file → elif condition false (241->...)
        _op_validate(
            root, [{"assert": "string_absent", "path": "missing-file", "pattern": "x"}]
        )
    print("  ✓ renderer: file_absent/string_absent validation paths")
