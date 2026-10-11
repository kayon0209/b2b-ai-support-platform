"""Client-visible OpenAPI contracts kept out of the domain unit suite."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.contract


@pytest.fixture(scope="module")
def openapi() -> dict[str, object]:
    from platform_core.main import app

    return app.openapi()


def test_published_openapi_has_stable_identity_and_nonempty_paths(
    openapi: dict[str, object],
) -> None:
    info = openapi["info"]
    paths = openapi["paths"]
    assert isinstance(info, dict)
    assert info.get("title") == "B2B AI Support Platform"
    assert isinstance(paths, dict) and len(paths) >= 20


def test_published_operations_have_unique_ids(openapi: dict[str, object]) -> None:
    paths = openapi["paths"]
    assert isinstance(paths, dict)
    ids: list[str] = []
    for path_item in paths.values():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            assert isinstance(operation, dict)
            operation_id = operation.get("operationId")
            assert isinstance(operation_id, str) and operation_id
            ids.append(operation_id)
    assert len(ids) == len(set(ids)), "OpenAPI operationId values must be unique"


def test_public_support_and_compliance_routes_are_in_openapi(
    openapi: dict[str, object],
) -> None:
    paths = openapi["paths"]
    assert isinstance(paths, dict)
    assert "/v1/support/sessions" in paths
    assert "/v1/tenant/compliance/export" in paths
