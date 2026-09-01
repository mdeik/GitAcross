"""Provider registry — maps platform type strings to API client classes.

To add a new provider:
  1. Create ``sync/providers/<name>.py`` implementing the same interface as
     ``BaseAPIClient`` (``download_asset``, ``upload_asset`` at minimum).
  2. Import the class here and register it in ``_REGISTRY``.

That's it — ``source.py`` and ``target.py`` use ``get_api_client`` and never
need to know which concrete class was returned.
"""
from __future__ import annotations


from .gitea import GiteaClient
from .github import GitHubClient

_REGISTRY: dict = {
    "gitea": GiteaClient,
    "github": GitHubClient,
}


def get_api_client(provider_type: str, api: str, repo: str, token: str):
    """Instantiate an API client for *provider_type*.

    Raises ``ValueError`` for unknown types with a clear message listing the
    known options, so users get actionable feedback when they mis-spell a type.
    """
    cls = _REGISTRY.get(provider_type)
    if cls is None:
        known = ", ".join(sorted(_REGISTRY))
        raise ValueError(
            f"Unknown provider type '{provider_type}' — known types: {known}"
        )
    return cls(api, repo, token)


def register_provider(name: str, cls) -> None:
    """Register a custom provider class at runtime.

    Useful for plugins or testing — call before ``get_api_client`` is invoked.
    """
    _REGISTRY[name] = cls


__all__ = ["get_api_client", "register_provider", "GiteaClient", "GitHubClient"]
