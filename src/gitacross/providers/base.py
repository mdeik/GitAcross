"""Base API client — shared implementation for all remote git hosting providers.

All HTTP, repository-management, and release-management logic lives here.
Provider subclasses only need to override:
  - ``_headers``               : dict of default HTTP request headers
  - ``_platform_name``         : human-readable name used in error messages
  - ``_conflict_codes``        : HTTP codes that mean "repo already exists"
  - ``_release_conflict_code`` : HTTP code meaning "release already exists"
  - ``download_asset``         : provider-specific download logic
  - ``upload_asset``           : provider-specific upload logic (RAM or stream)

Adding a brand-new provider therefore requires writing only those six items.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import ClassVar

logger = logging.getLogger(__name__)


class BaseAPIClient:
    """Shared REST API behaviour for all remote provider clients."""

    _platform_name: str = "Remote"
    _conflict_codes: tuple[int, ...] = (409, 422)
    _release_conflict_code: int = 422
    # Subclasses override this in their own __init__ (see class docstring).
    _headers: ClassVar[dict[str, str]] = {}

    def __init__(self, api: str, repo: str, token: str):
        self.api: str = api.rstrip("/")
        self.repo: str = repo
        self.token: str = token
        # Subclasses must set self._headers in their own __init__.

    # ------------------------------------------------------------------
    # Core HTTP helpers
    # ------------------------------------------------------------------

    def _request(self, method, path, data=None):
        """Make a JSON request relative to /repos/{repo}/{path}."""
        url = f"{self.api}/repos/{self.repo}"
        if path:
            url += f"/{path}"
        body = json.dumps(data).encode() if data else None
        headers = dict(self._headers)
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            body_text = e.read().decode()
            logger.error("%s API error %s: %s", self._platform_name, e.code, body_text)
            raise

    def _request_url(self, method, url, data=None):
        """Make a JSON request to an arbitrary absolute URL."""
        body = json.dumps(data).encode() if data else None
        headers = dict(self._headers)
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            body_text = e.read().decode()
            logger.error(
                "%s API error %s %s: %s",
                self._platform_name, method, url, body_text,
            )
            raise

    # ------------------------------------------------------------------
    # Repository management
    # ------------------------------------------------------------------

    def ensure_repo_exists(self, create=True):
        """Ensure the repo exists on the platform, creating it if not.

        Returns the repo metadata dict (includes clone_url, ssh_url, etc.).
        Raises ``RuntimeError`` on auth errors or inaccessible repos.
        Pass ``create=False`` to check for existence without ever creating
        the repo (used by dry-run mode, which must not mutate anything).
        """
        try:
            return self._request("GET", "")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                if not create:
                    raise RuntimeError(
                        f"{self._platform_name} repo '{self.repo}' does not exist "
                        + "(dry-run mode creates nothing)"
                    ) from e
                logger.info(
                    "%s repo '%s' not found — creating it automatically",
                    self._platform_name,
                    self.repo,
                )
                return self._create_repo()
            hint = {
                401: "the token is invalid or expired",
                403: "the token lacks the required permissions",
            }.get(e.code, f"unexpected HTTP {e.code}")
            raise RuntimeError(
                f"{self._platform_name} repo '{self.repo}' is not accessible "
                + f"(HTTP {e.code}): {hint}."
            ) from e

    def _create_repo(self):
        """Create the repository as private and return its metadata.

        Uses ``/user/repos`` for personal accounts, ``/orgs/{org}/repos`` for orgs.
        Raises ``RuntimeError`` on conflict (repo exists privately with wrong token scope).
        """
        owner, name = self.repo.split("/", 1)
        payload = {
            "name": name,
            "private": True,   # safe default — user can change visibility later
            "auto_init": False,
        }
        # Determine whether the owner is the authenticated user or an org.
        try:
            user_info = self._request_url("GET", f"{self.api}/user") or {}
            is_user = user_info.get("login", "").lower() == owner.lower()
        except (urllib.error.HTTPError, OSError):
            is_user = True  # fall back to user endpoint on transient errors

        url = (
            f"{self.api}/user/repos"
            if is_user
            else f"{self.api}/orgs/{owner}/repos"
        )

        body = json.dumps(payload).encode()
        headers = dict(self._headers)
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                meta = json.loads(raw) if raw else {}
                logger.info(
                    "Created %s repo '%s' (private) at %s",
                    self._platform_name,
                    self.repo,
                    meta.get("html_url", ""),
                )
                return meta
        except urllib.error.HTTPError as e:
            body_text = e.read().decode()
            if e.code in self._conflict_codes:
                raise RuntimeError(
                    f"{self._platform_name} repo '{self.repo}' could not be created "
                    + f"(HTTP {e.code}): the repository may already exist as a private "
                    + "repo that this token cannot access. Ensure the token has "
                    + "read/write access to the repo."
                ) from e
            logger.error(
                "Failed to create %s repo: %s %s",
                self._platform_name, e.code, body_text,
            )
            raise RuntimeError(
                f"Could not create {self._platform_name} repo '{self.repo}' "
                + f"(HTTP {e.code}): {body_text}"
            ) from e

    # ------------------------------------------------------------------
    # Release management
    # ------------------------------------------------------------------

    def list_releases(self, page=1, limit=50):
        """list releases (paginated). Override in subclasses if the query-param name differs."""
        return self._request("GET", f"releases?page={page}&limit={limit}")

    def get_release_by_tag(self, tag):
        """Get a release by tag name. Returns dict or None if not found (404)."""
        try:
            return self._request("GET", f"releases/tags/{tag}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def list_release_assets(self, release_id):
        """list assets for a release ID. Returns [] if not found (404)."""
        try:
            return self._request("GET", f"releases/{release_id}/assets") or []
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            raise

    def create_release(self, tag, name, body, prerelease=False):
        """Create a release. Idempotent: returns None if already exists."""
        try:
            return self._request(
                "POST",
                "releases",
                {
                    "tag_name": tag,
                    "name": name,
                    "body": body,
                    "prerelease": prerelease,
                },
            )
        except urllib.error.HTTPError as e:
            if e.code == self._release_conflict_code:
                logger.info(
                    "Release %s already exists on %s (idempotent)",
                    tag, self._platform_name,
                )
                return None
            raise

    # ------------------------------------------------------------------
    # Asset management — implemented by each provider
    # ------------------------------------------------------------------

    def download_asset(self, _asset, _dest):
        """Download a release asset to *dest* path. Must be overridden."""
        raise NotImplementedError

    def upload_asset(self, _release_or_id, _file_path, _name=None, _stream=False):
        """Upload an asset to a release. Must be overridden.

        When *stream* is ``True`` the file should be streamed from disk rather
        than fully buffered in RAM — useful for large prebuilt binaries.
        """
        raise NotImplementedError
