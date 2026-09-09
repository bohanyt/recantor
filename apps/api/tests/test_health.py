from fastapi import HTTPException, status
from fastapi.testclient import TestClient

from recantor.main import app
from recantor.routes.health import require_database


def test_healthz_is_process_only() -> None:
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "recantor-api"}


def test_readyz_reports_ready_when_database_probe_succeeds() -> None:
    async def database_ok() -> None:
        return None

    app.dependency_overrides[require_database] = database_ok
    try:
        client = TestClient(app)
        response = client.get("/readyz")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok"}


def test_readyz_preserves_unavailable_status() -> None:
    async def database_unavailable() -> None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        )

    app.dependency_overrides[require_database] = database_unavailable
    try:
        client = TestClient(app)
        response = client.get("/readyz")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}
