from scidatafusion.errors import AppError, ErrorCode


def test_problem_details_are_stable_and_include_context() -> None:
    error = AppError(
        ErrorCode.EXTERNAL_SERVICE_ERROR,
        "source timed out",
        details={"connector": "openalex"},
        retryable=True,
    )

    problem = error.to_problem_details(instance="/v1/tasks/tsk_123")

    assert str(error) == "source timed out"
    assert problem == {
        "type": "urn:scidatafusion:error:external_service_error",
        "title": "External Service Error",
        "detail": "source timed out",
        "code": "external_service_error",
        "retryable": True,
        "details": {"connector": "openalex"},
        "instance": "/v1/tasks/tsk_123",
    }


def test_problem_details_omit_empty_optional_fields() -> None:
    problem = AppError(ErrorCode.INVALID_REQUEST, "bad input").to_problem_details()

    assert "details" not in problem
    assert "instance" not in problem
