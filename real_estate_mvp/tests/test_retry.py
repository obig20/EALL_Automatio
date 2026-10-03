import pytest

from real_estate_mvp.retry import is_transient_error, with_transient_retry


class PermanentError(Exception):
    status_code = 400


class TemporaryError(Exception):
    status_code = 503


def test_transient_errors_retry_until_success():
    attempts = 0

    def operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("temporary")
        return "ok"

    result = with_transient_retry(
        operation, attempts=3, wait_min_seconds=0, wait_max_seconds=0
    )
    assert result == "ok"
    assert attempts == 3


def test_permanent_errors_do_not_retry():
    attempts = 0

    def operation():
        nonlocal attempts
        attempts += 1
        raise PermanentError("bad request")

    with pytest.raises(PermanentError):
        with_transient_retry(
            operation, attempts=3, wait_min_seconds=0, wait_max_seconds=0
        )
    assert attempts == 1


def test_provider_status_classification():
    assert is_transient_error(TemporaryError())
    assert not is_transient_error(PermanentError())