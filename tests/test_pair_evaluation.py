from __future__ import annotations

import csv
from pathlib import Path

import pytest

from chemiguard119 import pair_evaluation
from chemiguard119.pair_evaluation import PairEvaluationError, evaluate_verified_pairs


def _write_crosswalk(path: Path, verified_count: int) -> None:
    rows = [
        {
            "cas_number": f"CAS-{index}",
            "cameo_chemical_id": str(index),
            "selected_form": f"FORM-{index}",
            "verification_status": "PUBLIC_SOURCE_VERIFIED",
        }
        for index in range(verified_count)
    ]
    rows.append(
        {
            "cas_number": "CAS-DRAFT",
            "cameo_chemical_id": "999",
            "selected_form": "DRAFT",
            "verification_status": "CANDIDATE_UNVERIFIED",
        }
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_evaluation_runs_every_unique_verified_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_crosswalk(config_dir / "cameo_crosswalk.csv", verified_count=3)
    db_path = tmp_path / "runtime.sqlite"
    db_path.write_bytes(b"test-db")
    calls: list[tuple[str, str]] = []

    def fake_review(cas_a: str, cas_b: str, *_args, **_kwargs):
        calls.append((cas_a, cas_b))
        return {
            "status": "SCREENING_COMPLETED",
            "risk_level": "MEDIUM",
            "risk_level_ko": "중간",
            "risk_scale": {"raw_class_id": 1, "is_probability": False},
            "hazard_codes": ["R"],
            "gas_products": [],
            "cameo_group_screening": [{"pair_id": "1__2"}],
            "evidence_urls": ["https://cameochemicals.noaa.gov/reactivity"],
            "expert_reviewed": False,
            "reference_assurance": {
                "status": "PRIMARY_AUTHORITY_ONLY",
            },
        }

    monkeypatch.setattr(pair_evaluation, "review_pair", fake_review)
    monkeypatch.setattr(pair_evaluation, "validate_review_output", lambda _value: [])

    report = evaluate_verified_pairs(db_path, config_dir)

    assert calls == [
        ("CAS-0", "CAS-1"),
        ("CAS-0", "CAS-2"),
        ("CAS-1", "CAS-2"),
    ]
    assert report["expected_unique_pair_count"] == 3
    assert report["evaluated_pair_count"] == 3
    assert report["status_counts"] == {"SCREENING_COMPLETED": 3}
    assert report["reference_assurance_status_counts"] == {"PRIMARY_AUTHORITY_ONLY": 3}
    assert report["offline_regression_only"] is True
    assert report["does_not_confirm_on_site_presence"] is True
    assert report["is_probability"] is False


def test_evaluation_requires_at_least_two_verified_substances(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_crosswalk(config_dir / "cameo_crosswalk.csv", verified_count=1)
    db_path = tmp_path / "runtime.sqlite"
    db_path.write_bytes(b"test-db")

    with pytest.raises(PairEvaluationError, match="2개 이상"):
        evaluate_verified_pairs(db_path, config_dir)
