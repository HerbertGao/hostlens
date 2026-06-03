"""CLI rendering tests for the hypothesis segment of ``hostlens reports diff``.

Spec: ``openspec/changes/add-hypothesis-level-diff/specs/report-regression-diff/spec.md``
(`hostlens reports diff` 必须渲染 hypothesis 段).

These drive ``_render_diff`` directly with constructed ``RegressionDiff``s
(no store / no LLM) — the rendering contract is text-shape only. The fixed
substring ``按 supporting_findings 证据集匹配`` is a frozen contract string.
"""

from __future__ import annotations

import pytest

from hostlens.cli.reports import _render_diff
from hostlens.reporting.diff import (
    ConfidenceChange,
    HypothesisFingerprint,
    RegressionDiff,
)

MATCH_NOTE = "按 supporting_findings 证据集匹配"


def _render(diff: RegressionDiff, capsys: pytest.CaptureFixture[str]) -> str:
    _render_diff(diff)
    return capsys.readouterr().out


def test_unskipped_diff_emits_match_note(capsys: pytest.CaptureFixture[str]) -> None:
    out = _render(RegressionDiff(baseline_meta=None), capsys)
    assert MATCH_NOTE in out


def test_hypothesis_added_segment_rendered(capsys: pytest.CaptureFixture[str]) -> None:
    diff = RegressionDiff(
        baseline_meta=None,
        hypothesis_added=[
            HypothesisFingerprint(
                confidence="high", supporting_findings=["f1"], description="disk filling up"
            )
        ],
    )
    out = _render(diff, capsys)
    assert "hypothesis_added (1):" in out
    assert "+ high: disk filling up" in out


def test_hypothesis_resolved_and_confidence_changed(capsys: pytest.CaptureFixture[str]) -> None:
    diff = RegressionDiff(
        baseline_meta=None,
        hypothesis_resolved=[
            HypothesisFingerprint(
                confidence="low", supporting_findings=["f2"], description="gone cause"
            )
        ],
        hypothesis_confidence_changed=[
            ConfidenceChange(
                supporting_findings=["f3"],
                from_confidence="low",
                to_confidence="high",
                description="escalated",
            )
        ],
    )
    out = _render(diff, capsys)
    assert "- low: gone cause" in out
    assert "~ low -> high: escalated" in out


def test_unanchored_hint(capsys: pytest.CaptureFixture[str]) -> None:
    out = _render(RegressionDiff(baseline_meta=None, hypothesis_unanchored=3), capsys)
    assert "未锚定假设 (3, 两 run 合计)" in out
    assert "两 run 合计" in out


def test_ambiguous_keys_hint(capsys: pytest.CaptureFixture[str]) -> None:
    out = _render(RegressionDiff(baseline_meta=None, hypothesis_ambiguous_keys=2), capsys)
    assert "歧义键 (2)" in out
    assert "confidence 变化未计算" in out


def test_inspector_caveat_when_upgraded(capsys: pytest.CaptureFixture[str]) -> None:
    diff = RegressionDiff(baseline_meta=None, inspector_upgraded=["linux.disk.usage"])
    out = _render(diff, capsys)
    assert "注意: 存在 inspector 版本变更" in out
    assert "非真实诊断变化" in out


def test_skipped_diff_renders_no_hypothesis_segment(capsys: pytest.CaptureFixture[str]) -> None:
    diff = RegressionDiff(baseline_meta=None, diff_skipped_reason="baseline_not_ok")
    out = _render(diff, capsys)
    assert "diff 跳过: baseline_not_ok" in out
    assert MATCH_NOTE not in out
    assert "hypothesis_added" not in out
    assert "未锚定假设" not in out
    assert "歧义键" not in out


def test_hypothesis_segment_fixed_order(capsys: pytest.CaptureFixture[str]) -> None:
    diff = RegressionDiff(
        baseline_meta=None,
        inspector_upgraded=["insp.x"],
        hypothesis_added=[
            HypothesisFingerprint(confidence="high", supporting_findings=["a"], description="add")
        ],
        hypothesis_resolved=[
            HypothesisFingerprint(confidence="low", supporting_findings=["b"], description="res")
        ],
        hypothesis_confidence_changed=[
            ConfidenceChange(
                supporting_findings=["c"],
                from_confidence="low",
                to_confidence="high",
                description="cc",
            )
        ],
        hypothesis_unanchored=1,
        hypothesis_ambiguous_keys=1,
    )
    out = _render(diff, capsys)
    positions = [
        out.index(MATCH_NOTE),
        out.index("hypothesis_added (1):"),
        out.index("hypothesis_resolved (1):"),
        out.index("hypothesis_confidence_changed (1):"),
        out.index("未锚定假设"),
        out.index("歧义键"),
        out.index("注意: 存在 inspector 版本变更"),
    ]
    assert positions == sorted(positions)
