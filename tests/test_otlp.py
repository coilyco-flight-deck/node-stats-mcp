import json
from typing import Any

import pytest

from node_stats_mcp.otlp import (
    LogRecord,
    MetricPoint,
    encode_request,
    logs_request,
    metrics_request,
    signal_url,
)


def test_signal_url_accepts_base_or_existing_signal_path() -> None:
    assert signal_url("http://collector:4318", "metrics") == ("http://collector:4318/v1/metrics")
    assert signal_url("https://collector/otel/v1/logs", "metrics") == (
        "https://collector/otel/v1/metrics"
    )
    with pytest.raises(ValueError, match="http or https"):
        signal_url("collector:4318", "logs")


def test_metrics_request_uses_otlp_json_integer_and_enum_encoding() -> None:
    payload: Any = metrics_request(
        [
            MetricPoint(
                "node_stats.counter",
                42,
                attributes={"ready": True, "count": 3},
                kind="sum",
            ),
            MetricPoint("node_stats.gauge", 1.5, unit="s"),
        ],
        {"service.name": "node-stats-mcp"},
        1_234,
    )

    metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    counter = metrics[0]
    assert counter["sum"]["aggregationTemporality"] == 2
    assert counter["sum"]["isMonotonic"] is True
    point = counter["sum"]["dataPoints"][0]
    assert point["asInt"] == "42"
    assert point["timeUnixNano"] == "1234"
    assert {attribute["key"] for attribute in point["attributes"]} == {
        "count",
        "ready",
    }
    assert metrics[1]["gauge"]["dataPoints"][0]["asDouble"] == 1.5
    json.loads(encode_request(payload))


def test_logs_request_keeps_json_body_and_otlp_timestamps() -> None:
    body = '{"source":"health","snapshot":{"event_count":2}}'
    payload: Any = logs_request(
        [
            LogRecord(
                body,
                attributes={"node_stats.partial": False},
                severity_number=9,
                severity_text="INFO",
            )
        ],
        {"host.name": "kai-server"},
        9_999,
    )

    record = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert record["timeUnixNano"] == "9999"
    assert record["observedTimeUnixNano"] == "9999"
    assert record["body"]["stringValue"] == body
    assert record["severityNumber"] == 9
