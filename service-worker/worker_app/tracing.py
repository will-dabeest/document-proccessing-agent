import logging
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry.propagate import extract

logger = logging.getLogger(__name__)


def extract_trace_from_message(message: dict[str, Any]) -> Any:
    attrs = message.get("MessageAttributes") or {}
    tp = attrs.get("traceparent", {})
    if isinstance(tp, dict):
        val = tp.get("StringValue")
    else:
        val = None
    if not val:
        return otel_context.get_current()
    carrier = {"traceparent": val}
    return extract(carrier)
