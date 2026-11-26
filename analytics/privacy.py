"""Privacy and filesystem boundary helpers for ChatLens analytics."""
from __future__ import annotations

import hashlib
import hmac
from pathlib import Path, PureWindowsPath


class Pseudonymizer:
    """Scope-isolated deterministic one-way pseudonymizer for sender identities.

    Produces stable `u_<12hex>` pseudonyms using HMAC-SHA256 keyed by `scope_salt`.
    Order-independent within a scope, isolated across scopes, and never retains
    a reverse mapping or exposes raw identifiers.
    """

    def __init__(self, scope_salt: str) -> None:
        if not isinstance(scope_salt, str) or not scope_salt:
            raise ValueError("scope_salt must be a non-empty string")
        self._key = scope_salt.encode("utf-8")

    def pseudonymize(
        self,
        raw_id: str | None = None,
        *,
        display_name: str | None = None,
        is_system: bool = False,
    ) -> str:
        normalized_id = raw_id.strip() if isinstance(raw_id, str) else ""
        if normalized_id:
            digest = hmac.new(
                self._key,
                f"id:{normalized_id}".encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()[:12]
            return f"u_{digest}"

        if is_system:
            return "system"

        normalized_name = display_name.strip() if isinstance(display_name, str) else ""
        if normalized_name:
            digest = hmac.new(
                self._key,
                f"name:{normalized_name}".encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()[:12]
            return f"u_{digest}"

        return "u_unknown"

    def __call__(
        self,
        raw_id: str | None = None,
        *,
        display_name: str | None = None,
        is_system: bool = False,
    ) -> str:
        return self.pseudonymize(raw_id, display_name=display_name, is_system=is_system)

    def __repr__(self) -> str:
        return "Pseudonymizer(<redacted-scope>)"


def resolve_safe_path(root: Path, relative_path: str, *, base: Path | None = None) -> Path | None:
    """Resolve a portable relative path from base (default root) inside root."""
    if not isinstance(relative_path, str) or not relative_path or "\x00" in relative_path:
        return None
    portable = PureWindowsPath(relative_path)
    if portable.anchor or not portable.parts:
        return None
    if any(
        part.endswith((".", " ")) or ":" in part or PureWindowsPath(part).is_reserved()
        for part in portable.parts
    ):
        return None
    try:
        root = root.resolve()
        base = base.resolve() if base is not None else root
        if not base.is_relative_to(root):
            return None
        candidate = base.joinpath(*portable.parts).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate if candidate != root and candidate.is_relative_to(root) else None
