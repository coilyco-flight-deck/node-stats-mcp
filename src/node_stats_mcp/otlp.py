"""Small dependency-free OTLP/HTTP JSON encoder and client."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

AttributeValue = str | int | float | bool
MetricKind = Literal["gauge", "sum"]


@dataclass(frozen=True)
class MetricPoint:
    """One gauge or cumulative-sum point."""

    name: str
    value: int | float
    unit: str = "1"
    attributes: dict[str, AttributeValue] = field(default_factory=dict)
    kind: MetricKind = "gauge"


@dataclass(frozen=True)
class LogRecord:
    """One OTLP log record with a JSON string body."""

    body: str
    attributes: dict[str, AttributeValue] = field(default_factory=dict)
    severity_number: int = 9
    severity_text: str = "INFO"


def signal_url(endpoint: str, signal: Literal["metrics", "logs"]) -> str:
    """Normalize a collector base or signal URL to the requested OTLP signal."""
    parsed = urlsplit(endpoint.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("NODE_STATS_OTLP_ENDPOINT must be an http or https URL")
    if parsed.query or parsed.fragment:
        raise ValueError("NODE_STATS_OTLP_ENDPOINT must not contain a query or fragment")
    path = parsed.path.rstrip("/")
    for suffix in ("/v1/metrics", "/v1/logs", "/v1/traces"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit((parsed.scheme, parsed.netloc, f"{path}/v1/{signal}", "", ""))


def _any_value(value: AttributeValue) -> dict[str, object]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": value}


def _attributes(values: dict[str, AttributeValue]) -> list[dict[str, object]]:
    return [{"key": key, "value": _any_value(value)} for key, value in sorted(values.items())]


def _number_data_point(
    point: MetricPoint,
    time_unix_nano: int,
) -> dict[str, object]:
    value_field: dict[str, object]
    if isinstance(point.value, int):
        value_field = {"asInt": str(point.value)}
    else:
        value_field = {"asDouble": point.value}
    return {
        **value_field,
        "timeUnixNano": str(time_unix_nano),
        "attributes": _attributes(point.attributes),
    }


def metrics_request(
    points: list[MetricPoint],
    resource_attributes: dict[str, AttributeValue],
    time_unix_nano: int,
) -> dict[str, object]:
    """Build an ExportMetricsServiceRequest-compatible OTLP JSON object."""
    metrics: list[dict[str, object]] = []
    for point in points:
        data_point = _number_data_point(point, time_unix_nano)
        data: dict[str, object]
        if point.kind == "sum":
            data = {
                "sum": {
                    "aggregationTemporality": 2,
                    "isMonotonic": True,
                    "dataPoints": [data_point],
                }
            }
        else:
            data = {"gauge": {"dataPoints": [data_point]}}
        metrics.append({"name": point.name, "unit": point.unit, **data})
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": _attributes(resource_attributes)},
                "scopeMetrics": [
                    {
                        "scope": {
                            "name": "node_stats_mcp.exporter",
                            "version": "0.1.0",
                        },
                        "metrics": metrics,
                    }
                ],
            }
        ]
    }


def logs_request(
    records: list[LogRecord],
    resource_attributes: dict[str, AttributeValue],
    time_unix_nano: int,
) -> dict[str, object]:
    """Build an ExportLogsServiceRequest-compatible OTLP JSON object."""
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": _attributes(resource_attributes)},
                "scopeLogs": [
                    {
                        "scope": {
                            "name": "node_stats_mcp.exporter",
                            "version": "0.1.0",
                        },
                        "logRecords": [
                            {
                                "timeUnixNano": str(time_unix_nano),
                                "observedTimeUnixNano": str(time_unix_nano),
                                "severityNumber": record.severity_number,
                                "severityText": record.severity_text,
                                "body": {"stringValue": record.body},
                                "attributes": _attributes(record.attributes),
                            }
                            for record in records
                        ],
                    }
                ],
            }
        ]
    }


def encode_request(payload: dict[str, object]) -> bytes:
    """Encode compact UTF-8 JSON for OTLP/HTTP."""
    return json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()


def post_json(url: str, body: bytes, timeout_seconds: float) -> int:
    """POST one bounded OTLP JSON body and return the HTTP status."""
    request = Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "node-stats-mcp/0.1.0",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        response.read(4096)
        return int(response.status)
