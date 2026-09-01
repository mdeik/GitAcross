from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import final

import yaml

logger = logging.getLogger(__name__)

VALID_PROJECT_KEYS = {
    "name",
    "enabled",
    "source",
    "target",
    "renderer",
    "retry",
    "preserve_description",
    "sync_assets",
    "stream_assets",
    "commit_message",
    "commit_template",
    "release_description",
    "release_notes_template",
    "description_template",
}

KNOWN_SOURCE_KEYS = {
    "mode": "source",
    "sync_from": "source",
    "include_prereleases": "source",
    "include_drafts": "source",
    "tag_pattern": "source",
}

KNOWN_ENDPOINT_KEYS = {
    "repo": "source or target",
    "api": "source or target",
    "token": "source or target",
    "branch": "source or target",
}


@final
class Config:
    """Load and parse a config from a file path or an already-open file object.

    ``config_source`` may be a ``str``/:class:`~pathlib.Path` (opened and read
    here) or any file-like object with a ``read()`` method (e.g. the result of
    ``open()`` or an ``io.StringIO``), matching what ``yaml.safe_load`` accepts.
    To parse YAML held directly in a variable, use :meth:`from_yaml_string`.
    """

    def __init__(self, config_source):
        if isinstance(config_source, (str, Path)):
            try:
                with open(config_source) as f:
                    raw = yaml.safe_load(f)
            except FileNotFoundError as exc:
                raise Config._missing_source_error(exc, config_source) from exc
        elif hasattr(config_source, "read"):
            # Open file-like object (StringIO, an open() handle, BytesIO, ...)
            raw = yaml.safe_load(config_source)
        else:
            raise TypeError(
                "config_source must be a path (str/Path) or an open file-like "
                + f"object, got {type(config_source).__name__}"
            )
        self.projects = self._projects_from_parsed(raw)

    @classmethod
    def from_yaml_string(cls, content):
        """Build a :class:`Config` from raw YAML text (``str`` or ``bytes``).

        Equivalent to wrapping *content* in ``io.StringIO``/``io.BytesIO`` and
        passing that to :class:`Config`, but hands the string straight to the
        YAML parser — no intermediate file or stream object:

        >>> config = Config.from_yaml_string("projects: []")

        Raises:
            yaml.YAMLError: If *content* is not valid YAML.
            TypeError: If *content* is not a ``str`` or ``bytes``.
        """
        if not isinstance(content, (str, bytes)):
            raise TypeError(
                "content must be a str or bytes holding YAML, got "
                + type(content).__name__
            )
        config = cls.__new__(cls)
        config.projects = cls._projects_from_parsed(yaml.safe_load(content))
        return config

    @staticmethod
    def _projects_from_parsed(raw) -> list[ProjectConfig]:
        """Normalize one parsed YAML document into project configs.

        Single source of truth for the accepted top-level shapes: a ``projects``
        mapping, a flat list of project mappings, or nothing (empty config).
        Shared by :meth:`__init__` and :meth:`from_yaml_string` so both accept
        exactly the same documents.
        """
        if isinstance(raw, list):
            # Flat list at top level: [ {name:..., source:..., ...} ]
            raw = {"projects": raw}
        elif raw is None:
            raw = {"projects": []}
        return [ProjectConfig(p) for p in (raw.get("projects") or [])]

    @staticmethod
    def _missing_source_error(exc: FileNotFoundError, config_source) -> FileNotFoundError:
        """Re-raise a failed path open, hinting when the string looks like YAML
        content rather than a path. Cosmetic only — never affects dispatch."""
        text = os.fspath(config_source)
        hint = (
            " — did you mean to pass YAML content? Use "
            + "Config.from_yaml_string(...) or wrap it in io.StringIO(...)."
            if "\n" in text or ": " in text
            else ""
        )
        return FileNotFoundError(f"{exc}{hint}")

    def __repr__(self):
        return f"Config(projects={len(self.projects)})"


@final
class _EndpointConfig:
    """Config for a source or target endpoint.

    Fields shared by all types: type
    Remote (gitea/github): repo, api, token
    Local: path, tag_pattern/branch
    """

    def __init__(self, raw, is_source):
        self.type = raw.get("type", "gitea" if is_source else "github")

        # Remote endpoint fields
        self.repo = raw.get("repo", "")
        self.api = raw.get("api", "")
        self.token = _resolve_token(raw.get("token", ""))

        # Local endpoint fields
        self.path = raw.get("path", "")
        self.tag_pattern = raw.get("tag_pattern", "*") if is_source else None

        # Branch (target only by default; also used in source commit-mode to pick branch tip)
        self.branch = raw.get("branch", "main" if not is_source else "")

        # Source-side release filtering (remote only, ignored for local)
        self.include_prereleases = raw.get("include_prereleases", False)
        self.include_drafts = raw.get("include_drafts", False)

        # Optional: only sync releases from this tag onwards (no backfilling needed)
        # In commit mode this should be a commit SHA instead of a tag name.
        self.sync_from = raw.get("sync_from", "")

        # Release list source (remote sources): "release" (API releases,
        # default), "tag" (git tags treated as releases), or "commit"
        # (sync latest HEAD commit; sync_from must be a commit SHA).
        self.mode = raw.get("mode", "release")

    @property
    def is_remote(self):
        return self.type in ("gitea", "github")

    @property
    def owner(self):
        return self.repo.split("/")[0] if "/" in self.repo else ""

    @property
    def clone_url(self):
        """HTTPS clone URL with token embedded for auth."""
        if not self.api or not self.repo:
            return ""
        raw_host = self.api.split("://")[1].split("/")[0] if "://" in self.api else self.api
        # GitHub's API lives at api.github.com, but its git host is github.com.
        # Gitea's API typically lives on the same host as git, so no transform needed.
        host = "github.com" if raw_host == "api.github.com" else raw_host
        return f"https://{self.owner}:{self.token}@{host}/{self.repo}.git"


@final
class ProjectConfig:
    def __init__(self, raw):
        if not isinstance(raw, dict):
            raise TypeError(
                f"Project entry must be a map/dict (got {type(raw).__name__})"
            )
        name = raw.get("name")
        if not name:
            raise ValueError("Project is missing a required 'name'")
        self.name = name
        self.enabled = bool(raw.get("enabled", True))
        raw_source = raw.get("source", {})
        raw_target = raw.get("target", {})
        self.source = _EndpointConfig(raw_source, is_source=True)
        self.target = _EndpointConfig(raw_target, is_source=False)
        self.renderer = _RendererConfig(raw.get("renderer", {}))
        self.retry = _RetryConfig(raw.get("retry", {}))

        # Check for misplaced or unknown keys at the project level
        for k in raw:
            if k in VALID_PROJECT_KEYS:
                continue
            if k in KNOWN_SOURCE_KEYS:
                logger.warning(
                    "Project '%s': '%s' was specified at the project level, but must be configured under '%s:' (e.g. %s.%s: %s).",
                    self.name,
                    k,
                    KNOWN_SOURCE_KEYS[k],
                    KNOWN_SOURCE_KEYS[k],
                    k,
                    raw[k],
                )
            elif k in KNOWN_ENDPOINT_KEYS:
                logger.warning(
                    "Project '%s': '%s' was specified at the project level, but belongs under '%s:'.",
                    self.name,
                    k,
                    KNOWN_ENDPOINT_KEYS[k],
                )
            else:
                logger.warning(
                    "Project '%s': unrecognized configuration key '%s'.",
                    self.name,
                    k,
                )

        # preserve_description — single cascade: project → source → target → True
        self.preserve_description = _cascade(
            raw, raw_source, raw_target,
            keys=["preserve_description"],
            default=True,
        )

        # sync_assets — single cascade: project → source → target → False
        self.sync_assets = _cascade(
            raw, raw_source, raw_target,
            keys=["sync_assets"],
            default=False,
        )

        # stream_assets — project-level only (no endpoint-level alias)
        # When True, asset uploads stream from disk instead of buffering in RAM.
        # Default is False (RAM) to preserve existing behaviour.
        self.stream_assets = bool(raw.get("stream_assets", False))

        # commit_message — project-level commit message template (e.g. "chore(sync): {tag}")
        self.commit_message = raw.get("commit_message") or raw.get("commit_template") or None

        # release_description — project-level release description template
        self.release_description = (
            raw.get("release_description")
            or raw.get("release_notes_template")
            or raw.get("description_template")
            or None
        )

    def __repr__(self):
        return (
            f"ProjectConfig(name={self.name!r}, "
            f"source={self.source.type} -> target={self.target.type}, "
            f"enabled={self.enabled})"
        )


@final
class _AuthorConfig:
    def __init__(self, raw):
        self.name = raw.get("name", "")
        self.email = raw.get("email", "")

    @property
    def enabled(self):
        return bool(self.name) or bool(self.email)


@final
class _RendererConfig:
    def __init__(self, raw):
        self.ignore = raw.get("ignore", [])
        self.operations = raw.get("operations", [])
        raw_author = raw.get("author")
        self.author = _AuthorConfig(raw_author) if raw_author else _AuthorConfig({})


@final
class _RetryConfig:
    def __init__(self, raw):
        self.max_attempts = raw.get("max_attempts", 3)
        self.backoff_seconds = raw.get("backoff_seconds", 2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cascade(raw_project, raw_source, raw_target, keys, default):
    """Return the first value found for any of *keys* across project, source, target.

    Lookup order: project-level first (highest priority), then source-level,
    then target-level, then *default*.  All alias keys are checked at each
    level before moving to the next — so a project-level alias wins over a
    source-level primary key.
    """
    for raw in (raw_project, raw_source, raw_target):
        for k in keys:
            if k in raw:
                return raw[k]
    return default


def _resolve_token(val):
    if val.startswith("${") and val.endswith("}"):
        return os.environ.get(val[2:-1], "")
    return val
