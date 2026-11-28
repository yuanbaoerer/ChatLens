"""Privacy and filesystem boundary helpers for ChatLens analytics."""
from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Sequence
from pathlib import Path, PureWindowsPath

REDACTION_VERSION = "regex-v2"


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


# Boundaries must be ASCII-aware: Python's `\w` and `\b` include CJK, so `\b`
# would fail to match a URL/ID directly after a Chinese character.
_REDACTION_PATTERNS = (
    (re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE), "[链接]"),
    (re.compile(r"(?<![A-Za-z0-9])www\.[^\s<>\"']+", re.IGNORECASE), "[链接]"),
    # Unicode local parts and IDN domains are supported; no trailing
    # lookahead so an email before punctuation is still redacted.
    (re.compile(r"(?<![A-Za-z0-9._%+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[邮箱]"),
    (re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)"), "[电话]"),
    (
        re.compile(
            r"(?<![A-Za-z0-9])wxid_[A-Za-z0-9_-]+(?![A-Za-z0-9_])",
            re.IGNORECASE,
        ),
        "[微信ID]",
    ),
    (
        re.compile(
            r"(?<![A-Za-z0-9])(?:sk|ghp|gho|xox[baprs])-[A-Za-z0-9_-]{8,}"
            r"(?![A-Za-z0-9_])",
            re.IGNORECASE,
        ),
        "[密钥]",
    ),
    (re.compile(r"(?<!\d)(?:\d[ -]?){9,}\d(?!\d)"), "[数字ID]"),
)

# Applied after URL/email/ID patterns and configured name terms. The optional
# capitalized second token catches common Latin full names such as `@John Doe`.
_MENTION_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.])@[^\s<>\"'@，,。；;：:！!？?]{1,64}"
    r"(?:[ \t]+[A-Z][A-Za-z'’.-]{0,63})?"
)


def redact_text(
    text: str,
    *,
    max_chars: int = 500,
    terms: Sequence[str] = (),
) -> str:
    """Minimize free text before provider calls without normalizing its meaning.

    URLs, emails, phone numbers, WeChat/secret-like identifiers, @mentions and
    long digit sequences are replaced. Optional caller-authorized member names
    are replaced next, before the result is bounded. Whitespace is collapsed.
    Callers must not treat redaction as a substitute for consent or retention
    controls.
    """
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")
    if isinstance(terms, str):
        raise ValueError("terms must be a sequence of strings, not a string")
    normalized_terms = sorted(
        {
            term.strip()
            for term in terms
            if isinstance(term, str) and len(term.strip()) >= 2
        },
        key=len,
        reverse=True,
    )
    value = " ".join(str(text or "").split())
    for pattern, replacement in _REDACTION_PATTERNS:
        value = pattern.sub(replacement, value)
    if normalized_terms:
        value = re.sub(
            "|".join(re.escape(term) for term in normalized_terms),
            "[成员]",
            value,
            flags=re.IGNORECASE,
        )
    value = _MENTION_PATTERN.sub("[提及]", value)
    return value[:max_chars]


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
