"""Call tags, derived at read time from what call extraction already stored."""

from __future__ import annotations

_TYPE_TAGS = frozenset({"service_request", "out_of_scope", "personal", "business", "spam"})
_URGENCY_TAGS = frozenset({"emergency", "same_day", "routine", "quote"})


def tags_for_call(call: dict) -> list[str]:
    """Return the call's type tag, then its urgency tag. Unknown values produce none."""
    tags = []
    call_type = call.get("call_type")
    if type(call_type) is str and call_type in _TYPE_TAGS:
        tags.append(call_type)
    urgency = call.get("urgency")
    if type(urgency) is str and urgency in _URGENCY_TAGS:
        tags.append(urgency)
    return tags
