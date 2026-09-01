"""GitHub provider — REST API client for github.com and GitHub Enterprise."""
from __future__ import annotations


import json
import logging
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .base import BaseAPIClient

logger = logging.getLogger(__name__)


class GitHubClient(BaseAPIClient):
    """GitHub REST API client.

    Differences from the base:
    - Auth header uses ``Bearer <tok>``
    - ``Accept: application/vnd.github+json``
    - Release pagination uses ``per_page`` (not ``limit``)
    - Release conflict code is 422 (not 409)
    - Asset upload uses raw ``application/octet-stream`` to the upload endpoint
    - Asset download requires ``Accept: application/octet-stream``
    """

    _platform_name = "GitHub"
    _conflict_codes = (409, 422)
    _release_conflict_code = 422

    def __init__(self, api: str, repo: str, token: str):
        super().__init__(api, repo, token)
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }

    def list_releases(self, page=1, limit=50):
        """GitHub uses ``per_page`` instead of ``limit``."""
        return self._request("GET", f"releases?page={page}&per_page={limit}")

    # ------------------------------------------------------------------
    # Asset management
    # ------------------------------------------------------------------

    def download_asset(self, asset, dest):
        """Download a GitHub release asset to *dest*."""
        url = asset.get("url") or asset.get("browser_download_url")
        if not url:
            raise ValueError(f"Asset has no download URL: {asset}")
        headers = dict(self._headers)
        headers["Accept"] = "application/octet-stream"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)

    def upload_asset(self, release_or_id, file_path, name=None, stream=False):
        """Upload an asset to a GitHub release as raw ``application/octet-stream``.

        Idempotent — returns ``None`` if the asset already exists (422).

        When *stream* is ``True`` the file object is passed directly to
        ``urllib`` so the content is read in chunks rather than all at once.
        ``Content-Length`` is set from ``file.stat().st_size`` so no
        chunked-encoding handshake is needed.
        """
        name = name or Path(file_path).name
        upload_url = None
        if isinstance(release_or_id, dict):
            upload_url = release_or_id.get("upload_url")
            rel_id = release_or_id.get("id")
        else:
            rel_id = release_or_id

        # Resolve the upload base URL (prefer the API-returned upload_url).
        if upload_url:
            base_url = upload_url.split("{")[0]
        elif self.api == "https://api.github.com":
            base_url = (
                f"https://uploads.github.com/repos/{self.repo}/releases/{rel_id}/assets"
            )
        else:
            base_url = f"{self.api}/repos/{self.repo}/releases/{rel_id}/assets"

        url = f"{base_url}?name={urllib.parse.quote(name)}"
        req_headers = {
            "Authorization": self._headers["Authorization"],
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/octet-stream",
        }

        try:
            if stream:
                file_size = Path(file_path).stat().st_size
                req_headers["Content-Length"] = str(file_size)
                with open(file_path, "rb") as f:
                    req = urllib.request.Request(
                        url, data=f, headers=req_headers, method="POST"
                    )
                    with urllib.request.urlopen(req) as resp:
                        raw = resp.read()
                        return json.loads(raw) if raw else None
            else:
                with open(file_path, "rb") as f:
                    data = f.read()
                req_headers["Content-Length"] = str(len(data))
                req = urllib.request.Request(
                    url, data=data, headers=req_headers, method="POST"
                )
                with urllib.request.urlopen(req) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 422:
                logger.info("Asset %s already exists on GitHub (idempotent)", name)
                return None
            body_text = e.read().decode()
            logger.error("GitHub upload asset error %s: %s", e.code, body_text)
            raise
