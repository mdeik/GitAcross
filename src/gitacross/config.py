import logging
import os

import yaml

logger = logging.getLogger(__name__)

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


class Config:
    def __init__(self, config_path):
        with open(config_path) as f:
            raw = yaml.safe_load(f)
        if isinstance(raw, list):
            # Flat list at top level: [ {name:..., source:..., ...} ]
            raw = {"projects": raw}
        elif raw is None:
            raw = {"projects": []}
        self.projects = [ProjectConfig(p) for p in (raw.get("projects") or [])]

    @classmethod
    def from_path(cls, config_path):
        return cls(config_path)

    def __repr__(self):
        return f"Config(projects={len(self.projects)})"


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


class ProjectConfig:
    def __init__(self, raw):
        if not isinstance(raw, dict):
            raise ValueError(
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
            if k in _VALID_PROJECT_KEYS:
                continue
            if k in _KNOWN_SOURCE_KEYS:
                logger.warning(
                    "Project '%s': '%s' was specified at the project level, but must be configured under '%s:' (e.g. %s.%s: %s).",
                    self.name,
                    k,
                    _KNOWN_SOURCE_KEYS[k],
                    _KNOWN_SOURCE_KEYS[k],
                    k,
                    raw[k],
                )
            elif k in _KNOWN_ENDPOINT_KEYS:
                logger.warning(
                    "Project '%s': '%s' was specified at the project level, but belongs under '%s:'.",
                    self.name,
                    k,
                    _KNOWN_ENDPOINT_KEYS[k],
                )
            else:
                logger.warning(
                    "Project '%s': unrecognized configuration key '%s'.",
                    self.name,
                    k,
                )

        # preserve_description — single cascade: project → source → target → True
        # Supports aliases: preserve_release_description (legacy)
        self.preserve_description = _cascade(
            raw, raw_source, raw_target,
            keys=["preserve_description", "preserve_release_description"],
            default=True,
        )

        # sync_assets — single cascade: project → source → target → False
        # Supports aliases: preserve_assets, include_assets (legacy)
        self.sync_assets = _cascade(
            raw, raw_source, raw_target,
            keys=["sync_assets", "preserve_assets", "include_assets"],
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


class _AuthorConfig:
    def __init__(self, raw):
        self.name = raw.get("name", "")
        self.email = raw.get("email", "")

    @property
    def enabled(self):
        return bool(self.name) or bool(self.email)


class _RendererConfig:
    def __init__(self, raw):
        self.ignore = raw.get("ignore", [])
        self.operations = raw.get("operations", [])
        raw_author = raw.get("author")
        self.author = _AuthorConfig(raw_author) if raw_author else _AuthorConfig({})


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
