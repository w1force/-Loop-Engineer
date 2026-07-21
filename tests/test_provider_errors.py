# tests/test_provider_errors.py
from core.provider_errors import (
    FatalProviderError,
    PromptTooLongError,
    ProviderError,
    TransientProviderError,
    UserInterruptError,
)


def test_transient_is_provider_error_with_status_body():
    e = TransientProviderError("conn reset", status=429, body=b"busy")
    assert isinstance(e, ProviderError)
    assert e.status == 429
    assert e.body == b"busy"
    assert str(e) == "conn reset"


def test_prompt_too_long_and_fatal_are_provider_errors():
    assert isinstance(PromptTooLongError("x", status=400), ProviderError)
    assert isinstance(FatalProviderError("x", status=401), ProviderError)


def test_isinstance_dispatch_distinguishes_subclasses():
    assert isinstance(TransientProviderError("x"), TransientProviderError)
    assert not isinstance(FatalProviderError("x"), TransientProviderError)
    assert not isinstance(PromptTooLongError("x"), FatalProviderError)


def test_user_interrupt_is_not_provider_error():
    # UserInterruptError 走独立路径, 不被 except ProviderError 接住
    assert not isinstance(UserInterruptError(), ProviderError)
