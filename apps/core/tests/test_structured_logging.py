"""Stage 17 — JSON stdout logging: request id preserved, secrets scrubbed."""

import json
import logging

from apps.core.logging import JSONFormatter
from apps.core.request_context import set_request_id


def _record(msg="hello", **extra):
    record = logging.LogRecord(
        name="apps.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_emits_one_json_line_with_core_fields():
    set_request_id("req-abc")
    line = JSONFormatter().format(_record("sale completed"))
    payload = json.loads(line)
    assert payload["message"] == "sale completed"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "apps.test"
    assert payload["request_id"] == "req-abc"
    assert "time" in payload


def test_scrubs_sensitive_extra_fields():
    line = JSONFormatter().format(
        _record(customer_phone="08000000000", database_url="postgres://x", ok="keep")
    )
    payload = json.loads(line)
    assert payload["customer_phone"] == "[scrubbed]"
    assert payload["database_url"] == "[scrubbed]"
    assert payload["ok"] == "keep"


def test_never_raises_on_unserialisable_extra():
    line = JSONFormatter().format(_record(obj=object()))
    payload = json.loads(line)
    assert "obj" in payload
