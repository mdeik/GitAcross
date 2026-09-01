"""Backwards-compatibility shim — re-exports GitHubClient from gitacross.providers.

New code should import from ``gitacross.providers.github`` or use
``gitacross.providers.get_api_client`` instead.
"""

from .providers.github import GitHubClient  # noqa: F401

__all__ = ["GitHubClient"]
