"""Target endpoint factory — commits, tags, pushes, and creates releases.

Supported types: gitea, github, local.
"""
from __future__ import annotations


import logging
from pathlib import Path

from .git import GitRepo
from .providers import get_api_client
from .retry import retry

logger = logging.getLogger(__name__)


def create_target(config, cache_dir, retry_max=3, retry_backoff=2, author=None):
    """Factory: build a Target endpoint from the config."""
    t = config.type
    if t == "local":
        return _LocalTarget(config, author=author)
    if t in ("gitea", "github"):
        return _RemoteTarget(config, cache_dir, retry_max, retry_backoff, author=author)
    raise ValueError(f"Unknown target type: {t}")


def _resolve_author(author):
    """Return ``(name, email)`` from an author config, or ``(None, None)`` if unset."""
    if author and author.enabled:
        return author.name or None, author.email or None
    return None, None


# ---------------------------------------------------------------------------
# Target interface (duck-typed)
# Required: setup(branch), commit(wd, msg), tag(name, msg), push(br, tag),
#           create_release(tag, name, body, prerelease), tag_exists(tag),
#           head_sha(), repo_path
# ---------------------------------------------------------------------------


class _RemoteTarget:
    """Target backed by a remote API + a bare git mirror."""

    def __init__(self, config, cache_dir, retry_max, retry_backoff, author=None):
        self._api = get_api_client(config.type, config.api, config.repo, config.token)
        repo_meta = self._api.ensure_repo_exists()
        # Derive clone URL: prefer explicit config value, fall back to API-returned URL.
        clone_url = getattr(config, "clone_url", None) or (
            repo_meta.get("clone_url") if repo_meta else None
        )
        if not clone_url:
            raise RuntimeError(
                f"Could not determine clone URL for '{config.repo}'. "
                "set clone_url in the target config or ensure the API returns it."
            )
        repo_slug = config.repo.replace("/", "_")
        self._git = GitRepo.ensure_mirror(
            clone_url,
            Path(cache_dir) / f"target_{config.type}_{repo_slug}.git",
        )
        self._retry_max = retry_max
        self._retry_backoff = retry_backoff
        self._author = author

    def setup(self, branch):
        self._git.ensure_branch(branch)

    def commit(self, work_dir, message, date=None):
        author_name, author_email = _resolve_author(self._author)
        return self._git.commit(
            work_dir, message,
            date=date, author_name=author_name, author_email=author_email,
        )

    def tag(self, name, message):
        self._git.tag(name, message)

    def push(self, branch, tag=None):
        refs = [branch]
        if tag:
            refs.append(f"+refs/tags/{tag}")
        retry(
            lambda: self._git.push("origin", *refs),
            max_attempts=self._retry_max,
            backoff_seconds=self._retry_backoff,
        )

    def create_release(self, tag, name, body, prerelease=False):
        return retry(
            lambda: self._api.create_release(tag, name, body, prerelease),
            max_attempts=self._retry_max,
            backoff_seconds=self._retry_backoff,
        )

    def upload_release_asset(self, tag, file_path, name=None, release=None, stream=False):
        if not release:
            release = self._api.get_release_by_tag(tag)
        if not release:
            logger.warning(
                "Release %s not found on target — skipping asset upload", tag
            )
            return None
        return retry(
            lambda: self._api.upload_asset(release, file_path, name=name, stream=stream),
            max_attempts=self._retry_max,
            backoff_seconds=self._retry_backoff,
        )

    def tag_exists(self, tag):
        return self._git.tag_exists(tag)

    def head_sha(self):
        return self._git.head_sha()

    @property
    def repo_path(self):
        return self._git.git_dir


class _LocalTarget:
    """Target backed by a local git repo — no push, no API release."""

    def __init__(self, config, author=None):
        self._git = GitRepo.local(config.path)
        self._author = author

    def setup(self, branch):
        self._git.ensure_branch(branch)

    def commit(self, work_dir, message, date=None):
        author_name, author_email = _resolve_author(self._author)
        result = self._git.commit(
            work_dir, message,
            date=date, author_name=author_name, author_email=author_email,
        )
        self._git.reset_worktree()
        return result

    def tag(self, name, message):
        self._git.tag(name, message)

    def push(self, branch, tag=None):
        pass  # local — nothing to push

    def create_release(self, tag, name, body, prerelease=False):
        pass  # local — no API release to create

    def upload_release_asset(self, tag, file_path, name=None, release=None, stream=False):
        pass  # local — no API release to upload assets to

    def tag_exists(self, tag):
        return self._git.tag_exists(tag)

    def head_sha(self):
        return self._git.head_sha()

    @property
    def repo_path(self):
        return self._git.git_dir
