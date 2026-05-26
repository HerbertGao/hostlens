"""Tests for ``hostlens.agent.backend.is_daemon_mode`` hook.

M2 scope per spec §需求:`is_daemon_mode` M2 stub: the function ALWAYS
returns False; the signature is locked so M5 Scheduler can flip the
behavior without churning ``create_backend`` or its tests.

The signature contract test ensures:
- single parameter ``settings: Settings``
- return type ``bool``
"""

from __future__ import annotations

import inspect

from pydantic import SecretStr

from hostlens.agent.backend import is_daemon_mode
from hostlens.core.config import BackendSettings, Settings


def test_is_daemon_mode_returns_false_for_empty_settings() -> None:
    settings = Settings()
    assert is_daemon_mode(settings) is False


def test_is_daemon_mode_returns_false_for_backend_configured_settings() -> None:
    fake_key = (
        "sk-" + "ant-" + "validkey1234"
    )  # pragma: allowlist secret — fake fixture, not a real key
    settings = Settings(
        backend=BackendSettings(
            type="anthropic_api",
            api_key=SecretStr(fake_key),
        ),
    )
    assert is_daemon_mode(settings) is False


def test_is_daemon_mode_signature_is_locked() -> None:
    """Lock the function signature so M5 Scheduler hooks land without churn.

    Two locked properties:

    - exactly one positional parameter named ``settings``
    - return annotation ``bool`` (resolved via ``get_type_hints``;
      ``from __future__ import annotations`` in the source module makes the
      raw ``inspect.signature`` annotation a string)
    """

    import typing

    sig = inspect.signature(is_daemon_mode)
    params = list(sig.parameters.values())
    assert len(params) == 1, f"expected 1 param, got {len(params)}: {params!r}"
    assert params[0].name == "settings"

    # ``Settings`` is imported only under ``TYPE_CHECKING`` in
    # ``hostlens.agent.backend``, so ``get_type_hints`` needs an explicit
    # localns to resolve the forward reference.
    hints = typing.get_type_hints(is_daemon_mode, localns={"Settings": Settings})
    assert hints.get("return") is bool
    # Settings annotation resolves to the real Settings type.
    assert hints.get("settings") is Settings
