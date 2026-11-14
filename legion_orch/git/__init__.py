"""The controller's git hub: bare repository, integration worktrees, smart-HTTP endpoint.

Self-contained: authentication is supplied by the host as ``authorize(request) -> GitGrant``.
"""
from .http import GitGrant, make_git_endpoint
from .remote import GitError, GitRemote, path_matches, paths_overlap

__all__ = ["GitGrant", "make_git_endpoint", "GitError", "GitRemote", "path_matches", "paths_overlap"]
