from fastapi.testclient import TestClient

from recantor.main import app


def test_openapi_has_stable_operational_and_versioned_routes() -> None:
    schema = TestClient(app).get("/openapi.json").json()

    assert schema["paths"]["/healthz"]["get"]["operationId"] == "healthz"
    assert schema["paths"]["/readyz"]["get"]["operationId"] == "readyz"
    assert schema["paths"]["/api/v1/meta"]["get"]["operationId"] == "getApiMeta"
