"""Replay-mode Planner test over the committed ``planner_health_check`` cassette.

Task 6.2 / 6.5: drive ``PlannerAgent`` with a backend obtained from the
``llm_cassette`` fixture (replay mode) over the committed cassette and assert
the condensed ``PlannerResult`` is stable.

The committed cassette ``tests/fixtures/cassettes/planner_health_check.jsonl``
is **not** a real-Claude recording — it is generated deterministically from the
scripted synthetic ``scenario_fake_backend`` via
``_scenario.regenerate_committed_cassette`` (run
``python -m tests.agent._scenario`` to rebuild it after the scenario changes).
Because the scenario is byte-stable, replay always hits — this test RUNS and
PASSES in the CI default (replay) mode with no ``ANTHROPIC_API_KEY``.

``asyncio_mode = "auto"`` (pyproject) — no ``@pytest.mark.asyncio`` needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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


async def test_planner_replay_structure_stable(
    llm_cassette: Callable[..., LLMBackend],
) -> None:
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
