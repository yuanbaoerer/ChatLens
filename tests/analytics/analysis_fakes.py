"""Synthetic analyzers and snapshots shared by analysis lifecycle tests."""
from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Callable

from analytics.analyzer import (
    AnalysisContext,
    AnalysisResult,
    Analyzer,
    Cancellation,
    ProgressReporter,
)
from analytics.models import ForwardChild, Message, Snapshot
from analytics.snapshot import SnapshotBuilder

STAMP = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
GROUP = "synthetic@chatroom"


def make_message(
    message_id: str = "message-1",
    *,
    text: str = "Synthetic text",
    kind: str = "text",
    sent_at: datetime | None = None,
    forward_children: tuple[ForwardChild, ...] = (),
) -> Message:
    return Message(
        source_id="synthetic-source",
        account_scope="synthetic-account",
        group_id=GROUP,
        message_id=message_id,
        sender_pseudonym="pseudonym-1",
        sent_at=sent_at or STAMP,
        kind=kind,
        text=text,
        forward_children=forward_children,
    )


def make_snapshot(*, messages: list[Message] | None = None, with_child: bool = True) -> Snapshot:
    items = list(messages) if messages is not None else [make_message()]
    if with_child:
        child = ForwardChild(
            child_id="message-1/f0",
            sender_pseudonym="pseudonym-2",
            sent_at=STAMP + timedelta(seconds=1),
            kind="text",
            text="Synthetic child text",
        )
        items = [replace(items[0], forward_children=(child,))] + items[1:]
    builder = SnapshotBuilder(
        source_id="synthetic-source",
        account_scope="synthetic-account",
        group_id=GROUP,
        timezone_name="Asia/Shanghai",
    )
    builder.add_messages(items)
    return builder.build()


class SyntheticAnalyzer(Analyzer):
    """Configurable analyzer driven by events, never by wall-clock sleeps."""

    def __init__(
        self,
        analyzer_id: str,
        *,
        version: str = "1",
        prerequisites: tuple[str, ...] = (),
        supported_message_types: frozenset[str] = frozenset({"text"}),
        estimated_workload: int = 1,
        result: AnalysisResult | None = None,
        failure: BaseException | None = None,
        calls: list[str] | None = None,
        started: threading.Event | None = None,
        release: threading.Event | None = None,
        returned: threading.Event | None = None,
        progress_values: tuple[float, ...] = (),
        progress_done: threading.Event | None = None,
        ignore_cancel: bool = False,
        runner: Callable[[AnalysisContext, Snapshot], AnalysisResult] | None = None,
    ) -> None:
        self.analyzer_id = analyzer_id
        self.version = version
        self.prerequisites = prerequisites
        self.supported_message_types = supported_message_types
        self.estimated_workload = estimated_workload
        self.result = result
        self.failure = failure
        self.calls = calls
        self.started = started
        self.release = release
        self.returned = returned
        self.progress_values = progress_values
        self.progress_done = progress_done
        self.ignore_cancel = ignore_cancel
        self.runner = runner

    def run(
        self,
        snapshot: Snapshot,
        context: AnalysisContext,
        cancellation: Cancellation,
        progress: ProgressReporter,
    ) -> AnalysisResult:
        if self.calls is not None:
            self.calls.append(self.analyzer_id)
        if self.started is not None:
            self.started.set()
        for value in self.progress_values:
            progress(value, "synthetic progress")
        if self.progress_done is not None:
            self.progress_done.set()
        if self.release is not None:
            if not self.release.wait(5):
                raise TimeoutError("release event was not set")
        if not self.ignore_cancel:
            cancellation.check()
        if self.failure is not None:
            raise self.failure
        if self.runner is not None:
            result = self.runner(context, snapshot)
        elif self.result is not None:
            result = self.result
        else:
            result = AnalysisResult(
                output={"analyzer": self.analyzer_id},
                evidence_ids=("message-1", "message-1/f0"),
            )
        if self.returned is not None:
            self.returned.set()
        return result
