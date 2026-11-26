"""Normalized fields shared by the two Moect inputs."""
from typing import Any

from analytics.models import LinkMeta, QuoteRef
from analytics.privacy import Pseudonymizer
from analytics.snapshot import make_stable_message_id


_KIND_BY_TYPE = {
    1: "text", 3: "image", 34: "voice", 42: "card", 43: "video",
    47: "emoji", 48: "location", 49: "appmsg", 50: "voip", 62: "video",
    10000: "system", 10002: "system", 11000: "emoji",
}


def link_meta(kind: str, extra: dict[str, Any]) -> LinkMeta | None:
    url = str(extra.get("url") or "").strip()
    if kind not in ("link", "miniprogram") and not url:
        return None
    mini = extra.get("miniprogram")
    app_id = mini.get("appid") if isinstance(mini, dict) else None
    return LinkMeta(
        url=url,
        title=str(extra.get("title") or "").strip(),
        description=str(extra.get("description") or "").strip(),
        source_app=str(extra.get("source_name") or extra.get("source_app") or app_id or "").strip(),
    )


def quote_ref(
    raw: dict[str, Any], pseudonymizer: Pseudonymizer, scope: tuple[str, str, str],
) -> QuoteRef | None:
    extra = raw.get("extra")
    fallback = extra.get("reply") if isinstance(extra, dict) else None
    quote = next(
        (item for item in (raw.get("quote"), raw.get("_quote"), fallback) if isinstance(item, dict) and item),
        None,
    )
    if quote is None:
        return None
    server_id = str(quote.get("server_id") or "").strip()
    target_id = make_stable_message_id(server_id, scope=scope) if server_id not in ("", "0") else None
    kind = quote.get("kind")
    if not kind:
        try:
            base_type = int(quote.get("type") or 0) & 0xFFFFFFFF
        except (TypeError, ValueError, OverflowError):
            base_type = 0
        kind = _KIND_BY_TYPE.get(base_type, "other")
    return QuoteRef(
        target_message_id=target_id,
        sender_pseudonym=pseudonymizer.pseudonymize(
            quote.get("sender_wxid"), display_name=quote.get("sender"), is_system=(kind == "system"),
        ),
        kind=str(kind),
        text_excerpt=str(quote.get("text") or "")[:500],
    )
