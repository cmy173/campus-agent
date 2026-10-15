"""Recoverable tool errors must reach the model without echoing user values."""
from app.errors import ApiError
from app.routers.agent import error_envelope


def test_parameter_field_types_survive_gateway():
    result = error_envelope(ApiError(400, "INVALID_INPUT", "参数无效", details={
        "validation_errors": [{"field": "limit", "type": "int_type"}]}))
    assert result["status"] == "error"
    assert result["error"]["details"] == {"validation_errors": [{"field": "limit", "type": "int_type"}]}


def test_other_exception_details_not_forwarded_to_model():
    result = error_envelope(ApiError(500, "PRIVATE_ERROR", "请求失败", details={"private": "secret"}))
    assert "details" not in result["error"]
