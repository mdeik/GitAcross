"""Backwards-compatibility shim — re-exports GiteaClient from gitacross.providers.

New code should import from ``gitacross.providers.gitea`` or use
``gitacross.providers.get_api_client`` instead.
"""

from .providers.gitea import GiteaClient  # noqa: F401

__all__ = ["GiteaClient"]
