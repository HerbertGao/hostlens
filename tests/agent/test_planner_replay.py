"""Replay-mode Planner test over the committed ``planner_health_check`` cassette.

Task 6.2: drive ``PlannerAgent`` with a backend obtained from the
``llm_cassette`` fixture and assert the condensed ``PlannerResult`` is stable.

The formal cassette must be recorded against the real Anthropic API (task 6.4,
a manual / paid step done outside this change). Until that file exists this
test **cleanly skips** rather than fails — so CI is green now and the test
auto-activates the moment the cassette lands. To record it:

    HOSTLENS_LLM_MODE=record ANTHROPIC_API_KEY=... \
        pytest tests/agent/test_planner_replay.py

(in record mode the byte-stable synthetic ``target_registry`` from
``_scenario`` passes ``guard_record_targets``, so recording is allowed.)

``asyncio_mode = "auto"`` (pyproject) — no ``@pytest.mark.asyncio`` needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from hostlens.agent.planner import PlannerAgent, PlannerResult

from ._scenario import (
    CASSETTE_NAME,
    SCENARIO_INTENT,
    scenario_context_factory,
    scenario_settings,
    scenario_target_registry,
    scenario_tool_registry,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from hostlens.agent.backend import LLMBackend

# Path of the committed cassette this test replays (recorded by task 6.4).
_CASSETTE_PATH = Path(__file__).parent.parent / "fixtures" / "cassettes" / f"{CASSETTE_NAME}.jsonl"


async def test_planner_replay_structure_stable(
    llm_cassette: Callable[..., LLMBackend],
) -> None:
    # The formal cassette needs a real-API recording (task 6.4, manual). When
    # absent, skip cleanly so CI stays green; once committed the test replays.
    if not _CASSETTE_PATH.exists():
        pytest.skip(
            f"cassette {_CASSETTE_PATH.name} not yet recorded; record it with "
            "HOSTLENS_LLM_MODE=record ANTHROPIC_API_KEY=... "
            "pytest tests/agent/test_planner_replay.py"
        )

    registry = scenario_tool_registry()
    target_registry = scenario_target_registry()

    # In replay mode the fixture ignores ``target_registry`` and returns a
    # PlaybackBackend over the committed cassette; in record mode it uses the
    # registry to run ``guard_record_targets`` before recording (spec §需求:
    # record 模式下 fixture 据此 registry 过 guard, replay 模式忽略 registry).
    backend = llm_cassette(CASSETTE_NAME, target_registry=target_registry)

    planner = PlannerAgent(
        backend,
        registry,
        scenario_settings(),
        scenario_context_factory(target_registry),
    )
    result = await planner.run(SCENARIO_INTENT)

    # Structural stability — not exact wording (the formal cassette's narrative
    # comes from a real model and may evolve on re-record). What is contractual:
    # a well-formed PlannerResult that condenses the loop without raising.
    assert isinstance(result, PlannerResult)
    assert result.intent == SCENARIO_INTENT
    assert isinstance(result.narrative, str)
    assert isinstance(result.findings, list)
    assert result.loop_result.terminal_status in {
        "ok",
        "degraded_token_budget",
        "degraded_max_turns",
    }
