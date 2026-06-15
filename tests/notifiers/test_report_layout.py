"""Structured-layout rendering tests for the Telegram + Lark notifiers.

Spec: ``openspec/specs/notifier-telegram/spec.md`` and
``openspec/specs/notifier-lark/spec.md`` (§需求:结构化布局 — 抬头非 intent / 覆盖行 /
发现优先 / 四元组去重 / 同 message 不同 severity 不去重 / severity 排序 / 带来源 /
健康态 / 多 target 分节 / fleet 主机归因 / agent 单机退化 / 去重x分节组合).

Both channels render through the real ``render()`` entry point (which redacts
the report before templating), so the multi-target cases exercise the
proposal-B ``_redact_finding`` ``target_name`` pass-through — feeding a raw
(unredacted) report would mask a dropped ``target_name`` and turn a real
regression green.
"""

from __future__ import annotations

import json
from datetime import datetime

import httpx

from hostlens.inspectors.result import InspectorResult
from hostlens.notifiers.lark import LarkNotifier
from hostlens.notifiers.telegram import TelegramNotifier
from hostlens.reporting.models import (
    Finding,
    Report,
    RootCauseHypothesis,
    Severity,
)

_START = datetime(2026, 1, 1, 3, 30)
_END = datetime(2026, 1, 1, 3, 31)


# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #


def _ir(
    name: str,
    *,
    target: str = "web-1",
    status: str = "ok",
    findings: list[Finding] | None = None,
    error: str | None = None,
) -> InspectorResult:
    return InspectorResult(
        name=name,
        version="1.0.0",
        status=status,  # type: ignore[arg-type]
        target_name=target,
        duration_seconds=0.1,
        output={},
        findings=findings or [],
        error=error,
        missing=["svc"] if status == "requires_unmet" else [],
    )


def _single_report(
    findings: list[Finding],
    *,
    hypotheses: list[RootCauseHypothesis] | None = None,
    extra_results: list[InspectorResult] | None = None,
    intent: str = "对所有主机做夜间巡检并报告异常",
) -> Report:
    results = [_ir("linux.systemd", findings=findings)]
    if extra_results:
        results.extend(extra_results)
    report = Report.from_inspector_results(
        "web-1",
        results,
        started_at=_START,
        finished_at=_END,
        intent=intent,
    )
    if hypotheses:
        report = report.model_copy(update={"hypotheses": hypotheses})
    return report


def _fleet_report(
    results: list[InspectorResult],
    *,
    hypotheses: list[RootCauseHypothesis] | None = None,
) -> Report:
    report = Report.from_fleet_results(
        results,
        schedule_name="nightly",
        started_at=_START,
        finished_at=_END,
        intent="对 fleet 做夜间巡检",
    )
    if hypotheses:
        report = report.model_copy(update={"hypotheses": hypotheses})
    return report


def _tg() -> TelegramNotifier:
    return TelegramNotifier(
        instance_name="ops-tg",
        config={"bot_token": "x", "chat_id": "1"},
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
    )


def _lark() -> LarkNotifier:
    return LarkNotifier(
        instance_name="ops-lk",
        config={"webhook_url": "https://open.feishu.cn/hook/x"},
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
    )


def _tg_body(report: Report, severity: Severity) -> str:
    return _tg().render(report, severity=severity).body


def _lark_card(report: Report, severity: Severity) -> dict[str, object]:
    body = _lark().render(report, severity=severity).body
    card = json.loads(body)
    assert isinstance(card, dict)
    return card


def _lark_contents(card: dict[str, object]) -> list[str]:
    """Flatten every ``lark_md`` / ``plain_text`` content string from the card."""

    out: list[str] = []
    inner = card["card"]
    assert isinstance(inner, dict)
    header = inner["header"]
    assert isinstance(header, dict)
    title = header["title"]
    assert isinstance(title, dict)
    out.append(str(title["content"]))
    elements = inner["elements"]
    assert isinstance(elements, list)
    for el in elements:
        assert isinstance(el, dict)
        text = el.get("text")
        if isinstance(text, dict) and "content" in text:
            out.append(str(text["content"]))
    return out


# --------------------------------------------------------------------------- #
# 抬头不是 intent + 覆盖行
# --------------------------------------------------------------------------- #


def test_telegram_header_is_not_intent_and_has_coverage() -> None:
    report = _single_report(
        [Finding(severity="critical", message="磁盘使用率 95%")],
        intent="对所有主机做夜间巡检并报告异常这是一段很长的意图描述",
    )
    body = _tg_body(report, "critical")
    first_line = body.splitlines()[0]
    assert "Hostlens 巡检" in first_line
    assert "严重" in first_line
    assert "🔴" in first_line
    # The integral intent sentence must NOT be the title.
    assert "对所有主机做夜间巡检" not in first_line
    # Coverage line present (N/M 项检查).
    assert "项检查" in body


def test_lark_header_is_not_intent_and_isomorphic() -> None:
    report = _single_report(
        [Finding(severity="critical", message="磁盘使用率 95%")],
        hypotheses=[
            RootCauseHypothesis(
                description="磁盘写满", confidence="high", suggested_actions=["清理"]
            )
        ],
        intent="对所有主机做夜间巡检并报告异常这是一段很长的意图描述",
    )
    card = _lark_card(report, "critical")
    header = card["card"]["header"]  # type: ignore[index]
    title_content = header["title"]["content"]  # type: ignore[index]
    assert "Hostlens 巡检" in title_content
    assert "严重" in title_content
    assert "对所有主机做夜间巡检" not in title_content
    contents = _lark_contents(card)
    joined = "\n".join(contents)
    assert "项检查" in joined  # coverage
    assert "**根因分析**" in contents  # root cause section
    assert "**发现**" in contents


# --------------------------------------------------------------------------- #
# 覆盖行计入失败状态
# --------------------------------------------------------------------------- #


def _coverage_results() -> list[InspectorResult]:
    """5 ok / 1 requires_unmet / 2 failed (timeout + target_unreachable) = 8."""

    results = [_ir(f"linux.ok{i}") for i in range(5)]
    results.append(_ir("linux.skip", status="requires_unmet"))
    results.append(_ir("linux.t1", status="timeout", error="timed out"))
    results.append(_ir("linux.t2", status="target_unreachable", error="unreachable"))
    return results


def test_telegram_coverage_counts_failures() -> None:
    report = Report.from_inspector_results(
        "web-1", _coverage_results(), started_at=_START, finished_at=_END
    )
    body = _tg_body(report, "info")
    # ok=5, total=8, skipped=1, failed=2 → 5/8 项检查 · 1 项跳过 · 2 项失败
    assert "5/8 项检查" in body
    assert "1 项跳过" in body
    assert "2 项失败" in body
    # Invariant ok + skipped + failed == total (5 + 1 + 2 == 8).


def test_lark_coverage_counts_failures() -> None:
    report = Report.from_inspector_results(
        "web-1", _coverage_results(), started_at=_START, finished_at=_END
    )
    contents = _lark_contents(_lark_card(report, "info"))
    coverage = next(c for c in contents if "项检查" in c)
    assert "5/8 项检查" in coverage
    assert "1 项跳过" in coverage
    assert "2 项失败" in coverage


def test_coverage_omits_failed_clause_when_zero() -> None:
    # 3 ok + 1 requires_unmet, no timeout / unreachable / exception.
    results = [_ir(f"linux.ok{i}") for i in range(3)]
    results.append(_ir("linux.skip", status="requires_unmet"))
    report = Report.from_inspector_results("web-1", results, started_at=_START, finished_at=_END)
    body = _tg_body(report, "info")
    assert "项失败" not in body
    assert "3/4 项检查 · 1 项跳过" in body
    contents = _lark_contents(_lark_card(report, "info"))
    coverage = next(c for c in contents if "项检查" in c)
    assert "项失败" not in coverage


# --------------------------------------------------------------------------- #
# 发现优先(发现段在根因段之上)
# --------------------------------------------------------------------------- #


def test_telegram_findings_before_root_cause() -> None:
    report = _single_report(
        [Finding(severity="warning", message="磁盘使用率 80%")],
        hypotheses=[
            RootCauseHypothesis(
                description="日志暴涨占满磁盘",
                confidence="high",
                suggested_actions=["清理 /var/log", "扩容磁盘"],
            )
        ],
    )
    body = _tg_body(report, "warning")
    assert "*根因分析*" in body
    # 发现优先:发现段必须出现在根因分析段之前。
    assert body.index("*发现*") < body.index("*根因分析*")
    # 根因段仍正常渲染,只是位置移到发现之后。
    # confidence rendered as 中文 label
    assert "置信度 高" in body
    # suggested_actions listed with ↳
    assert "↳ 清理 /var/log" in body
    assert "↳ 扩容磁盘" in body


def test_lark_findings_before_root_cause() -> None:
    report = _single_report(
        [Finding(severity="warning", message="磁盘使用率 80%")],
        hypotheses=[
            RootCauseHypothesis(
                description="日志暴涨占满磁盘",
                confidence="medium",
                suggested_actions=["清理 /var/log"],
            )
        ],
    )
    contents = _lark_contents(_lark_card(report, "warning"))  # _lark_card asserts valid JSON
    assert "**根因分析**" in contents
    # 发现优先:发现段元素必须排在根因分析段元素之前。
    assert contents.index("**发现**") < contents.index("**根因分析**")
    # 根因段仍正常渲染,只是位置在后。
    assert any("置信度 中" in c for c in contents)
    assert any(c.startswith("↳ 清理 /var/log") for c in contents)


def test_lark_health_with_hypotheses_is_valid_json() -> None:
    # JSON-validity blind spot: ``findings == [] 且 hypotheses != []`` (a healthy
    # card that still carries a 根因分析 section). After the 发现优先 reorder the
    # 根因 block becomes the LAST ``elements`` entry — the spot most prone to a
    # dangling / double comma with the leading-comma `,{...}` card style. The
    # existing json.loads guards (``test_lark_renders_valid_json_for_every_scenario``)
    # never exercise this combination, so pin it.
    report = _single_report(
        [],
        hypotheses=[
            RootCauseHypothesis(
                description="历史日志暴涨", confidence="high", suggested_actions=["清理 /var/log"]
            )
        ],
    )
    contents = _lark_contents(_lark_card(report, "info"))  # _lark_card asserts valid JSON
    assert "**根因分析**" in contents
    assert "✅ 未发现异常" in contents
    assert "**发现**" not in contents


def test_lark_legacy_meta_none_is_valid_json() -> None:
    # Legacy schema-1.0 path: ``meta is None`` → the ``{% if report.meta %}``
    # coverage div is absent, so the findings/health block becomes the **first**
    # ``elements`` entry — a distinct leading-comma topology from every
    # meta-present case (where coverage is the leading no-comma element). The
    # 发现优先 reorder shifts the root-cause block to the tail, so pin both the
    # findings-present and the (most fragile) findings-empty + hypotheses shapes
    # through ``_lark_card``'s ``json.loads``; ``meta is None`` also drives the
    # fleet signal to False (non-fleet → flat), which must not crash.
    hyp = [
        RootCauseHypothesis(
            description="历史日志暴涨", confidence="high", suggested_actions=["清理 /var/log"]
        )
    ]
    # findings == [] ∧ hypotheses != [] ∧ meta is None (root-cause is last element)
    health = _single_report([], hypotheses=hyp).model_copy(
        update={"meta": None, "schema_version": "1.0"}
    )
    assert health.meta is None
    health_contents = _lark_contents(_lark_card(health, "info"))  # asserts valid JSON
    assert "**根因分析**" in health_contents
    assert "✅ 未发现异常" in health_contents
    # findings != [] ∧ hypotheses != [] ∧ meta is None (findings is first element)
    rich = _single_report(
        [Finding(severity="warning", message="磁盘使用率 80%")], hypotheses=hyp
    ).model_copy(update={"meta": None, "schema_version": "1.0"})
    assert rich.meta is None
    rich_contents = _lark_contents(_lark_card(rich, "warning"))  # asserts valid JSON
    assert "**发现**" in rich_contents
    assert rich_contents.index("**发现**") < rich_contents.index("**根因分析**")


# --------------------------------------------------------------------------- #
# 四元组去重 + 排序 + 来源
# --------------------------------------------------------------------------- #


def test_telegram_dedup_sort_and_source() -> None:
    dup = Finding(severity="warning", message="磁盘使用率 80%")
    low = Finding(severity="info", message="负载正常")
    report = _single_report([dup, dup, low])
    body = _tg_body(report, "warning")
    # The two identical (four-tuple equal) findings collapse to one line.
    assert body.count("磁盘使用率 80%") == 1
    # critical/warning ranks above info → warning line before info line.
    assert body.index("磁盘使用率 80%") < body.index("负载正常")
    # Each finding carries its source inspector name.
    assert "linux\\.systemd" in body


def test_lark_dedup_sort_and_source() -> None:
    dup = Finding(severity="warning", message="磁盘使用率 80%")
    low = Finding(severity="info", message="负载正常")
    report = _single_report([dup, dup, low])
    contents = _lark_contents(_lark_card(report, "warning"))
    disk = [c for c in contents if "磁盘使用率 80%" in c]
    assert len(disk) == 1
    assert "(linux.systemd)" in disk[0]
    disk_idx = next(i for i, c in enumerate(contents) if "磁盘使用率 80%" in c)
    load_idx = next(i for i, c in enumerate(contents) if "负载正常" in c)
    assert disk_idx < load_idx


# --------------------------------------------------------------------------- #
# 同 message 不同 severity 不去重
# --------------------------------------------------------------------------- #


def test_telegram_same_message_diff_severity_not_merged() -> None:
    report = _single_report(
        [
            Finding(severity="critical", message="服务 nginx 异常"),
            Finding(severity="warning", message="服务 nginx 异常"),
        ]
    )
    body = _tg_body(report, "critical")
    assert body.count("服务 nginx 异常") == 2
    # critical (🔴) ranks above warning (⚠️) in the 发现 section.
    crit = body.index("🔴 服务 nginx 异常")
    warn = body.index("⚠️ 服务 nginx 异常")
    assert crit < warn


def test_lark_same_message_diff_severity_not_merged() -> None:
    report = _single_report(
        [
            Finding(severity="critical", message="服务 nginx 异常"),
            Finding(severity="warning", message="服务 nginx 异常"),
        ]
    )
    contents = _lark_contents(_lark_card(report, "critical"))
    nginx = [c for c in contents if "服务 nginx 异常" in c]
    assert len(nginx) == 2


# --------------------------------------------------------------------------- #
# 健康态
# --------------------------------------------------------------------------- #


def test_telegram_health_no_empty_findings_section() -> None:
    report = _single_report([])
    body = _tg_body(report, "info")
    assert "✅ 未发现异常" in body
    assert "*发现*" not in body
    # Coverage line still present.
    assert "项检查" in body


def test_lark_health_no_empty_findings_section() -> None:
    report = _single_report([])
    contents = _lark_contents(_lark_card(report, "info"))
    assert "✅ 未发现异常" in contents
    assert "**发现**" not in contents


# --------------------------------------------------------------------------- #
# 多 target 分节
# --------------------------------------------------------------------------- #


def test_telegram_multi_target_sections() -> None:
    report = _fleet_report(
        [
            _ir(
                "linux.disk",
                target="hostA",
                findings=[Finding(severity="critical", message="磁盘满")],
            ),
            _ir(
                "linux.cpu",
                target="hostB",
                findings=[Finding(severity="warning", message="CPU 高")],
            ),
        ]
    )
    body = _tg_body(report, "critical")
    # Each section header carries the host's own max severity (spec「每节主机名 +
    # 该主机 severity」): hostA's finding is critical, hostB's is warning.
    assert "*hostA · 严重*" in body
    assert "*hostB · 警告*" in body
    # hostA section holds its finding; hostB section holds its own.
    assert body.index("hostA") < body.index("磁盘满")
    assert body.index("hostB") < body.index("CPU 高")


def test_lark_multi_target_sections() -> None:
    report = _fleet_report(
        [
            _ir(
                "linux.disk",
                target="hostA",
                findings=[Finding(severity="critical", message="磁盘满")],
            ),
            _ir(
                "linux.cpu",
                target="hostB",
                findings=[Finding(severity="warning", message="CPU 高")],
            ),
        ]
    )
    joined = "\n".join(_lark_contents(_lark_card(report, "critical")))
    # Per-host section header carries the host's own max severity.
    assert "**hostA · 严重**" in joined
    assert "**hostB · 警告**" in joined


def test_telegram_none_section_gets_header_when_sectioned() -> None:
    # A sectioned fleet report where one finding is unstamped (target_name None,
    # e.g. partial fleet stamping) renders an explicit "(未标注主机)" header for
    # the None group rather than silently folding it under the previous host.
    base = _fleet_report(
        [_ir("linux.disk", target="hostA", findings=[Finding(severity="info", message="seed")])]
    )
    report = base.model_copy(
        update={
            "findings": [
                Finding(severity="critical", message="磁盘满", target_name="hostA"),
                Finding(severity="warning", message="CPU 高", target_name="hostB"),
                Finding(severity="info", message="未知来源项", target_name=None),
            ]
        }
    )
    body = _tg_body(report, "critical")
    # Header uses fullwidth parens (U+FF08/FF09) — not MarkdownV2-reserved (ASCII
    # parens would need escaping); assert the CJK label, not the paren form.
    assert "未标注主机" in body
    # The unstamped finding renders under its own header, after the named hosts.
    assert body.index("未标注主机") < body.index("未知来源项")
    assert body.index("hostA") < body.index("未标注主机")


def test_lark_none_section_gets_header_when_sectioned() -> None:
    # Lark mirror of the above — its None section uses a leading-comma `,{...}`
    # card element, more fragile than Telegram's newline, so assert the header
    # renders AND the card is still valid JSON (no dangling/double comma).
    base = _fleet_report(
        [_ir("linux.disk", target="hostA", findings=[Finding(severity="info", message="seed")])]
    )
    report = base.model_copy(
        update={
            "findings": [
                Finding(severity="critical", message="磁盘满", target_name="hostA"),
                Finding(severity="warning", message="CPU 高", target_name="hostB"),
                Finding(severity="info", message="未知来源项", target_name=None),
            ]
        }
    )
    contents = _lark_contents(_lark_card(report, "critical"))  # _lark_card asserts valid JSON
    assert any("未标注主机" in c for c in contents)
    assert any("未知来源项" in c for c in contents)


def test_none_section_stays_last_even_when_unstamped_finding_is_first() -> None:
    # Regression: a None-target finding appearing FIRST in the flattened list
    # must NOT push the unlabeled section ahead of the named hosts —
    # group_by_target holds the None group aside and appends it last, so the
    # documented order ("None section after named hosts") holds for any input order.
    base = _fleet_report(
        [_ir("linux.disk", target="hostA", findings=[Finding(severity="info", message="seed")])]
    )
    report = base.model_copy(
        update={
            "findings": [
                Finding(severity="info", message="未知来源项", target_name=None),  # FIRST
                Finding(severity="critical", message="磁盘满", target_name="hostA"),
                Finding(severity="warning", message="CPU 高", target_name="hostB"),
            ]
        }
    )
    body = _tg_body(report, "critical")
    assert body.index("hostA") < body.index("未标注主机")
    assert body.index("hostB") < body.index("未标注主机")


# --------------------------------------------------------------------------- #
# 单台 finding 的 fleet 报告仍按主机标注(本提案核心修复)
# --------------------------------------------------------------------------- #


def test_telegram_single_finding_fleet_keeps_host_section() -> None:
    # A fleet covering ≥2 targets where only 1 host (hostA) has a finding (the
    # other ran clean). from_fleet_results stamps the finding's target_name, so
    # distinct(non-None) == 1 — but a fleet report MUST still section by host so
    # ops can see WHICH host. hostA has no hyphen, so no escaping concern.
    report = _fleet_report(
        [
            _ir(
                "linux.disk",
                target="hostA",
                findings=[Finding(severity="critical", message="磁盘满")],
            ),
            _ir("linux.cpu", target="hostB"),  # clean, no finding
        ]
    )
    body = _tg_body(report, "critical")
    assert "*hostA · 严重*" in body
    assert "磁盘满" in body


def test_lark_single_finding_fleet_keeps_host_section() -> None:
    report = _fleet_report(
        [
            _ir(
                "linux.disk",
                target="hostA",
                findings=[Finding(severity="critical", message="磁盘满")],
            ),
            _ir("linux.cpu", target="hostB"),  # clean, no finding
        ]
    )
    joined = "\n".join(_lark_contents(_lark_card(report, "critical")))
    assert "**hostA · 严重**" in joined
    assert "磁盘满" in joined


# --------------------------------------------------------------------------- #
# all-None fleet 退化(distinct non-None == 0 ∧ fleet)— 禁止孤立未标注主机节头
# --------------------------------------------------------------------------- #


def test_telegram_all_none_fleet_no_unlabeled_header() -> None:
    # Degenerate fleet (fleet=True but every finding's target_name is None →
    # distinct(non-None) == 0). The named path returns a single (None, …)
    # section, which the template's "节数 > 1 才渲未标注主机头" guard renders as a
    # headerless flat list — NO isolated "未标注主机" header.
    base = _fleet_report(
        [_ir("linux.disk", target="hostA", findings=[Finding(severity="info", message="seed")])]
    )
    report = base.model_copy(
        update={
            "findings": [
                Finding(severity="critical", message="磁盘满", target_name=None),
                Finding(severity="warning", message="CPU 高", target_name=None),
            ]
        }
    )
    body = _tg_body(report, "critical")
    assert "磁盘满" in body
    assert "CPU 高" in body
    assert "未标注主机" not in body


def test_lark_all_none_fleet_no_unlabeled_header() -> None:
    base = _fleet_report(
        [_ir("linux.disk", target="hostA", findings=[Finding(severity="info", message="seed")])]
    )
    report = base.model_copy(
        update={
            "findings": [
                Finding(severity="critical", message="磁盘满", target_name=None),
                Finding(severity="warning", message="CPU 高", target_name=None),
            ]
        }
    )
    contents = _lark_contents(_lark_card(report, "critical"))  # _lark_card asserts valid JSON
    assert any("磁盘满" in c for c in contents)
    assert any("CPU 高" in c for c in contents)
    assert not any("未标注主机" in c for c in contents)


# --------------------------------------------------------------------------- #
# fleet 信号 guard(钉住决策 1 的调用约定:agent 路径 target_type != "fleet")
# --------------------------------------------------------------------------- #


def test_agent_path_target_type_is_not_fleet() -> None:
    # Guard the decision-1 calling convention with zero model change: an agent
    # report (from_inspector_results) must never produce meta.target_type ==
    # "fleet" (it takes the target's runtime .type, defaulting to "local"), so
    # the render layer can safely treat ``target_type == "fleet"`` as the fleet
    # signal. Future code that mistakenly stamps "fleet" on the agent path would
    # trip this.
    report = Report.from_inspector_results(
        "web-1",
        [_ir("linux.systemd", findings=[Finding(severity="warning", message="x")])],
        started_at=_START,
        finished_at=_END,
        intent="单机巡检",
    )
    assert report.meta is not None
    assert report.meta.target_type != "fleet"
    assert report.meta.target_type == "local"
    # _single_report (the agent-path builder used across this suite) likewise.
    single = _single_report([Finding(severity="info", message="ok")])
    assert single.meta is not None
    assert single.meta.target_type != "fleet"


# --------------------------------------------------------------------------- #
# 去重 x 分节组合
# --------------------------------------------------------------------------- #


def test_telegram_dedup_x_section_cross_host_not_merged() -> None:
    # hostA & hostB: same inspector/message/severity but different target →
    # NOT merged (target_name in the dedup key). hostA also has an intra-host
    # exact duplicate that MUST merge.
    same = Finding(severity="critical", message="磁盘满")
    report = _fleet_report(
        [
            _ir("linux.disk", target="hostA", findings=[same, same]),
            _ir("linux.disk", target="hostB", findings=[same]),
        ]
    )
    body = _tg_body(report, "critical")
    # Cross-host: two sections, each one finding → message appears twice total.
    assert body.count("磁盘满") == 2
    # Both hosts' findings are critical → each section header shows 严重.
    assert "*hostA · 严重*" in body
    assert "*hostB · 严重*" in body


def test_lark_dedup_x_section_cross_host_not_merged() -> None:
    same = Finding(severity="critical", message="磁盘满")
    report = _fleet_report(
        [
            _ir("linux.disk", target="hostA", findings=[same, same]),
            _ir("linux.disk", target="hostB", findings=[same]),
        ]
    )
    contents = _lark_contents(_lark_card(report, "critical"))
    disk = [c for c in contents if "磁盘满" in c]
    assert len(disk) == 2
    joined = "\n".join(contents)
    assert "**hostA · 严重**" in joined
    assert "**hostB · 严重**" in joined


# --------------------------------------------------------------------------- #
# 单主机:agent 退化无分节 vs fleet 仍按主机标注
# --------------------------------------------------------------------------- #


def test_telegram_single_host_no_sections() -> None:
    # Single-target agent report (from_inspector_results, meta.target_type != "fleet"):
    # target_name on findings is None (per-target path) → flat, no host sectioning.
    report = _single_report([Finding(severity="warning", message="磁盘 80%")])
    assert all(f.target_name is None for f in report.findings)
    assert report.meta is not None and report.meta.target_type != "fleet"
    body = _tg_body(report, "warning")
    assert "*发现*" in body
    # No per-host section header (``*<host> · <sev>*``) is rendered. Asserted on
    # the section-header glyph form (``· <sev>*``) rather than a bare ``*`` count,
    # so the check is decoupled from whether a 根因分析 section follows after the
    # 发现优先 reorder (which would otherwise add its own ``*…*`` markers). The
    # 抬头 (first line) legitimately carries that glyph (``· 警告*``), so drop it
    # before scanning the body for per-host section headers.
    below_header = body.split("\n", 1)[1]
    assert "· 警告*" not in below_header
    assert "· 严重*" not in below_header
    assert "· 信息*" not in below_header


def test_telegram_single_host_fleet_still_sections() -> None:
    # A fleet of one target → distinct non-None == 1 → MUST still section
    # (the proposal's core fix: a single-host fleet finding keeps its
    # attribution). Section header form is ``*<host> · <sev>*``; ``only-host``'s
    # hyphen is MarkdownV2-escaped to ``only\-host`` so assert the escaped form
    # (the bare ``*only-host*`` never appears).
    report = _fleet_report(
        [
            _ir(
                "linux.disk",
                target="only-host",
                findings=[Finding(severity="warning", message="磁盘 80%")],
            )
        ]
    )
    body = _tg_body(report, "warning")
    assert "*only\\-host · 警告*" in body
    assert "磁盘 80%" in body


def test_lark_single_host_fleet_sections() -> None:
    # Lark mirror: a single-host fleet card MUST carry the per-host section
    # header. Lark serializes via ``tojson`` and does NOT MarkdownV2-escape, so
    # the hyphen renders literally → ``**only-host · 警告**`` (with sev_icon
    # prefix, asserted as a substring of the joined contents).
    report = _fleet_report(
        [
            _ir(
                "linux.disk",
                target="only-host",
                findings=[Finding(severity="warning", message="磁盘 80%")],
            )
        ]
    )
    joined = "\n".join(_lark_contents(_lark_card(report, "warning")))
    assert "**only-host · 警告**" in joined
    assert "磁盘 80%" in joined


def test_lark_agent_single_host_no_sections() -> None:
    # Lark agent-path no-section anchor (re-added after the fleet flip above
    # removed the old one): an agent single-host report (from_inspector_results,
    # meta.target_type != "fleet") MUST NOT render a per-host section header.
    # ``web-1`` is _single_report's default target. Routing through _lark_card
    # also transparently guards the card JSON validity.
    report = _single_report([Finding(severity="warning", message="磁盘 80%")])
    assert report.meta is not None and report.meta.target_type != "fleet"
    contents = _lark_contents(_lark_card(report, "warning"))
    assert not any("**web-1 ·" in c for c in contents)
    assert any("磁盘 80%" in c for c in contents)


# --------------------------------------------------------------------------- #
# MarkdownV2 转义不回归
# --------------------------------------------------------------------------- #


def test_telegram_markdownv2_escape_not_regressed() -> None:
    report = _single_report([Finding(severity="warning", message="disk 95.2% used (warn!)")])
    body = _tg_body(report, "warning")
    assert r"95\.2%" in body
    assert r"\(warn\!\)" in body


def test_lark_renders_valid_json_for_every_scenario() -> None:
    # Smoke: a rich report (findings + hypotheses + multi-target) must serialize
    # to valid JSON through render() (json.loads is inside _lark_card).
    report = _fleet_report(
        [
            _ir(
                "linux.disk",
                target="hostA",
                findings=[Finding(severity="critical", message='磁盘满 "quoted" \\ slash')],
            ),
            _ir(
                "linux.cpu",
                target="hostB",
                findings=[Finding(severity="warning", message="CPU 高")],
            ),
        ],
        hypotheses=[
            RootCauseHypothesis(
                description='根因含 "引号" 与 \\ 反斜杠',
                confidence="high",
                suggested_actions=['执行 "command"'],
            )
        ],
    )
    card = _lark_card(report, "critical")  # raises if invalid JSON
    assert card["msg_type"] == "interactive"
