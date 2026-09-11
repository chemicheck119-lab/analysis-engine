"""공개 계측만으로 과거 집계를 재계산하고 원문 비공개 경계를 검사한다."""

import json
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = runpy.run_path(str(ROOT / "scripts/export_action_brief_evidence.py"))


def test_public_runs_reproduce_all_locked_report_aggregates():
    public = json.loads(
        (ROOT / "data/evaluation/action_brief_v1_runs.json").read_text()
    )
    summary = json.loads(
        (ROOT / "data/evaluation/action_brief_v1_summary.json").read_text()
    )
    assert set(public["reports"]) == set(summary["report_sha256"])
    for name, report in public["reports"].items():
        assert report["private_report_sha256"] == summary["report_sha256"][name]
        assert MODULE["recompute"](report) == report["recomputed"]
        for row in report.get("first_pass", []) + report["runs"]:
            if "checks" in row:
                assert row["passed"] is all(row["checks"].values())
    final = public["reports"]["action-brief-v1-r7-final/report.json"]["recomputed"]
    assert (final["execution_count"], final["failure_count"], final["rule_calls"]) == (
        120,
        0,
        16,
    )


def test_export_rejects_changed_private_report_before_reading_contents(tmp_path):
    (tmp_path / "report.json").write_text('{"input":"PRIVATE"}')
    with pytest.raises(ValueError, match="hash"):
        MODULE["export"](tmp_path, {"report_sha256": {"report.json": "0" * 64}})


def test_projection_drops_raw_fields_and_rejects_non_metric_values():
    project = MODULE["project_row"]
    assert project({"passed": True, "raw_input": "PRIVATE"}, ("passed",)) == {
        "passed": True
    }
    with pytest.raises(ValueError, match="비공개"):
        project({"status": "신고 원문 테스트"}, ("status",))
    with pytest.raises(ValueError, match="bool"):
        project({"checks": {"check": "PRIVATE"}}, ("checks",))
