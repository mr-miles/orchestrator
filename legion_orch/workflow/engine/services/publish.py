"""Publishing a change signed off at H-3: its result pushed to the workspace's source on GitHub
as ``<publish_prefix><change-id>``, and a pull request from it – ready for review – with the
approved description (the ``pr`` pointer), the change's id in its body, and its labels.
Done again, it changes only what differs: the branch is pushed if it moved (never forced), the
pull request found and brought up to date, and each label family's labels made exactly the
wanted ones (other labels, a human's included, are left alone). Recorded in
``control/pr.json``."""
from __future__ import annotations

from dataclasses import dataclass

from ....git import GitHubError
from ... import guards
from ...attest import FILE_RE
from ..base.core import ChangeCore


@dataclass(frozen=True)
class LabelSpec:
    """One family of labels (``prefix``) and the ones of it the pull request should have."""
    prefix: str
    names: tuple[str, ...]
    color: str                       # hex, without '#': for a label created on the repository
    description: str


@dataclass(frozen=True)
class Published:
    url: str
    number: int
    branch: str
    head: str
    labels: tuple[str, ...]


class Publisher:
    """A change's pull request on GitHub, through the shared :class:`.GitHub`."""

    def __init__(self, core: ChangeCore):
        self.c = core

    def branch(self) -> str:
        """The published branch: the workspace's prefix and the change's id."""
        ch = self.c.change
        return self.c.cfg.workspace(ch.workspace).publish_prefix + ch.id

    def body(self, text: str) -> str:
        """The description's body, then a footer the controller adds (always there, always
        right): the change's id and its tier."""
        ch = self.c.change
        return (guards.pr_body(text).rstrip() + "\n\n---\n"
                f"Legion change: `{ch.id}` · risk tier {ch.tier or 'unassessed'}\n")

    def text(self) -> str:
        """The pull request's text: the approved description (the change's ``pr`` pointer)."""
        ref = self.c.tickets.root.pointers.get("pr")
        m = FILE_RE.match(ref) if isinstance(ref, str) else None
        if m is None:
            raise GitHubError("there is no approved pull request description (`pr`)")
        return self.c.docs.read(m["path"], int(m["version"])).content

    async def publish(self, labels: list[LabelSpec], head: str) -> Published:
        """Push ``head``, open or update the pull request, and set its labels. Raises
        :class:`.GitHubError` with what went wrong."""
        ch, gh, hub = self.c.change, self.c.svc.github, self.c.git
        repo = gh.repo_of(hub.source)
        if repo is None:
            raise GitHubError(f"workspace {ch.workspace}'s source is not on GitHub")
        branch = self.branch()
        text = self.text()
        title, body = guards.pr_title(text), self.body(text)
        await gh.push(hub.repo, hub.source, head, branch)
        pr = await gh.find_pr(repo, branch)
        if pr is None:
            pr = await gh.create_pr(repo, hub.base_branch, branch, title, body)
        elif pr.state == "OPEN":
            await gh.update_pr(repo, pr.number, title, body)
        existing = await gh.labels(repo)
        for spec in labels:
            for name in spec.names:
                if name not in existing:
                    await gh.create_label(repo, name, spec.color, spec.description)
        current = set(pr.labels)
        add = [n for spec in labels for n in spec.names if n not in current]
        remove = sorted(lb for spec in labels for lb in current
                        if lb.startswith(spec.prefix) and lb not in spec.names)
        await gh.set_labels(repo, pr.number, add, remove)
        wanted = tuple(sorted((current - set(remove)) | set(add)))
        return Published(pr.url, pr.number, branch, head, wanted)

    def record(self, done: Published) -> None:
        self.c.write_record("control/pr.json", {
            "url": done.url, "number": done.number, "branch": done.branch, "head": done.head,
            "labels": list(done.labels), "workspace": self.c.change.workspace})
