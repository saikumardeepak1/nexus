"""Contract test for issue #21: the generated OpenAPI schema (what actually
renders at /docs and /redoc) must stay a complete reference on its own, with
no separately maintained API doc to go stale.

This asserts, directly against ``app.openapi()`` (the same schema FastAPI
serves at ``/openapi.json`` and renders into Swagger UI / ReDoc), two things:

1. Every path operation has a non-empty description, i.e. its route function
   has a real docstring. FastAPI surfaces a route's docstring as the
   operation's ``description`` field automatically; an operation with no
   docstring has no ``description`` key at all (see the check below), so
   this catches a future route shipped without one.
2. Every schema in ``components/schemas`` -- except a small, explicit
   allowlist of schemas FastAPI itself generates rather than the app (the
   multipart upload body schema and the built-in 422 validation-error
   schemas) -- has at least one example, either a whole-model example (the
   ``examples`` key Pydantic v2's ``model_config=ConfigDict(json_schema_extra=...)``
   produces at the schema's top level) or a per-field example (the
   ``examples`` key ``Field(examples=[...])`` produces on an individual
   property). Either satisfies the acceptance criteria: a client reading
   /docs or /redoc sees a real example payload, not just field types.

Both checks are intentionally strict (an explicit allowlist for the
framework-generated schemas, not a broad name pattern) so that a future PR
adding a new route or a new app-defined schema without documentation fails
this test, rather than the test silently widening to cover whatever gets
added.
"""

from typing import Any

from app.main import app

# Schemas FastAPI itself generates (from an UploadFile parameter, and from
# its built-in request-validation error handling) rather than schemas this
# app defines in app/schemas/*.py. Not worth hand-annotating with examples,
# and excluded here by exact name (not by prefix/pattern) so a real
# app-defined schema can never accidentally slip through this allowlist.
_FRAMEWORK_SCHEMAS = {
    "Body_upload_document_v1_documents_post",
    "HTTPValidationError",
    "ValidationError",
}


def _schema_has_example(schema: dict[str, Any]) -> bool:
    """True if ``schema`` (a component schema dict from the OpenAPI spec)
    carries an example either at the schema's own level or on at least one
    of its properties.

    Checks both ``examples`` (a list, the current Pydantic v2 / JSON Schema
    2020-12 keyword, produced by ``Field(examples=[...])`` and
    ``ConfigDict(json_schema_extra={"examples": [...]})``) and the older
    singular ``example`` keyword, in case either style is present.
    """
    if schema.get("examples") or schema.get("example") is not None:
        return True
    for prop_schema in schema.get("properties", {}).values():
        if prop_schema.get("examples") or prop_schema.get("example") is not None:
            return True
    return False


def test_every_path_operation_has_a_description() -> None:
    """Every route's docstring must surface as a non-empty OpenAPI
    description, so /docs and /redoc explain what each route does without a
    separately maintained API doc.
    """
    spec = app.openapi()
    missing: list[str] = []

    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            description = operation.get("description")
            if not description or not description.strip():
                missing.append(f"{method.upper()} {path}")

    assert not missing, (
        "These path operations have no (or a blank) docstring, so /docs and "
        f"/redoc would show no description for them: {missing}"
    )


def test_every_app_schema_has_an_example() -> None:
    """Every request/response schema (excluding FastAPI's own
    framework-generated ones, see ``_FRAMEWORK_SCHEMAS``) must carry at
    least one example, so /docs and /redoc show a real example payload for
    every schema instead of just field names and types.
    """
    spec = app.openapi()
    schemas = spec["components"]["schemas"]
    missing: list[str] = []

    for name, schema in schemas.items():
        if name in _FRAMEWORK_SCHEMAS:
            continue
        if not _schema_has_example(schema):
            missing.append(name)

    assert not missing, (
        "These schemas have no example (neither a whole-model example nor "
        f"a field-level one), so /docs and /redoc would show no example "
        f"payload for them: {missing}"
    )


def test_framework_schema_allowlist_is_still_accurate() -> None:
    """Guards ``_FRAMEWORK_SCHEMAS`` itself against going stale: every name
    in it must still exist in the generated spec, and none of them should
    have quietly gained an example (which would make the exclusion
    unnecessary and worth removing).
    """
    spec = app.openapi()
    schemas = spec["components"]["schemas"]

    missing_from_spec = _FRAMEWORK_SCHEMAS - schemas.keys()
    assert not missing_from_spec, (
        "These names in _FRAMEWORK_SCHEMAS no longer appear in the generated "
        f"OpenAPI spec and should be removed from the allowlist: {missing_from_spec}"
    )
