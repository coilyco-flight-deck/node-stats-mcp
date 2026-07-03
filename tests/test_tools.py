"""Behavioural tests for the node-stats tools.

The tools are registered with FastMCP without rebinding their names, so the
plain callables stay directly invokable here. The focus is the security
envelope: file reads are denied unless a root is allowlisted, and the allowlist
cannot be escaped.
"""

from __future__ import annotations

import importlib
from types import ModuleType

import pytest


def _load(monkeypatch: pytest.MonkeyPatch, tmp_root: str, roots: str) -> ModuleType:
    """Reimport the server module with ROOTFS + allowlist env applied at import time."""
    monkeypatch.setenv("ROOTFS", tmp_root)
    monkeypatch.setenv("NODE_STATS_READABLE_ROOTS", roots)
    import node_stats_mcp.server as server

    return importlib.reload(server)


def test_cpu_and_memory_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _load(monkeypatch, "/", "")
    cpu = server.get_cpu_info()
    assert "percent" in cpu
    assert cpu["logical_cores"] and cpu["logical_cores"] >= 1
    mem = server.get_memory_info()
    assert mem["virtual"]["total"] > 0


def test_file_reads_denied_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    (tmp_path / "hello.txt").write_text("hi")
    server = _load(monkeypatch, str(tmp_path), "")  # empty allowlist
    with pytest.raises(ValueError, match="disabled"):
        server.stat_path("hello.txt")


def test_allowlisted_read_and_escape(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    (tmp_path / "ok").mkdir()
    (tmp_path / "ok" / "f.txt").write_text("payload")
    (tmp_path / "secret.txt").write_text("nope")
    server = _load(monkeypatch, str(tmp_path), "/ok")

    got = server.read_text_head("ok/f.txt")
    assert got["text"] == "payload"
    assert got["truncated"] is False

    # A path outside the single allowed root is refused.
    with pytest.raises(ValueError, match="outside"):
        server.stat_path("secret.txt")


def test_read_is_capped(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    (tmp_path / "big.txt").write_text("x" * 1000)
    server = _load(monkeypatch, str(tmp_path), "/")
    got = server.read_text_head("big.txt", max_bytes=100)
    assert got["bytes_returned"] == 100
    assert got["truncated"] is True


def test_top_processes_validates_sort(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _load(monkeypatch, "/", "")
    with pytest.raises(ValueError, match="sort_by"):
        server.get_top_processes(sort_by="bogus")
