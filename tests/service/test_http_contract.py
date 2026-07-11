from __future__ import annotations

import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from service.api import http
from service.auth import AuthenticationError, b64e


class _PasswordState:
    initialized = True
    pwd_epoch = 7


class _AuthManager:
    password_state = _PasswordState()

    def __init__(self) -> None:
        self.fail_remember = False

    def server_public_key_b64(self) -> str:
        return "server-key"

    def verify_remember_proof(self, *, session_id: str, proof: bytes):
        if self.fail_remember:
            raise AuthenticationError("bad proof")
        assert session_id == "session-1"
        assert proof == b"proof"
        return SimpleNamespace(session_id=session_id)

    def issue_remember_token(self, session) -> tuple[str, float]:
        assert session.session_id == "session-1"
        return "remember-token", time.time() + 120


class _Runtime:
    def __init__(self) -> None:
        self.solve_calls = []

    def current_status(self):
        return {"default_config": {"running": False}}

    async def solve_task(self, config_id, task, set_log=None):
        self.solve_calls.append((config_id, task, bool(set_log)))
        return {"status": "ok", "task": task, "result": 0}


def _client(monkeypatch, auth_manager: _AuthManager) -> TestClient:
    runtime = _Runtime()
    fake_context = SimpleNamespace(
        auth_manager=auth_manager,
        runtime=runtime,
        ensure_runtime_logger_attached=lambda: None,
    )
    monkeypatch.setattr(http, "context", fake_context)
    app = FastAPI()
    app.include_router(http.router)
    return TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))


def test_health_contract(monkeypatch):
    client = _client(monkeypatch, _AuthManager())

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["statuses"] == {"default_config": {"running": False}}
    assert payload["auth"] == {
        "initialized": True,
        "pwd_epoch": 7,
        "server_sign_public_key": "server-key",
    }


def test_remember_auth_sets_cookie(monkeypatch):
    client = _client(monkeypatch, _AuthManager())

    response = client.post("/auth/remember", json={"session_id": "session-1", "proof": b64e(b"proof")})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert "baas_remember=remember-token" in response.headers["set-cookie"]


def test_remember_auth_failure_returns_401(monkeypatch):
    auth_manager = _AuthManager()
    auth_manager.fail_remember = True
    client = _client(monkeypatch, auth_manager)

    response = client.post("/auth/remember", json={"session_id": "session-1", "proof": b64e(b"proof")})

    assert response.status_code == 401
    assert response.json()["detail"] == "bad proof"


def test_logout_deletes_cookie(monkeypatch):
    client = _client(monkeypatch, _AuthManager())

    response = client.post("/auth/logout")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert "baas_remember=" in response.headers["set-cookie"]


def test_android_solve_contract(monkeypatch):
    monkeypatch.setenv("BAAS_ANDROID", "1")
    client = _client(monkeypatch, _AuthManager())

    response = client.post("/android/solve", json={"config_id": "default_config", "task": "cafe_reward"})

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "data": {"status": "ok", "task": "cafe_reward", "result": 0},
    }
    assert http.context.runtime.solve_calls == [("default_config", "cafe_reward", True)]


def test_android_solve_requires_task(monkeypatch):
    monkeypatch.setenv("BAAS_ANDROID", "1")
    client = _client(monkeypatch, _AuthManager())

    response = client.post("/android/solve", json={"config_id": "default_config"})

    assert response.status_code == 400
    assert response.json()["detail"] == "task is required"
