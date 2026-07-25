import asyncio
import json
from typing import Any

import pytest

from node_stats_mcp import exporter


def _config() -> exporter.ExportConfig:
    return exporter.ExportConfig(
        endpoint="http://collector:4318",
        interval_seconds=60,
        volume_interval_seconds=900,
        limit=50,
        max_log_bytes=2_048,
        max_payload_bytes=65_536,
        max_metric_points=2_000,
        timeout_seconds=5.0,
    )


def _snapshots() -> dict[str, dict[str, Any]]:
    return {
        "contention": {
            "pressure_stall_information": {"cpu": {"some": {"avg10": 1.25, "total": 100}}},
            "vm_pressure_counters": {"pgmajfault": 3},
            "block_devices": [
                {
                    "device": "nvme0n1",
                    "read_bytes": 10,
                    "write_bytes": 20,
                    "read_count": 1,
                    "write_count": 2,
                    "busy_time": 3,
                }
            ],
            "errors": [],
        },
        "kubelet": {
            "node": {
                "cpu": {"usageNanoCores": 1_000_000_000},
                "memory": {"workingSetBytes": 4_096},
                "filesystem": {"usedBytes": 1_000},
                "runtime": {},
            },
            "pods": [
                {
                    "namespace": "apps",
                    "pod": "high-cardinality-pod-abc123",
                    "cpu": {"usageNanoCores": 500_000_000},
                    "memory": {"workingSetBytes": 2_048},
                    "ephemeral_storage": {"usedBytes": 512},
                }
            ],
            "pod_count": 1,
            "errors": [],
        },
        "health": {
            "node": {
                "unschedulable": False,
                "taints": [],
                "conditions": [{"type": "Ready", "status": "True"}],
            },
            "events": [{"type": "Warning", "reason": "DiskPressure", "count": 2}],
            "errors": [],
        },
        "scheduled_work": {
            "jobs": [{"namespace": "ops", "active": 0, "succeeded": 1, "failed": 0}],
            "cronjobs": [
                {
                    "namespace": "ops",
                    "cronjob": "backup",
                    "last_successful_age_seconds": 60,
                    "active_jobs": [],
                    "suspended": False,
                }
            ],
            "errors": [],
        },
        "freshness": {
            "checks": [{"name": "backup", "status": "fresh", "age_seconds": 60}],
            "errors": [],
        },
        "conditions": {
            "sources": [
                {
                    "name": "external-secrets",
                    "items": [
                        {
                            "namespace": "ops",
                            "name": "registry",
                            "conditions": [{"type": "Ready", "status": "True"}],
                        }
                    ],
                    "errors": [],
                }
            ],
            "errors": [],
        },
        "filesystem": {
            "root": {
                "total_bytes": 10_000,
                "available_bytes": 4_000,
                "pressure_used_bytes": 6_000,
                "used_percent": 60.0,
                "inodes_used_percent": 10.0,
                "status": "ok",
            }
        },
        "volumes": {
            "namespaces": [
                {
                    "namespace": "apps",
                    "used_bytes": 2_048,
                    "volume_count": 1,
                    "pod_count": 1,
                    "truncated_volume_count": 0,
                }
            ],
            "volumes": [
                {
                    "namespace": "apps",
                    "persistent_volume_claim": "data",
                    "persistent_volume": "opaque-generated-name",
                    "usage": {"size_bytes": 2_048},
                    "requested_bytes": 4_096,
                    "capacity_bytes": 4_096,
                    "scan_status": "scanned",
                    "pvc_finalizers": [],
                    "pv_finalizers": [],
                    "pvc_conditions": [],
                }
            ],
            "unattributed_paths": [],
            "unattributed_path_count": 0,
            "errors": [],
        },
    }


def test_metric_mapping_uses_rollups_without_pod_or_pv_labels() -> None:
    points = exporter.metric_points(_snapshots(), cycle_duration_seconds=0.25)

    assert any(
        point.name == "node_stats.kubernetes.namespace.memory.working_set"
        and point.value == 2_048
        and point.attributes == {"namespace": "apps"}
        for point in points
    )
    assert any(
        point.name == "node_stats.kubernetes.volume.claim.used"
        and point.attributes == {"namespace": "apps", "claim": "data"}
        for point in points
    )
    assert all("pod" not in point.attributes for point in points)
    assert all("persistent_volume" not in point.attributes for point in points)
    assert "high-cardinality-pod-abc123" not in json.dumps([point.attributes for point in points])
    assert "opaque-generated-name" not in json.dumps([point.attributes for point in points])


def test_oversize_source_log_becomes_valid_truncation_envelope() -> None:
    body = exporter.bounded_log_body("health", {"events": ["x" * 10_000]}, 2_048)
    decoded = json.loads(body)

    assert len(body.encode()) <= 2_048
    assert decoded["source"] == "health"
    assert decoded["truncated"] is True
    assert decoded["original_bytes"] > 10_000
    assert decoded["summary"] == {}


def test_collect_sources_preserves_a_source_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_contention(limit: int) -> dict[str, Any]:
        raise OSError("pressure unavailable")

    async def empty_async(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"errors": []}

    monkeypatch.setattr(exporter.server, "get_node_pressure_stalls", fail_contention)
    monkeypatch.setattr(exporter.server, "get_k3s_resource_usage", empty_async)
    monkeypatch.setattr(exporter.server, "get_k3s_node_health", empty_async)
    monkeypatch.setattr(exporter.server, "get_k3s_scheduled_work", empty_async)
    monkeypatch.setattr(exporter.server, "get_configured_freshness", lambda: {"errors": []})
    monkeypatch.setattr(exporter.server, "get_k3s_configured_conditions", empty_async)
    monkeypatch.setattr(exporter.server, "get_filesystem_pressure", lambda: {"root": {}})

    snapshots = asyncio.run(exporter.collect_sources(10, include_volume=False))

    assert set(snapshots) == {
        "contention",
        "kubelet",
        "health",
        "scheduled_work",
        "freshness",
        "conditions",
        "filesystem",
    }
    assert snapshots["contention"]["collection_error"] == "OSError: pressure unavailable"
    assert snapshots["kubelet"] == {"errors": []}


def test_run_cycle_posts_independent_bounded_metric_and_log_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_collect(limit: int, *, include_volume: bool) -> dict[str, dict[str, Any]]:
        assert limit == 50
        assert include_volume is True
        return _snapshots()

    requests: list[tuple[str, bytes, float]] = []

    def fake_post(url: str, body: bytes, timeout: float) -> int:
        requests.append((url, body, timeout))
        return 200

    monkeypatch.setattr(exporter, "collect_sources", fake_collect)

    result = asyncio.run(exporter.run_cycle(_config(), include_volume=True, post=fake_post))

    assert result.succeeded
    assert {request[0] for request in requests} == {
        "http://collector:4318/v1/metrics",
        "http://collector:4318/v1/logs",
    }
    assert all(len(body) <= _config().max_payload_bytes for _, body, _ in requests)
    assert all(timeout == 5.0 for _, _, timeout in requests)
    decoded = [json.loads(body) for _, body, _ in requests]
    assert any("resourceMetrics" in payload for payload in decoded)
    assert any("resourceLogs" in payload for payload in decoded)


def test_one_signal_failure_does_not_cancel_the_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_collect(limit: int, *, include_volume: bool) -> dict[str, dict[str, Any]]:
        return {"filesystem": {"root": {"status": "ok"}}}

    calls: list[str] = []

    def fake_post(url: str, body: bytes, timeout: float) -> int:
        calls.append(url)
        if url.endswith("/metrics"):
            raise OSError("collector refused metrics")
        return 200

    monkeypatch.setattr(exporter, "collect_sources", fake_collect)
    result = asyncio.run(exporter.run_cycle(_config(), include_volume=False, post=fake_post))

    assert len(calls) == 2
    assert result.metrics_status is None
    assert result.logs_status == 200
    assert result.errors == ("metrics: OSError: collector refused metrics",)
