"""GitHub integration service package."""

from .service import (
    GitHubService,
    GitHubError,
    Repo,
    PROVIDER,
    RECOMMENDED_SCOPES,
    get_token,
    save_token,
    delete_token,
)

__all__ = [
    "GitHubService",
    "GitHubError",
    "Repo",
    "PROVIDER",
    "RECOMMENDED_SCOPES",
    "get_token",
    "save_token",
    "delete_token",
]
