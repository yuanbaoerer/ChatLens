"""Base protocol for ChatLens chat source adapters."""
from __future__ import annotations

from typing import Iterator, Protocol, runtime_checkable

from analytics.models import GroupInfo, Message, MessageCursor, MessagePage, Snapshot, TimeRange


@runtime_checkable
class ChatSourceAdapter(Protocol):
    """Read-only adapter contract producing normalized Messages and Snapshots."""

    def list_groups(self) -> list[GroupInfo]:
        """Return available group conversations from this source."""
        ...

    def iter_messages(
        self,
        group_id: str,
        time_range: TimeRange | None = None,
    ) -> Iterator[Message]:
        """Yield normalized top-level messages within the half-open [start, end) range."""
        ...

    def build_snapshot(
        self,
        group_id: str,
        time_range: TimeRange | None = None,
    ) -> Snapshot:
        """Build an immutable, deterministic Snapshot for a single group and time range."""
        ...

    def page_messages(
        self,
        group_id: str,
        time_range: TimeRange | None = None,
        *,
        limit: int = 100,
        after: MessageCursor | None = None,
    ) -> MessagePage:
        """Page normalized messages. A changed snapshot invalidates its cursors."""
        ...
