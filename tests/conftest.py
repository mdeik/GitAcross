"""Shared helpers for the GitAcross test suite.

Split out of the original single-file suite so every test module can
reuse the same repo/mock helpers. Imported via ``from tests.conftest import ...``.
"""
# pyright: reportUnusedFunction=false, reportUnusedClass=false
from __future__ import annotations

import email.message
import io
import subprocess
import urllib.error
from pathlib import Path


def _make_file(root, path, content="hello"):
    full = root / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    return full


def _make_git_repo(path):
    """Init a bare or non-bare git repo at *path* and return it."""
    _ = subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    _ = subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test"],
        check=True,
        capture_output=True,
    )
    _ = subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    return path


def _git_commit(path, msg="init"):
    _ = subprocess.run(
        ["git", "-C", str(path), "add", "-A"], check=True, capture_output=True
    )
    _ = subprocess.run(
        ["git", "-C", str(path), "commit", "-m", msg, "--allow-empty"],
        check=True,
        capture_output=True,
    )


def _git_tag(path, tag):
    _ = subprocess.run(
        ["git", "-C", str(path), "tag", tag], check=True, capture_output=True
    )




class _FakeHTTPResponse:
    def __init__(self, body: bytes = b"{}"):
        self._body: bytes = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _http_error(code):
    return urllib.error.HTTPError(
        "https://api.example.com/repos/owner/repo",
        code,
        "error",
        email.message.Message(),
        io.BytesIO(b'{"message": "nope"}'),
    )


def _write_local_config(path, projects):
    """Write a minimal local→local config for run() tests."""
    lines = ["projects:\n"]
    for p in projects:
        enabled_line = f"    enabled: {str(p.get('enabled', True)).lower()}\n" if "enabled" in p else ""
        lines += [
            f"  - name: {p['name']}\n",
            enabled_line,
            f"    source:\n      type: local\n      path: {p.get('src', '/nonexistent')}\n",
            f"    target:\n      type: local\n      path: {p.get('tgt', '/nonexistent')}\n",
        ]
    _ = Path(path).write_text("".join(lines))

