from __future__ import annotations

import fnmatch
import logging
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def apply_operations(work_dir, project):
    """Run the full render pipeline on *work_dir* after source tag overlay.

    1. Remove paths in `ignore` (glob list, always first).
    2. Apply `operations` in declaration order.
    """
    work = Path(work_dir)

    for pattern in project.renderer.ignore:
        _remove_glob(work, pattern)

    for op in project.renderer.operations:
        if "remove" in op:
            _op_remove(work, op["remove"])
        elif "rename" in op:
            _op_rename(work, op["rename"])
        elif "replace" in op:
            _op_replace(work, op["replace"])
        elif "add" in op:
            _op_add(work, op["add"])
        elif "validate" in op:
            _op_validate(work, op["validate"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _remove_glob(root, pattern):
    """Remove all files/dirs matching a glob pattern relative to *root*."""
    paths = sorted(root.rglob(pattern), key=lambda p: len(str(p)), reverse=True)
    for path in paths:
        if not path.exists():
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    _clean_empty_dirs(root)


def _clean_empty_dirs(root):
    for path in sorted(root.rglob("*"), key=lambda p: len(str(p)), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def _iter_files(root):
    for path in root.rglob("*"):
        if path.is_file():
            yield path


# ---------------------------------------------------------------------------
# Operation handlers
# ---------------------------------------------------------------------------


def _op_remove(work, ops):
    """Remove matching files/dirs.
    Each op: {path: str, pattern?: literal|glob|regex}
    """
    for op in ops:
        pat = op.get("path", "")
        mode = op.get("pattern", "literal")
        if mode == "literal":
            target = work / pat
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
        elif mode == "glob":
            _remove_glob(work, pat)
        elif mode == "regex":
            for f in _iter_files(work):
                if re.search(pat, str(f.relative_to(work))):
                    f.unlink()
            _clean_empty_dirs(work)


def _op_rename(work, ops):
    """Rename files/dirs. Processes deepest paths first to avoid parent conflicts.

    Each op: {from: str, to: str, pattern?: literal|glob|regex}
    """
    # ponytail: only literal renames are implemented. Glob/regex rename raises.
    for op in sorted(ops, key=lambda o: -len(o.get("from", ""))):
        mode = op.get("pattern", "literal")
        if mode != "literal":
            raise NotImplementedError(
                f"Rename pattern '{mode}' is not yet implemented — use 'literal'"
            )

        src = work / op["from"]
        dst = work / op["to"]
        if src.exists():
            if dst.exists():
                raise RuntimeError(f"Rename conflict: {op['to']} already exists")
            src.rename(dst)


def _op_replace(work, ops):
    """Search-and-replace in UTF-8 text files.

    Each op: {search: str, replace: str, pattern?: literal|regex, glob?: str, path?: str,
              case_sensitive?: bool (default true), match_case?: bool (default false)}
    """
    for op in ops:
        search = op["search"]
        replace = op["replace"]
        mode = op.get("pattern", "literal")
        case_sensitive = bool(op.get("case_sensitive", True))
        match_case = bool(op.get("match_case", False))
        path_filter = op.get("path")
        glob_filter = op.get("glob")

        for f in _iter_files(work):
            rel = f.relative_to(work)
            if path_filter:
                if str(rel) != path_filter:
                    continue
            elif glob_filter:
                if not fnmatch.fnmatch(str(rel), glob_filter):
                    continue

            try:
                text = f.read_text("utf-8")
            except (UnicodeDecodeError, ValueError):
                continue

            new_text = _apply_replace(
                text, search, replace, mode, case_sensitive, match_case
            )
            if new_text != text:
                f.write_text(new_text, encoding="utf-8")


def _apply_replace(text, search, replace, mode, case_sensitive, match_case):
    """Apply a single replace rule to *text*.

    *case_sensitive* controls whether the search is case-sensitive
    (default true). *match_case* adapts each replacement to the casing of
    the text it matched (default false), e.g. with `search: gitea` and
    `replace: github`: `gitea -> github`, `Gitea -> Github`, `GITEA -> GITHUB`.
    """
    if mode == "regex":
        flags = 0 if case_sensitive else re.IGNORECASE
        if match_case:
            return re.sub(
                search,
                lambda m: _match_case(m.group(0), m.expand(replace)),
                text,
                flags=flags,
            )
        return re.sub(search, replace, text, flags=flags)

    # literal
    if case_sensitive and not match_case:
        return text.replace(search, replace)

    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(re.escape(search), flags)
    if match_case:
        return pattern.sub(lambda m: _match_case(m.group(0), replace), text)
    # lambda keeps the replacement literal — re.sub would otherwise
    # interpret backreferences (e.g. r"\1") in the replacement string.
    return pattern.sub(lambda m: replace, text)


def _match_case(matched, replacement):
    """Adapt *replacement* to the casing pattern of the matched text.

    - ALL CAPS match   -> replacement uppercased
    - Capitalized match -> replacement with first letter uppercased
    - otherwise (lowercase / mixed) -> replacement unchanged
    """
    if matched.isupper():
        return replacement.upper()
    if matched[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _op_add(work, ops):
    """Create files in the work tree before commit.

    Each op: {path: str, content: str}
    Raises if the path already exists (avoids silently overwriting).
    Creates parent directories automatically.
    """
    for op in ops:
        path = op["path"]
        target = work / path
        if target.exists():
            raise RuntimeError(f"Add conflict: '{path}' already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(op.get("content", ""), encoding="utf-8")
        logger.debug("Added file: %s", path)


def _op_validate(work, ops):
    """Assert conditions on the final tree. Aborts on failure.

    Each op: {assert: str, path: str, pattern?: str, case_sensitive?: bool (default true)}
    """
    for op in ops:
        assert_type = op.get("assert", "")
        path = op.get("path", "")
        pattern = op.get("pattern", "")
        case_sensitive = bool(op.get("case_sensitive", True))
        target = work / path

        if assert_type == "file_exists":
            if not target.exists():
                raise RuntimeError(f"Validation failed: '{path}' must exist")
        elif assert_type == "file_absent":
            if target.exists():
                raise RuntimeError(f"Validation failed: '{path}' must not exist")
        elif assert_type == "string_exists":
            if not target.exists():
                raise RuntimeError(
                    f"Validation failed: '{path}' not found (for string_exists check)"
                )
            content = target.read_text("utf-8", errors="replace")
            if not _contains(content, pattern, case_sensitive):
                raise RuntimeError(
                    f"Validation failed: '{pattern}' not found in '{path}'"
                )
        elif assert_type == "string_absent":
            if target.exists():
                content = target.read_text("utf-8", errors="replace")
                if _contains(content, pattern, case_sensitive):
                    raise RuntimeError(
                        f"Validation failed: '{pattern}' found in '{path}'"
                    )


def _contains(content, pattern, case_sensitive):
    """Case-sensitive or case-insensitive substring check."""
    if case_sensitive:
        return pattern in content
    return pattern.casefold() in content.casefold()
