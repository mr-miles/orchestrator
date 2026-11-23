"""Universal report guards: size limits, shape, required documents and git result (G-R*)."""
from __future__ import annotations

import json
from typing import Any

from ...store import DocStore
from ..assets import GitMode
from ..attest import FILE_RE, Attestation, normalize
from ..model import ReportStatus

MAX_TEXT = 2000
MAX_ATTESTATIONS = 100
MAX_DATA_BYTES = 256 * 1024


def report_limits(summary: Any, attestations: Any, data: Any, blocked_reason: Any,
                  missing_decision: Any) -> list[str]:
    """Guard G-R8: bound everything a report can make the controller store or process."""
    v = []
    for name, val in (("summary", summary), ("blocked_reason", blocked_reason),
                      ("missing_decision", missing_decision)):
        if val is not None and (not isinstance(val, str) or len(val) > MAX_TEXT):
            v.append(f"G-R8: {name} must be a string of at most {MAX_TEXT} characters")
    if not isinstance(attestations, list) or len(attestations) > MAX_ATTESTATIONS \
            or not all(isinstance(a, str) and len(a) <= 600 for a in attestations):
        v.append(f"G-R8: attestations must be a list of at most {MAX_ATTESTATIONS} strings")
    try:
        size = len(json.dumps(data))
    except (TypeError, ValueError):
        return v + ["G-R8: data must be JSON"]
    if size > MAX_DATA_BYTES:
        v.append(f"G-R8: data is {size} bytes; at most {MAX_DATA_BYTES}")
    return v


def report_shape(status: str, blocked_reason: str | None, missing_decision: str | None) -> list[str]:
    v = []
    if status not in tuple(ReportStatus):
        v.append(f"G-R1: status must be DONE or BLOCKED (got {status!r}); deferral is not allowed")
    explained = (blocked_reason or "").strip() and (missing_decision or "").strip()
    if status == ReportStatus.BLOCKED and not explained:
        v.append("G-R2: BLOCKED requires blocked_reason and missing_decision")
    return v


def required_docs(run_id: str, required: list[str], atts: list[Attestation], docs: DocStore,
                  confirmed: list[str] = ()) -> tuple[dict[str, Attestation], list[str]]:
    """G-R4: each required document attested at its latest version, which this run wrote – or
    confirmed, by writing it again unchanged (``confirmed``: those attestations)."""
    found: dict[str, Attestation] = {}
    v = []
    for path in required:
        cands = [a for a in atts if a.kind == "file" and a.path == path]
        if not cands:
            v.append(f"G-R4: required document {path} not attested")
            continue
        att = max(cands, key=lambda a: a.version)
        latest = docs.latest_version(path)
        if att.version != latest:
            v.append(f"G-R4: {att} is not the latest version (latest {latest})")
            continue
        if docs.read(path, att.version).author != run_id and str(att) not in confirmed:
            v.append(f"G-R4: {att} was not written by this run")
            continue
        found[path] = att
    return found, v


def required_outputs(run_id: str, required: list[str], outputs: dict, scope: list[str],
                     docs: DocStore, cid: str, confirmed: list[str] = ()
                     ) -> tuple[dict[str, str | list[str]], list[str]]:
    """G-R4: the report names each output its role requires (``name*``: a list, possibly empty)
    as documents (``file:<path>@<version>``) inside the run's write scope, each at its latest
    version, which this run wrote – or confirmed, by writing it again unchanged. Returns the
    outputs as they are pinned (fully qualified) and the violations."""
    pinned: dict[str, str | list[str]] = {}
    v: list[str] = []
    wanted = {name.rstrip("*"): name.endswith("*") for name in required}
    if extra := sorted(set(outputs) - set(wanted)):
        v.append(f"G-R4: outputs {extra} are not this role's (it reports {sorted(wanted)})")
    for name, many in wanted.items():
        value = outputs.get(name, [] if many else None)
        refs = value if isinstance(value, list) else [value] if value else []
        if not many and len(refs) != 1:
            v.append(f"G-R4: output {name!r} must name one document (file:<path>@<version>)")
            continue
        good = []
        for ref in refs:
            ref = normalize(str(ref), cid)
            m = FILE_RE.match(ref)
            if not m:
                v.append(f"G-R4: output {name!r}: {ref} is not a document (file:<path>@<version>)")
                continue
            path, version = m["path"], int(m["version"])
            if not any(path.startswith(w) for w in scope):
                v.append(f"G-R4: output {name!r}: {path} is outside your write scope")
            elif version != (latest := docs.latest_version(path)):
                v.append(f"G-R4: output {name!r}: {ref} is not the latest version (latest {latest})")
            elif docs.read(path, version).author != run_id and ref not in confirmed:
                v.append(f"G-R4: output {name!r}: {ref} was not written by this run")
            else:
                good.append(ref)
        if len(good) == len(refs):
            pinned[name] = good if many else good[0]
    return pinned, v


def required_git(mode: GitMode, atts: list[Attestation], repo: str,
                 judged_sha: str | None) -> tuple[str | None, list[str]]:
    if mode == GitMode.NONE:
        return None, []
    commits = [a.sha for a in atts if a.kind == "git" and a.path is None and a.repo == repo]
    if mode == GitMode.COMMIT:
        if len(set(commits)) != 1:
            return None, [f"G-R5: attest exactly one commit as git:{repo}@<sha> (got {len(set(commits))})"]
        return commits[0], []
    # candidate / head: must attest exactly the sha under judgement
    if judged_sha not in commits:
        gid = "G-RV1" if mode == GitMode.CANDIDATE else "G-R5"
        return None, [f"{gid}: must attest git:{repo}@{judged_sha} (the {mode} under judgement)"]
    return judged_sha, []
