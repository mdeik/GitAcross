"""Valid key sets for source/target endpoints.

Separate from the validator so the fixer never pulls in validation code.
"""
from __future__ import annotations

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

