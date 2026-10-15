from fastapi.testclient import TestClient

from recantor.main import app


def test_openapi_has_stable_operational_and_versioned_routes() -> None:
    schema = TestClient(app).get("/openapi.json").json()

    assert schema["paths"]["/healthz"]["get"]["operationId"] == "healthz"
    assert schema["paths"]["/readyz"]["get"]["operationId"] == "readyz"
    assert schema["paths"]["/api/v1/meta"]["get"]["operationId"] == "getApiMeta"
    operation_ids = {
        method["operationId"]
        for path in schema["paths"].values()
        for method in path.values()
        if isinstance(method, dict) and "operationId" in method
    }
    assert {
        "submitMeetingEssenceDerivation",
        "getMeetingEssenceDerivationStatus",
        "getMeetingEssenceResultPage",
        "getMeetingEssenceRecordEvidence",
        "cancelMeetingEssenceDerivation",
        "retryMeetingEssenceDerivation",
    } <= operation_ids
