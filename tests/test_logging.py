import io
import json
import logging

from pydantic import SecretStr

from scidatafusion.logging import JsonFormatter, configure_logging


def test_json_formatter_redacts_nested_secrets() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("test.redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info(
        "connector configured",
        extra={
            "api_key": "plain-secret",
            "context": {"token": "nested-secret", "source": "openalex"},
            "values": [SecretStr("list-secret"), 3],
        },
    )
    payload = json.loads(stream.getvalue())

    assert payload["message"] == "connector configured"
    assert payload["api_key"] == "[REDACTED]"
    assert payload["context"] == {"token": "[REDACTED]", "source": "openalex"}
    assert payload["values"] == ["[REDACTED]", 3]
    assert "plain-secret" not in stream.getvalue()


def test_formatter_serializes_exceptions_and_unknown_values() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "failed", (), None)
    record.custom = object()
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record.exc_info = __import__("sys").exc_info()

    payload = json.loads(formatter.format(record))

    assert payload["level"] == "ERROR"
    assert "RuntimeError: boom" in payload["exception"]
    assert isinstance(payload["custom"], str)


def test_formatter_redacts_query_and_bearer_secrets_in_messages() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        "test",
        logging.ERROR,
        __file__,
        1,
        "GET https://example.test/data?api_key=leaked&format=json Authorization: Bearer abc.def",
        (),
        None,
    )

    rendered = formatter.format(record)

    assert "leaked" not in rendered
    assert "abc.def" not in rendered
    assert rendered.count("[REDACTED]") == 2


def test_formatter_redacts_parameterized_message_secrets() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        "test",
        logging.ERROR,
        __file__,
        1,
        "request failed api_key=%s",
        ("sentinel-key-material",),
        None,
    )

    rendered = formatter.format(record)

    assert "sentinel-key-material" not in rendered
    assert "api_key=[REDACTED]" in rendered


def test_configure_logging_replaces_root_handlers() -> None:
    configure_logging("WARNING")

    root = logging.getLogger()
    assert root.level == logging.WARNING
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
