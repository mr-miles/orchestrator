"""The controller's git hub: bare repository, integration worktrees, smart-HTTP endpoint.

Self-contained: authentication is supplied by the host as ``authorize(request) -> GitGrant``.
"""
from .github import GitHub, GitHubError, PullRequest
from .http import GitEndpoint, GitGrant, make_git_endpoint
from .push_scope import PushScope, Rule
from .remote import (
    GitError,
    GitRemote,
    ShaError,
    WorkspaceUnsynced,
    is_blank_tree,
    path_matches,
    paths_overlap,
)

__all__ = ["GitEndpoint", "GitError", "GitGrant", "GitRemote", "is_blank_tree", "make_git_endpoint",
           "path_matches", "paths_overlap", "ShaError", "WorkspaceUnsynced", "GitHub", "GitHubError",
           "PullRequest", "PushScope", "Rule"]
