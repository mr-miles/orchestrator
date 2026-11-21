"""Publishing an accepted change: after H-3 its branch is pushed to the workspace's source on
GitHub as ``<publish_prefix><change-id>``, and a pull request opened from it with the reviewed
description, the change's id and its labels – brought up to date when it runs again."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from engine_harness import Flow, scenario

from legion_orch.git import GitHub, GitHubError, PullRequest
from legion_orch.workflow.engine.services.publish import LabelSpec
from legion_orch.workflow.model import ACCEPTED, ESCALATED, ChangeStatus


class FakeGitHub(GitHub):
    """GitHub as a few dicts: what was pushed, the pull requests and the repository's labels.
    Every workspace source counts as ``owner/app``."""

    def __init__(self):
        super().__init__()
        self.pushed: dict[str, str] = {}
        self.prs: dict[str, PullRequest] = {}
        self.bodies: dict[int, tuple[str, str]] = {}
        self.repo_labels: set[str] = {"bug"}
        self.created_labels: list[str] = []
        self.fail: str | None = None             # the next push fails with this

    def repo_of(self, source):
        return "owner/app" if source else None

    async def push(self, hub, source, sha, branch):
        if self.fail:
            msg, self.fail = self.fail, None
            raise GitHubError(msg)
        self.pushed[branch] = sha

    async def find_pr(self, repo, branch):
        return self.prs.get(branch)

    async def create_pr(self, repo, base, branch, title, body):
        n = len(self.prs) + 1
        self.prs[branch] = PullRequest(n, f"https://github.com/{repo}/pull/{n}", "OPEN", [])
        self.bodies[n] = (title, body)
        return self.prs[branch]

    async def update_pr(self, repo, number, title, body):
        self.bodies[number] = (title, body)

    async def labels(self, repo):
        return set(self.repo_labels)

    async def create_label(self, repo, name, color, description):
        self.repo_labels.add(name)
        self.created_labels.append(name)

    async def set_labels(self, repo, number, add, remove):
        pr = next(p for p in self.prs.values() if p.number == number)
        pr.labels = sorted((set(pr.labels) - set(remove)) | set(add))


async def to_h3(f: Flow, gh: FakeGitHub | None = None) -> FakeGitHub:
    """An R1 change whose workspace is on GitHub, built, described and reviewed: H-3 (automatic
    at R1) accepts it, and the publish step runs."""
    gh = f.ctl.svc.github = gh or FakeGitHub()
    await f.intake()
    await f.human("H-1", "accept")
    await f.plan()
    await f.critique("PASS")
    await f.human("H-E1", "approve")
    await f.implement()
    await f.review(verdict="PASS")                  # then described and reviewed
    return gh


@scenario()
async def test_an_accepted_change_is_published_as_a_labelled_pull_request(f: Flow):
    gh = await to_h3(f)
    cid, ch = f.ce.cid, f.ce.change
    assert list(f.ce.nodes)[-2:] == ["H-3", "publish"] and f.status("publish") == ACCEPTED
    branch = f"agent/{cid}"
    assert gh.pushed == {branch: ch.integration_head}                # legion/* stays internal
    pr = gh.prs[branch]
    title, body = gh.bodies[pr.number]
    assert title == "Greet the user" and body.startswith("## Motivation")
    assert f"Legion change: `{cid}` · risk tier R1" in body           # the controller's footer
    assert pr.labels == ["risk:R1"] and gh.created_labels == ["risk:R1"]
    rec = f.ce.json_doc("control/pr.json")
    assert rec == {"url": pr.url, "number": 1, "branch": branch, "head": ch.integration_head,
                   "labels": ["risk:R1"], "workspace": ch.workspace}
    assert ch.status == ChangeStatus.READY                           # ready once published
    from legion_orch.server.views import change_state
    assert change_state(f.ctl, f.ce)["pull_request"]["url"] == pr.url


@scenario()
async def test_publishing_again_updates_the_pull_request_and_only_its_own_labels(f: Flow):
    gh = await to_h3(f)
    pr = gh.prs[f"agent/{f.ce.cid}"]
    pr.labels = ["needs-docs", "risk:R0"]            # a human's label; a stale tier
    gh.bodies[pr.number] = ("old", "old")
    done = await f.ce._phases.publisher.publish([LabelSpec("risk:", ("risk:R1",), "d93f0b", "tier")])
    assert len(gh.prs) == 1 and done.number == pr.number             # the same pull request
    assert pr.labels == ["needs-docs", "risk:R1"] and done.labels == ("needs-docs", "risk:R1")
    assert gh.bodies[pr.number][0] == "Greet the user"               # brought up to date
    assert gh.created_labels == ["risk:R1"]                          # created once


@scenario()
async def test_a_failed_push_escalates_and_a_retry_publishes(f: Flow):
    gh = FakeGitHub()
    gh.fail = "pushing agent/x failed: ! [rejected] (non-fast-forward)"
    await to_h3(f, gh)
    node = f.ce.nodes["publish"]
    assert node.status == ESCALATED and node.escalation.kind == "publish_failed"
    assert "non-fast-forward" in node.escalation.reason
    assert f.ce.change.status == ChangeStatus.ACTIVE and not gh.prs
    await f.human("publish", "retry")
    assert f.status("publish") == ACCEPTED and len(gh.prs) == 1
    assert f.ce.change.status == ChangeStatus.READY


@scenario()
async def test_a_workspace_not_on_github_ends_at_h3(f: Flow):
    await f.intake()
    await f.human("H-1", "accept")
    assert "publish" not in f.ce.nodes                               # a local source
    assert f.ce._flow.tree["publish"] is False


# ---------------------------------------------------------------------------- the real client
def test_github_sources_name_their_repository():
    gh = GitHub()
    for src in ("https://github.com/mr-miles/orchestrator",
                "https://github.com/mr-miles/orchestrator.git",
                "git@github.com:mr-miles/orchestrator.git",
                "ssh://git@github.com/mr-miles/orchestrator"):
        assert gh.repo_of(src) == "mr-miles/orchestrator", src
    for src in (None, "", "/srv/repos/app", "https://gitlab.com/a/b", "https://github.com/a"):
        assert gh.repo_of(src) is None, src


def test_a_push_is_never_forced(tmp_path):
    def git(cwd, *a):
        return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                              text=True).stdout.strip()

    async def go():
        work, remote = tmp_path / "work", tmp_path / "remote.git"
        git(tmp_path, "init", "-q", "--bare", str(remote))
        git(tmp_path, "init", "-q", "-b", "main", str(work))
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            git(work, "config", k, v)
        (work / "a").write_text("1")
        git(work, "add", "a")
        git(work, "commit", "-qm", "one")
        one = git(work, "rev-parse", "HEAD")
        gh = GitHub()
        await gh.push(Path(work), str(remote), one, "agent/c1")
        assert git(remote, "rev-parse", "refs/heads/agent/c1") == one
        git(work, "commit", "-q", "--amend", "-m", "rewritten")        # history rewritten
        other = git(work, "rev-parse", "HEAD")
        with pytest.raises(GitHubError, match="pushing agent/c1"):
            await gh.push(Path(work), str(remote), other, "agent/c1")
        assert git(remote, "rev-parse", "refs/heads/agent/c1") == one   # not forced
    asyncio.run(go())


def test_gh_calls_are_built_as_gh_expects(tmp_path):
    calls: list[list[str]] = []

    async def fake_run(argv, cwd):
        calls.append(argv)
        if argv[1:3] == ["pr", "list"]:
            return 0, '[{"number": 7, "url": "u", "state": "OPEN", "labels": [{"name": "x"}]}]'
        return 0, "[]"

    async def go():
        gh = GitHub(fake_run)
        pr = await gh.create_pr("o/r", "main", "agent/c1", "Title", "Body")
        assert (pr.number, pr.labels) == (7, ["x"])
        create = calls[0]
        assert create[:3] == ["gh", "pr", "create"] and "--body-file" in create
        assert create[create.index("--head") + 1] == "agent/c1"
        await gh.set_labels("o/r", 7, ["risk:R2"], ["risk:R1"])
        assert calls[-1] == ["gh", "pr", "edit", "7", "--repo", "o/r", "--add-label", "risk:R2",
                             "--remove-label", "risk:R1"]
        await gh.set_labels("o/r", 7, [], [])                          # nothing to do: no call
        assert len(calls) == 3

    async def failing(argv, cwd):
        return 1, "HTTP 401: Bad credentials"

    async def go_fail():
        with pytest.raises(GitHubError, match="Bad credentials"):
            await GitHub(failing).labels("o/r")
    asyncio.run(go())
    asyncio.run(go_fail())
