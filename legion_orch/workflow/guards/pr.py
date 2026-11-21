"""The change's pull request description (G-PR1, G-PR2): what the describer writes and what the
PR reviewer must attest. The description is ``delivery/pr.md``: a title line, then the body."""
from __future__ import annotations

PR_DOC = "delivery/pr.md"
PR_SECTIONS = ("## Motivation", "## Changes", "## Verification")
MAX_TITLE = 120


def pr_title(text: str) -> str:
    """The PR's title: the description's first non-empty line, without its ``# ``."""
    first = next((line for line in text.splitlines() if line.strip()), "")
    return first[2:].strip() if first.startswith("# ") else ""


def pr_body(text: str) -> str:
    """The PR's body: the description after its title line."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip():
            return "\n".join(lines[i + 1:]).strip() + "\n"
    return ""


def pr_description(text: str, requirement_ids: list[str]) -> list[str]:
    """G-PR1: a title line (``# <title>``, at most 120 characters), the sections Motivation,
    Changes and Verification, and every requirement of the change cited by its id."""
    v = []
    title = pr_title(text)
    if not title:
        v.append("G-PR1: the first line is the PR's title: '# <title>'")
    elif len(title) > MAX_TITLE:
        v.append(f"G-PR1: the title is at most {MAX_TITLE} characters (got {len(title)})")
    lines = {line.strip() for line in text.splitlines()}
    v += [f"G-PR1: section '{s}' is missing" for s in PR_SECTIONS if s not in lines]
    if missing := [r for r in requirement_ids if r not in text]:
        v.append(f"G-PR1: requirement(s) {missing} are not cited")
    return v


def pr_review(attested: dict[str, int], path: str, latest: int | None) -> list[str]:
    """G-PR2: the review attests the latest version of the description it judged."""
    if latest is None:
        return ["G-PR2: there is no PR description to review"]
    if attested.get(path) != latest:
        return [f"G-PR2: attest the latest description, file:{path}@{latest}"]
    return []
