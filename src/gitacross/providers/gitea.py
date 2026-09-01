"""Gitea provider — REST API client for Gitea instances."""
from __future__ import annotations

import io
import json
import logging
import shutil
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .base import BaseAPIClient

logger = logging.getLogger(__name__)


class _MultipartReader:
    """File-like object that streams multipart/form-data without buffering the attachment.

    Yields: boundary_header_bytes → file_content_chunks → boundary_footer_bytes.
    The caller computes ``Content-Length = len(header) + file_size + len(footer)``
    upfront so HTTP/1.1 chunked-encoding is not required.
    """

    def __init__(self, header: bytes, file_path, footer: bytes):
        # Keep the file handle open for the lifetime of the reader (streaming);
        # it is closed in close()/__exit__. A context manager would defeat this.
        self._parts = [
            io.BytesIO(header),
            open(file_path, "rb"),  # noqa: SIM115
            io.BytesIO(footer),
        ]
        self._idx = 0

    def read(self, size=-1):
        if size == 0:
            return b""
        chunks = []
        remaining = size
        while self._idx < len(self._parts):
            chunk = self._parts[self._idx].read(remaining if remaining > 0 else -1)
            if chunk:
                chunks.append(chunk)
                if remaining > 0:
                    remaining -= len(chunk)
                    if remaining <= 0:
                        break
            else:
                self._idx += 1
        return b"".join(chunks)

    def close(self):
        for part in self._parts:
            try:
                part.close()
            except Exception:  # noqa: S110, BLE001 — close() must never raise during cleanup
                pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class GiteaClient(BaseAPIClient):
    """Gitea REST API client.

    Differences from the base:
    - Auth header uses ``token <tok>`` (not Bearer)
    - Release conflict code is 409 (not 422)
    - Asset upload uses multipart/form-data
    - Asset download falls back to constructing a URL from asset IDs
    """

    _platform_name = "Gitea"
    _conflict_codes = (409, 422)
    _release_conflict_code = 409

    def __init__(self, api: str, repo: str, token: str):
        super().__init__(api, repo, token)
        self._headers = {
            "Authorization": f"token {token}",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    # Asset management
    # ------------------------------------------------------------------

    def download_asset(self, asset, dest):
        """Download a Gitea release asset to *dest*."""
        url = asset.get("browser_download_url") or ""
        if not url and asset.get("id") and asset.get("release_id"):
            url = (
                f"{self.api}/repos/{self.repo}/releases"
                f"/{asset['release_id']}/assets/{asset['id']}"
            )
        if not url:
            raise ValueError(f"Asset has no download URL: {asset}")
        # Gitea can return a relative URL for self-hosted instances
        if url.startswith("/"):
            host_base = self.api.split("/api")[0]
            url = f"{host_base}{url}"
        headers = dict(self._headers)
        headers["Accept"] = "*/*"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)

    def upload_asset(self, release_or_id, file_path, name=None, stream=False):
        """Upload an asset to a Gitea release using multipart/form-data.

        Idempotent — returns ``None`` if the asset already exists (409/422).

        When *stream* is ``True`` the file content is never fully loaded into
        RAM: a ``_MultipartReader`` wraps the boundary bytes + file handle and
        is passed directly to ``urllib``. ``Content-Length`` is pre-computed
        from ``len(header) + file_size + len(footer)`` so chunked encoding is
        not needed.
        """
        name = name or Path(file_path).name
        rel_id = (
            release_or_id.get("id")
            if isinstance(release_or_id, dict)
            else release_or_id
        )
        url = (
            f"{self.api}/repos/{self.repo}/releases/{rel_id}/assets"
            f"?name={urllib.parse.quote(name)}"
        )
        boundary = f"----GitAcrossBoundary{uuid.uuid4().hex}"
        header_bytes = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="attachment"; filename="{name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        footer_bytes = f"\r\n--{boundary}--\r\n".encode()

        req_headers = {
            "Authorization": self._headers["Authorization"],
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }

        try:
            if stream:
                file_size = Path(file_path).stat().st_size
                content_length = len(header_bytes) + file_size + len(footer_bytes)
                req_headers["Content-Length"] = str(content_length)
                with _MultipartReader(header_bytes, file_path, footer_bytes) as body:
                    req = urllib.request.Request(
                        url, data=body, headers=req_headers, method="POST"
                    )
                    with urllib.request.urlopen(req) as resp:
                        raw = resp.read()
                        return json.loads(raw) if raw else None
            else:
                with open(file_path, "rb") as f:
                    file_bytes = f.read()
                body = header_bytes + file_bytes + footer_bytes
                req_headers["Content-Length"] = str(len(body))
                req = urllib.request.Request(
                    url, data=body, headers=req_headers, method="POST"
                )
                with urllib.request.urlopen(req) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code in (409, 422):
                logger.info("Asset %s already exists on Gitea (idempotent)", name)
                return None
            body_text = e.read().decode()
            logger.error("Gitea upload asset error %s: %s", e.code, body_text)
            raise
