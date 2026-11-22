"""Outbound notifications for attention items (WORKFLOW.md §2.2).

The controller calls :meth:`Notifier.raised` / :meth:`Notifier.resolved` when a change's save
raises or resolves an item, and :meth:`Notifier.remind` from its reminders timer. Each delivery is
a background task with a few retries; it never raises into the engine and never blocks it. A
delivery that fails for good is only recorded (the timeline and the log): the UI is always the
authority, and a still-waiting blocking item is sent again by the reminder.

Nothing is persisted: after a restart the items already waiting count as sent at start-up, so
nobody is paged again for them until ``remind_after_s`` has passed.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Callable, Coroutine
from typing import Any
from urllib.parse import quote

import httpx

from .config import NotifyConfig, NotifySink
from .workflow.attention import AttentionItem

log = logging.getLogger("legion.notify")

RAISED, REMINDER, RESOLVED = "raised", "reminder", "resolved"
# Result callback: (item, event, sink label, error or None)
OnResult = Callable[[AttentionItem, str, str, str | None], None]


class Notifier:
    RETRY_DELAYS_S: tuple[float, ...] = (1.0, 5.0)      # between the attempts (3 in all)

    def __init__(self, cfg: NotifyConfig, project: str, approvers: list[str],
                 spawn: Callable[[Coroutine], None], on_result: OnResult):
        self.cfg, self.project, self.approvers = cfg, project, list(approvers)
        self._spawn, self._on_result = spawn, on_result
        self._sent: dict[str, float] = {}       # blocking item key -> when it was last sent

    # ------------------------------------------------------------------ item events
    def seed(self, items: list[AttentionItem], now: float | None = None) -> None:
        """Items already waiting when the controller starts: known, not sent again now."""
        now = time.monotonic() if now is None else now
        for i in items:
            if i.blocking:
                self._sent.setdefault(i.key, now)

    def raised(self, item: AttentionItem) -> None:
        if item.blocking:
            self._sent[item.key] = time.monotonic()
        self._send(item, RAISED)

    def resolved(self, item: AttentionItem) -> None:
        self._sent.pop(item.key, None)
        if item.blocking and self.cfg.resolved:
            self._send(item, RESOLVED)

    def remind(self, items: list[AttentionItem], now: float | None = None) -> None:
        """Send again every blocking item that has waited ``remind_after_s`` since it was last
        sent (called from the controller's reminders timer with every change's current items)."""
        if self.cfg.remind_after_s <= 0:
            return
        now = time.monotonic() if now is None else now
        for i in items:
            if not i.blocking:
                continue
            last = self._sent.setdefault(i.key, now)
            if now - last >= self.cfg.remind_after_s:
                self._sent[i.key] = now
                self._send(i, REMINDER)

    # ------------------------------------------------------------------ delivery
    def link(self, item: AttentionItem) -> str | None:
        """The UI link that opens the item (``#<change>/overview[/<node>]``)."""
        if not self.cfg.ui_url:
            return None
        frag = f"{quote(item.change, safe='')}/overview"
        if item.node:
            frag += f"/{quote(item.node, safe='')}"
        return f"{self.cfg.ui_url.rstrip('/')}/#{frag}"

    def payload(self, item: AttentionItem, event: str) -> dict[str, Any]:
        return {"event": event, "project": self.project, "url": self.link(item),
                "approvers": self.approvers} | item.model_dump(mode="json")

    def sinks_for(self, item: AttentionItem) -> list[NotifySink]:
        return [s for s in self.cfg.sinks
                if item.kind in (s.items if s.items is not None else self.cfg.items)]

    def _send(self, item: AttentionItem, event: str) -> None:
        sinks = self.sinks_for(item)
        if not sinks:
            return
        body = self.payload(item, event)
        for s in sinks:
            self._spawn(self._deliver(s, item, event, body))

    async def _deliver(self, sink: NotifySink, item: AttentionItem, event: str,
                       body: dict[str, Any]) -> None:
        err: str | None = None
        for attempt in range(len(self.RETRY_DELAYS_S) + 1):
            if attempt:
                await asyncio.sleep(self.RETRY_DELAYS_S[attempt - 1])
            try:
                await SENDERS[sink.kind](self, sink, body)
                err = None
                break
            except Exception as e:   # every failure is retried, then recorded
                err = f"{type(e).__name__}: {e}"[:300]
                log.warning("notification via %s failed (attempt %d): %s", sink.label, attempt + 1, err)
        self._on_result(item, event, sink.label, err)

    async def _webhook(self, sink: NotifySink, body: dict[str, Any]) -> None:
        data = json.dumps(body, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "legion-orchestrator"}
        if sink.secret_env:
            secret = os.environ.get(sink.secret_env)
            if not secret:
                raise RuntimeError(f"environment variable {sink.secret_env} is not set")
            headers["X-Legion-Signature"] = "sha256=" + hmac.new(
                secret.encode(), data, hashlib.sha256).hexdigest()
        async with httpx.AsyncClient(timeout=self.cfg.timeout_s) as client:
            r = await client.post(sink.url, content=data, headers=headers)
            r.raise_for_status()

    async def _ntfy(self, sink: NotifySink, body: dict[str, Any]) -> None:
        """ntfy's JSON publishing (POST to the server root, topic in the body), so titles and
        goals need not be ASCII as header values would."""
        root, topic = sink.url.rstrip("/").rsplit("/", 1)
        prefix = {RAISED: "", REMINDER: "Still waiting: ", RESOLVED: "Resolved: "}[body["event"]]
        msg = {"topic": topic, "title": f"{prefix}{body['title']}",
               "message": "\n".join(x for x in (f"{body['change']}: {body['goal']}", body["reason"],
                                                  f"Resolver proposes {body['proposal']}"
                                                  if body.get("proposal") else "")
                                    if x),
               "priority": 4 if body["blocking"] and body["event"] != RESOLVED else 3,
               "tags": ["warning"] if body["blocking"] else ["inbox_tray"]}
        if body.get("url"):
            msg["click"] = body["url"]
        async with httpx.AsyncClient(timeout=self.cfg.timeout_s) as client:
            r = await client.post(root, json=msg, headers={"User-Agent": "legion-orchestrator"})
            r.raise_for_status()

    async def _command(self, sink: NotifySink, body: dict[str, Any]) -> None:
        proc = await asyncio.create_subprocess_exec(
            *sink.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE)
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(json.dumps(body).encode()),
                                               self.cfg.timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"timed out after {self.cfg.timeout_s}s") from None
        if proc.returncode:
            raise RuntimeError(f"exit {proc.returncode}: {stderr.decode(errors='replace')[-200:]}")


SENDERS = {"webhook": Notifier._webhook, "ntfy": Notifier._ntfy, "command": Notifier._command}
