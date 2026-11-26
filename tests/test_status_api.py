"""Tests for src/status_api.py."""

import socket
import threading
import time

import pytest
import requests
from fastapi.testclient import TestClient

from status_api import StatusAPIServer, create_app


def test_status_returns_file_contents(tmp_path):
    path = tmp_path / "status.json"
    path.write_text('{"on_primary": true, "status": "NBN (primary)"}', encoding="utf-8")

    response = TestClient(create_app(path)).get("/status")

    assert response.status_code == 200
    assert response.json() == {"on_primary": True, "status": "NBN (primary)"}


def test_status_missing_file_returns_503(tmp_path):
    response = TestClient(create_app(tmp_path / "status.json")).get("/status")

    assert response.status_code == 503
    assert "error" in response.json()


@pytest.mark.parametrize("content", ["{bad json", "[1, 2]"])
def test_status_invalid_file_returns_503(tmp_path, content):
    path = tmp_path / "status.json"
    path.write_text(content, encoding="utf-8")

    response = TestClient(create_app(path)).get("/status")

    assert response.status_code == 503


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_server_serves_and_stops_cleanly(tmp_path):
    path = tmp_path / "status.json"
    path.write_text('{"status": "ok"}', encoding="utf-8")
    server = StatusAPIServer(path, "127.0.0.1", _free_port())
    stop_event = threading.Event()
    errors = []

    def _run():
        try:
            server.serve(stop_event)
        except RuntimeError as e:  # pragma: no cover - reported via the assertion below
            errors.append(e)

    thread = threading.Thread(target=_run)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        response = None
        while time.monotonic() < deadline:
            try:
                response = requests.get(server.url, timeout=1)
                break
            except requests.ConnectionError:
                time.sleep(0.05)
        assert response is not None
        assert response.json() == {"status": "ok"}
    finally:
        stop_event.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []


def test_server_bind_failure_raises(tmp_path):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        server = StatusAPIServer(tmp_path / "status.json", "127.0.0.1", port)

        with pytest.raises(RuntimeError, match="failed to start"):
            server.serve(threading.Event())
