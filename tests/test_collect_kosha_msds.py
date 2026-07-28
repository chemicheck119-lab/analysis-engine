from __future__ import annotations

import csv
import importlib.util
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "data" / "collect_kosha_msds.py"
)
SPEC = importlib.util.spec_from_file_location("collect_kosha_msds_script", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
collect_kosha_msds = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collect_kosha_msds)


def _write_priority(path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "cas_number",
                "expansion_rank",
                "missing_official_evidence",
            ),
        )
        writer.writeheader()
        writer.writerows(
            [
                {
                    "cas_number": "64-17-5",
                    "expansion_rank": "9",
                    "missing_official_evidence": "",
                },
                {
                    "cas_number": "67-56-1",
                    "expansion_rank": "3",
                    "missing_official_evidence": "KOSHA_MSDS|CAMEO_PUBLIC_CROSSWALK",
                },
                {
                    "cas_number": "7664-93-9",
                    "expansion_rank": "4",
                    "missing_official_evidence": "KOSHA_MSDS",
                },
                {
                    "cas_number": "71-43-2",
                    "expansion_rank": "6",
                    "missing_official_evidence": "KOSHA_MSDS",
                },
            ]
        )


def test_select_priority_cas_uses_expansion_rank_and_missing_kosha(
    tmp_path: Path,
) -> None:
    priority = tmp_path / "priority.csv"
    _write_priority(priority)

    selected = collect_kosha_msds.select_priority_cas(
        priority,
        limit=2,
        skip_cas={"7664-93-9"},
    )

    assert selected == ["67-56-1", "71-43-2"]


def test_collect_batch_preserves_partial_failure_without_fabricating_rows() -> None:
    class FakeClient:
        def collect_cas(self, cas: str, *, sections: tuple[int, ...]):
            if cas == "7664-93-9":
                raise collect_kosha_msds.KoshaApiError(
                    "KOSHA_NETWORK_ERROR",
                    "통신 실패",
                    retryable=True,
                )
            return {
                "cas_number": cas,
                "status": "COLLECTED",
                "request_count": len(sections) + 1,
                "records": [
                    {
                        column: (
                            "RID-1"
                            if column == "레코드ID"
                            else cas
                            if column == "CAS번호"
                            else ""
                        )
                        for column in collect_kosha_msds.KOSHA_STAGING_COLUMNS
                    }
                ],
            }

    rows, results = collect_kosha_msds.collect_batch(
        FakeClient(),
        ["67-56-1", "7664-93-9"],
        sections=(6, 10),
    )

    assert len(rows) == 1
    assert rows[0]["CAS번호"] == "67-56-1"
    assert results[0]["status"] == "COLLECTED"
    assert results[0]["record_count"] == 1
    assert results[1] == {
        "cas_number": "7664-93-9",
        "status": "FAILED",
        "error": {
            "code": "KOSHA_NETWORK_ERROR",
            "message": "통신 실패",
            "retryable": True,
        },
    }
