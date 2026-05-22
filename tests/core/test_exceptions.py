from __future__ import annotations

import hostlens.core.exceptions as exceptions_module
from hostlens.core.exceptions import (
    ConfigError,
    HostlensError,
    InspectorError,
    TargetError,
)


def test_subclasses_inherit_from_hostlens_error() -> None:
    assert isinstance(ConfigError("x"), HostlensError)
    assert isinstance(TargetError("x"), HostlensError)
    assert isinstance(InspectorError("x"), HostlensError)


def test_hostlens_error_catches_all_subclasses() -> None:
    caught: list[type[HostlensError]] = []
    for exc_cls in (ConfigError, TargetError, InspectorError):
        try:
            raise exc_cls("boom")
        except HostlensError as e:
            caught.append(type(e))
    assert caught == [ConfigError, TargetError, InspectorError]


def test_config_error_accepts_optional_original_exception() -> None:
    """`ConfigError` exposes `original` so `load_settings()` can chain the
    underlying `pydantic.ValidationError` for callers that need raw details.
    The base message must remain accessible via `str(...)` unchanged.
    """

    cause = ValueError("underlying")
    err = ConfigError("formatted message", original=cause)
    assert err.original is cause
    assert str(err) == "formatted message"

    # Default keeps original=None so existing call sites continue to work.
    err_no_cause = ConfigError("just a message")
    assert err_no_cause.original is None


def test_module_exports_exactly_four_exception_classes() -> None:
    public_names = [name for name in dir(exceptions_module) if not name.startswith("_")]
    assert sorted(public_names) == [
        "ConfigError",
        "HostlensError",
        "InspectorError",
        "TargetError",
    ]
    assert len(public_names) == 4
