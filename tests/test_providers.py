"""Tests for remote API clients — split from the original single-file suite."""
from __future__ import annotations

import json
import tempfile
import urllib.error
from pathlib import Path
from unittest import mock

from tests.conftest import (
    _FakeHTTPResponse,
    _http_error,
)

# ---------------------------------------------------------------------------
# Remote client access checks
# ---------------------------------------------------------------------------



def test_github_ensure_repo_exists_ok():
    from gitacross.providers.github import GitHubClient

    client = GitHubClient("https://api.github.com", "owner/repo", "token")
    with mock.patch(
        "gitacross.providers.base.urllib.request.urlopen", return_value=_FakeHTTPResponse()
    ):
        _ = client.ensure_repo_exists()  # should not raise
    print("  ✓ github: ensure_repo_exists accepts a reachable repo")


def test_github_ensure_repo_exists_missing_repo_creates_it():
    """When the repo 404s, ensure_repo_exists should create it and return metadata."""
    from gitacross.providers.github import GitHubClient

    client = GitHubClient("https://api.github.com", "owner/repo", "token")
    repo_meta = {"clone_url": "https://github.com/owner/repo.git", "html_url": "https://github.com/owner/repo"}
    user_meta = {"login": "owner"}

    call_count = 0

    def fake_urlopen(_req):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call: GET /repos/owner/repo → 404
            raise _http_error(404)
        if call_count == 2:
            # Second call: GET /user → authenticated user
            return _FakeHTTPResponse(json.dumps(user_meta).encode())
        # Third call: POST /user/repos → created
        return _FakeHTTPResponse(json.dumps(repo_meta).encode())

    with mock.patch("gitacross.providers.base.urllib.request.urlopen", side_effect=fake_urlopen):
        result = client.ensure_repo_exists()
    assert result is not None
    assert result["clone_url"] == repo_meta["clone_url"]
    print("  ✓ github: ensure_repo_exists auto-creates a missing repo")


def test_github_ensure_repo_exists_missing_repo_creates_it_org():
    """When the owner is an org (not the token user), use /orgs/{org}/repos."""
    from gitacross.providers.github import GitHubClient

    client = GitHubClient("https://api.github.com", "my-org/repo", "token")
    repo_meta = {"clone_url": "https://github.com/my-org/repo.git", "html_url": "https://github.com/my-org/repo"}
    user_meta = {"login": "not-the-org-owner"}
    created_urls = []

    call_count = 0

    def fake_urlopen(req):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _http_error(404)
        if call_count == 2:
            return _FakeHTTPResponse(json.dumps(user_meta).encode())
        created_urls.append(req.full_url)
        return _FakeHTTPResponse(json.dumps(repo_meta).encode())

    with mock.patch("gitacross.providers.base.urllib.request.urlopen", side_effect=fake_urlopen):
        result = client.ensure_repo_exists()
    assert result is not None
    assert result["clone_url"] == repo_meta["clone_url"]
    assert any("/orgs/my-org/repos" in u for u in created_urls), "should POST to org endpoint"
    print("  ✓ github: ensure_repo_exists auto-creates a missing org repo")


def test_github_create_repo_is_private():
    """Auto-created repos must be private=True in the POST payload."""
    import json as _json

    from gitacross.providers.github import GitHubClient

    client = GitHubClient("https://api.github.com", "owner/newrepo", "token")
    user_meta = {"login": "owner"}
    repo_meta = {"clone_url": "https://github.com/owner/newrepo.git", "html_url": "..."}
    posted_bodies = []

    call_count = 0

    def fake_urlopen(req):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _http_error(404)
        if call_count == 2:
            return _FakeHTTPResponse(_json.dumps(user_meta).encode())
        posted_bodies.append(_json.loads(req.data))
        return _FakeHTTPResponse(_json.dumps(repo_meta).encode())

    with mock.patch("gitacross.providers.base.urllib.request.urlopen", side_effect=fake_urlopen):
        _ = client.ensure_repo_exists()
    assert posted_bodies, "should have POSTed a create-repo body"
    assert posted_bodies[0]["private"] is True, "repo should be created as private"
    print("  ✓ github: auto-created repo uses private=True")


def test_github_create_repo_404_then_422_private_collision():
    """If creation returns 422, raise a clear 'private repo collision' error."""
    from gitacross.providers.github import GitHubClient

    client = GitHubClient("https://api.github.com", "owner/repo", "token")
    user_meta = {"login": "owner"}

    call_count = 0

    def fake_urlopen(_req):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _http_error(404)   # repo not visible (private + bad token)
        if call_count == 2:
            return _FakeHTTPResponse(json.dumps(user_meta).encode())
        raise _http_error(422)       # name already taken → repo exists privately

    with mock.patch("gitacross.providers.base.urllib.request.urlopen", side_effect=fake_urlopen):
        try:
            _ = client.ensure_repo_exists()
            assert False, "should raise"
        except RuntimeError as e:
            msg = str(e)
            assert "already exist" in msg or "private" in msg
            assert "repo" in msg.lower()
    print("  ✓ github: 404 → 422 raises actionable private-repo-collision error")


def test_github_ensure_repo_exists_401_raises_directly():
    """401 should raise immediately without ever attempting repo creation."""
    from gitacross.providers.github import GitHubClient

    client = GitHubClient("https://api.github.com", "owner/repo", "token")
    call_count = 0

    def fake_urlopen(_req):
        nonlocal call_count
        call_count += 1
        raise _http_error(401)

    with mock.patch("gitacross.providers.base.urllib.request.urlopen", side_effect=fake_urlopen):
        try:
            _ = client.ensure_repo_exists()
            assert False, "should raise"
        except RuntimeError as e:
            assert "invalid or expired" in str(e)
    assert call_count == 1, "should not retry or call create after 401"
    print("  ✓ github: 401 raises immediately without attempting creation")


def test_gitea_ensure_repo_exists_bad_token():
    from gitacross.providers.gitea import GiteaClient

    client = GiteaClient("https://git.example.com/api/v1", "owner/repo", "token")
    with mock.patch(
        "gitacross.providers.base.urllib.request.urlopen", side_effect=_http_error(401)
    ):
        try:
            _ = client.ensure_repo_exists()
            assert False, "should raise"
        except RuntimeError as e:
            assert "invalid or expired" in str(e)
    print("  ✓ gitea: ensure_repo_exists explains a bad token")


def test_gitea_create_repo_is_private():
    """Auto-created Gitea repos must be private=True in the POST payload."""
    import json as _json

    from gitacross.providers.gitea import GiteaClient

    client = GiteaClient("https://git.example.com/api/v1", "owner/newrepo", "token")
    user_meta = {"login": "owner"}
    repo_meta = {"clone_url": "https://git.example.com/owner/newrepo.git", "html_url": "..."}
    posted_bodies = []

    call_count = 0

    def fake_urlopen(req):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _http_error(404)
        if call_count == 2:
            return _FakeHTTPResponse(_json.dumps(user_meta).encode())
        posted_bodies.append(_json.loads(req.data))
        return _FakeHTTPResponse(_json.dumps(repo_meta).encode())

    with mock.patch("gitacross.providers.base.urllib.request.urlopen", side_effect=fake_urlopen):
        _ = client.ensure_repo_exists()
    assert posted_bodies, "should have POSTed a create-repo body"
    assert posted_bodies[0]["private"] is True, "repo should be created as private"
    print("  ✓ gitea: auto-created repo uses private=True")


def test_gitea_create_repo_404_then_409_private_collision():
    """If Gitea creation returns 409 Conflict, raise a clear private repo collision error."""
    from gitacross.providers.gitea import GiteaClient

    client = GiteaClient("https://git.example.com/api/v1", "owner/repo", "token")
    user_meta = {"login": "owner"}

    call_count = 0

    def fake_urlopen(_req):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _http_error(404)
        if call_count == 2:
            return _FakeHTTPResponse(json.dumps(user_meta).encode())
        raise _http_error(409)

    with mock.patch("gitacross.providers.base.urllib.request.urlopen", side_effect=fake_urlopen):
        try:
            _ = client.ensure_repo_exists()
            assert False, "should raise"
        except RuntimeError as e:
            msg = str(e)
            assert "already exist" in msg or "private" in msg
            assert "token" in msg.lower()
    print("  ✓ gitea: 404 → 409 raises actionable private-repo-collision error")


def test_gitea_ensure_repo_exists_missing_repo_creates_it():
    """When the Gitea repo 404s, ensure_repo_exists should create it."""
    from gitacross.providers.gitea import GiteaClient

    client = GiteaClient("https://git.example.com/api/v1", "owner/repo", "token")
    repo_meta = {"clone_url": "https://git.example.com/owner/repo.git", "html_url": "https://git.example.com/owner/repo"}
    user_meta = {"login": "owner"}

    call_count = 0

    def fake_urlopen(_req):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _http_error(404)
        if call_count == 2:
            return _FakeHTTPResponse(json.dumps(user_meta).encode())
        return _FakeHTTPResponse(json.dumps(repo_meta).encode())

    with mock.patch("gitacross.providers.base.urllib.request.urlopen", side_effect=fake_urlopen):
        result = client.ensure_repo_exists()
    assert result is not None
    assert result["clone_url"] == repo_meta["clone_url"]
    print("  ✓ gitea: ensure_repo_exists auto-creates a missing repo")


def test_ensure_repo_exists_create_false_does_not_create():
    """ensure_repo_exists(create=False) must raise on a missing repo without creating it.

    This is the dry-run path: a missing repo must never be auto-created.
    """
    from gitacross.providers.gitea import GiteaClient

    client = GiteaClient("https://git.example.com/api/v1", "owner/missing", "token")

    calls = {"n": 0}

    def fake_urlopen(_req):
        calls["n"] += 1
        raise _http_error(404)

    with mock.patch(
        "gitacross.providers.base.urllib.request.urlopen", side_effect=fake_urlopen
    ):
        try:
            _ = client.ensure_repo_exists(create=False)
            assert False, "should have raised"
        except RuntimeError as e:
            assert "does not exist" in str(e)

    # Only the existence GET was made — no repo-creation request followed
    assert calls["n"] == 1
    print("  ✓ ensure_repo_exists(create=False) raises without creating the repo")


def test_api_request_re_raises_http_errors():
    """_request must log the error body and re-raise the HTTPError."""
    from gitacross.providers.github import GitHubClient

    client = GitHubClient("https://api.github.com", "owner/repo", "token")
    with mock.patch(
        "gitacross.providers.base.urllib.request.urlopen", side_effect=_http_error(500)
    ):
        try:
            _ = client._request("GET", "")
            assert False, "should have raised"
        except urllib.error.HTTPError:
            pass
    print("  ✓ base: _request re-raises HTTP errors")


def test_create_release_idempotent_and_error_paths():
    """create_release is idempotent on conflict and re-raises other errors."""
    from gitacross.providers.github import GitHubClient

    client = GitHubClient("https://api.github.com", "owner/repo", "token")
    requests = []

    def fake_urlopen(req):
        requests.append(req)
        raise _http_error(422)  # GitHub's release-conflict code

    with mock.patch(
        "gitacross.providers.base.urllib.request.urlopen", side_effect=fake_urlopen
    ):
        result = client.create_release("v1.0.0", "v1.0.0", "body", False)

    # Idempotent: conflict means "already exists" → None
    assert result is None
    # POST carried a JSON body with the correct content type
    # (urllib normalizes header-name case, so compare case-insensitively)
    header_keys = {k.lower(): v for k, v in requests[0].headers.items()}
    assert header_keys.get("content-type") == "application/json"
    assert requests[0].data is not None

    # Non-conflict errors propagate
    with mock.patch(
        "gitacross.providers.base.urllib.request.urlopen", side_effect=_http_error(500)
    ):
        try:
            _ = client.create_release("v1.0.0", "v1.0.0", "body", False)
            assert False, "should have raised"
        except urllib.error.HTTPError:
            pass
    print("  ✓ base: create_release idempotent on conflict, re-raises other errors")


def test_get_release_by_tag_404_returns_none():
    """get_release_by_tag returns None on 404 (missing release)."""
    from gitacross.providers.github import GitHubClient

    client = GitHubClient("https://api.github.com", "owner/repo", "token")
    with mock.patch(
        "gitacross.providers.base.urllib.request.urlopen", side_effect=_http_error(404)
    ):
        assert client.get_release_by_tag("v1.0.0") is None
    print("  ✓ base: get_release_by_tag 404 → None")


def test_list_release_assets_404_returns_empty():
    """list_release_assets returns [] on 404."""
    from gitacross.providers.github import GitHubClient

    client = GitHubClient("https://api.github.com", "owner/repo", "token")
    with mock.patch(
        "gitacross.providers.base.urllib.request.urlopen", side_effect=_http_error(404)
    ):
        assert client.list_release_assets(99) == []
    print("  ✓ base: list_release_assets 404 → []")


def test_list_releases_real_request_path():
    """base list_releases issues a paginated GET and parses the response."""
    from gitacross.providers.gitea import GiteaClient

    # GiteaClient does not override list_releases → exercises the base implementation
    client = GiteaClient("https://gitea.example.com/api/v1", "owner/repo", "tok")
    resp = _FakeHTTPResponse(b'[{"tag_name": "v1"}]')
    with mock.patch(
        "gitacross.providers.base.urllib.request.urlopen", return_value=resp
    ):
        releases = client.list_releases(page=1)
    assert releases == [{"tag_name": "v1"}]
    print("  ✓ base: list_releases GETs the paginated endpoint")


def test_create_repo_failure_raises_runtime_error():
    """_create_repo surfaces non-conflict failures, falling back to the user endpoint."""
    from gitacross.providers.github import GitHubClient

    client = GitHubClient("https://api.github.com", "owner/repo", "token")

    def fake_urlopen(req):
        # /user check fails transiently → fall back to is_user=True
        if req.full_url.endswith("/user"):
            raise _http_error(500)
        raise _http_error(500)  # repo creation itself fails

    with mock.patch(
        "gitacross.providers.base.urllib.request.urlopen", side_effect=fake_urlopen
    ):
        try:
            _ = client._create_repo()
            assert False, "should have raised"
        except RuntimeError as e:
            assert "Could not create" in str(e)
            assert "HTTP 500" in str(e)
    print("  ✓ base: _create_repo failure surfaces as RuntimeError with HTTP code")




def test_git_redact_urls():
    from gitacross.git import _redact

    url = "https://uqkami:secret123@git.nodebay.top/uqkami/Repo.git"
    out = _redact(url)
    assert "secret123" not in out
    assert out == "https://uqkami:***@git.nodebay.top/uqkami/Repo.git"
    # No credentials — untouched
    plain = "https://github.com/uqkami/Repo.git"
    assert _redact(plain) == plain
    # Auth-failure stderr from git echoes the URL with credentials
    fatal = "fatal: Authentication failed for 'https://uqkami:secret123@github.com/uqkami/Repo.git/'"
    assert "secret123" not in _redact(fatal)
    print("  ✓ git: redacts credentials in URLs and git stderr")


def test_request_url_with_data():
    """_request_url with a body sets Content-Type and posts JSON."""
    from gitacross.providers.github import GitHubClient

    client = GitHubClient("https://api.github.com", "owner/repo", "token")
    resp = _FakeHTTPResponse(b'{"ok": true}')
    with mock.patch(
        "gitacross.providers.base.urllib.request.urlopen", return_value=resp
    ) as m:
        result = client._request_url("POST", "https://example.com/x", {"a": 1})
    assert result == {"ok": True}
    req = m.call_args[0][0]
    headers = {k.lower(): v for k, v in req.headers.items()}
    assert headers.get("content-type") == "application/json"
    print("  ✓ base: _request_url posts JSON with content type")

def test_release_and_asset_errors_re_raise_non_404():
    """get_release_by_tag/list_release_assets propagate non-404 errors."""
    from gitacross.providers.github import GitHubClient

    client = GitHubClient("https://api.github.com", "owner/repo", "token")
    for method, arg in (("get_release_by_tag", "v1"), ("list_release_assets", 1)):
        with mock.patch(
            "gitacross.providers.base.urllib.request.urlopen", side_effect=_http_error(500)
        ):
            try:
                getattr(client, method)(arg)
                assert False, "should raise"
            except urllib.error.HTTPError:
                pass
    print("  ✓ base: non-404 release/asset errors propagate")

def test_base_asset_methods_not_implemented():
    """BaseAPIClient download/upload raise NotImplementedError."""
    from gitacross.providers.base import BaseAPIClient

    client = BaseAPIClient("https://x", "owner/repo", "tok")
    try:
        client.download_asset({}, "f")
        assert False, "should raise"
    except NotImplementedError:
        pass
    try:
        client.upload_asset(1, "f")
        assert False, "should raise"
    except NotImplementedError:
        pass
    print("  ✓ base: unimplemented asset methods raise")

def test_github_download_asset_no_url_raises():
    """An asset without any download URL is rejected."""
    from gitacross.providers.github import GitHubClient

    gh = GitHubClient("https://api.github.com", "owner/repo", "token123")
    try:
        gh.download_asset({}, "f.zip")
        assert False, "should raise"
    except ValueError as e:
        assert "no download URL" in str(e)
    print("  ✓ github: asset without URL raises")

def test_github_upload_asset_int_id_and_enterprise_url():
    """upload_asset accepts an int id and derives the base URL for enterprise hosts."""
    from gitacross.providers.github import GitHubClient

    gh = GitHubClient("https://ghe.example.com/api/v3", "owner/repo", "tok")
    resp = _FakeHTTPResponse(b'{"id": 7}')
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        _ = tmp.write(b"data")
        tmp_path = Path(tmp.name)
    try:
        with mock.patch("urllib.request.urlopen", return_value=resp) as m_open:
            res = gh.upload_asset(42, tmp_path)  # int id → non-dict branch
            assert res == {"id": 7}
            url = m_open.call_args[0][0].full_url
            assert url.startswith(
                "https://ghe.example.com/api/v3/repos/owner/repo/releases/42/assets"
            )
    finally:
        tmp_path.unlink(missing_ok=True)
    print("  ✓ github: upload with int id and enterprise base URL")

def test_github_upload_asset_errors():
    """upload_asset returns None on 422 (idempotent) and re-raises other errors."""
    from gitacross.providers.github import GitHubClient

    gh = GitHubClient("https://api.github.com", "owner/repo", "tok")
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        _ = tmp.write(b"data")
        tmp_path = Path(tmp.name)
    try:
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(422)):
            assert gh.upload_asset(42, tmp_path) is None  # idempotent
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(500)):
            try:
                gh.upload_asset(42, tmp_path)
                assert False, "should raise"
            except urllib.error.HTTPError:
                pass
    finally:
        tmp_path.unlink(missing_ok=True)
    print("  ✓ github: upload 422 idempotent, 500 propagates")

def test_gitea_download_asset_fallbacks():
    """download_asset: constructed URL from IDs, relative URL fix, no-URL ValueError."""
    from gitacross.providers.gitea import GiteaClient

    gt = GiteaClient("https://gitea.example.com/api/v1", "owner/repo", "tok")

    def _make_resp():
        r = mock.MagicMock()
        r.read.side_effect = [b"data", b""]  # copyfileobj reads until b""
        r.__enter__.return_value = r
        return r

    urlopen = mock.patch(
        "urllib.request.urlopen", side_effect=lambda _req: _make_resp()
    )

    # Constructed from ids (no browser_download_url)
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = Path(tmp.name)
    with urlopen as m:
        gt.download_asset({"id": 5, "release_id": 9}, tmp_path)
        assert m.call_args[0][0].full_url == (
            "https://gitea.example.com/api/v1/repos/owner/repo/releases/9/assets/5"
        )
    tmp_path.unlink(missing_ok=True)

    # Relative URL → host fixed up
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = Path(tmp.name)
    with urlopen as m2:
        gt.download_asset({"browser_download_url": "/att/1"}, tmp_path)
        assert m2.call_args[0][0].full_url == "https://gitea.example.com/att/1"
    tmp_path.unlink(missing_ok=True)

    # No URL at all → ValueError
    try:
        gt.download_asset({"name": "x"}, "f")
        assert False, "should raise"
    except ValueError as e:
        assert "no download URL" in str(e)
    print("  ✓ gitea: download_asset URL fallbacks and errors")

def test_gitea_upload_asset_streaming_and_error():
    """upload_asset streams with _MultipartReader; non-conflict errors propagate."""
    from gitacross.providers.gitea import GiteaClient

    gt = GiteaClient("https://gitea.example.com/api/v1", "owner/repo", "tok")
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        _ = tmp.write(b"stream payload")
        tmp_path = Path(tmp.name)
    try:
        resp = _FakeHTTPResponse(b'{"id": 3}')
        with mock.patch("urllib.request.urlopen", return_value=resp):
            res = gt.upload_asset(3, tmp_path, stream=True)
            assert res == {"id": 3}

        with mock.patch("urllib.request.urlopen", side_effect=_http_error(500)):
            try:
                gt.upload_asset(3, tmp_path)
                assert False, "should raise"
            except urllib.error.HTTPError:
                pass
    finally:
        tmp_path.unlink(missing_ok=True)
    print("  ✓ gitea: streaming upload and error propagation")

def test_gitea_multipart_reader_read_zero():
    """_MultipartReader.read(0) returns empty without consuming the stream."""
    from gitacross.providers.gitea import _MultipartReader

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        _ = tmp.write(b"x")
        tmp_path = Path(tmp.name)
    try:
        with _MultipartReader(b"h", tmp_path, b"f") as reader:
            assert reader.read(0) == b""
            assert reader.read() == b"hxf"
    finally:
        tmp_path.unlink(missing_ok=True)
    print("  ✓ gitea: _MultipartReader.read(0) returns empty")

def test_gitea_multipart_reader_close_swallows_errors():
    """_MultipartReader.close must never raise, even if a part fails to close."""
    from gitacross.providers.gitea import _MultipartReader

    class BadPart:
        def close(self):
            raise OSError("boom")

    reader = _MultipartReader.__new__(_MultipartReader)
    object.__setattr__(reader, "_parts", [BadPart()])
    object.__setattr__(reader, "_idx", 0)
    reader.close()  # must not raise
    print("  ✓ gitea: _MultipartReader.close swallows part errors")


def test_github_list_releases_override():
    """GitHub's list_releases uses per_page (overrides the base)."""
    from gitacross.providers.github import GitHubClient

    gh = GitHubClient("https://api.github.com", "owner/repo", "tok")
    resp = _FakeHTTPResponse(b"[]")
    with mock.patch(
        "gitacross.providers.base.urllib.request.urlopen", return_value=resp
    ) as m:
        assert gh.list_releases(page=2) == []
    assert "per_page" in m.call_args[0][0].full_url
    print("  ✓ github: list_releases uses per_page")
