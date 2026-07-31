"""모델 API와 대시보드 BFF OpenAPI snapshot을 결정적으로 생성한다."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from chemiguard119.api import create_app
from chemiguard119.dashboard_contract import build_dashboard_bff_openapi
from chemiguard119.rules import (
    CAMEO_CLASS_RESULTS,
    PUBLIC_SCREENING_BRIEF_TEMPLATE,
    PUBLIC_SCREENING_LIMITATIONS,
    PUBLIC_SCREENING_REQUIRED_CHECKS,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_OPENAPI_PATH = (
    PROJECT_ROOT / "contracts" / "generated" / "model-api-v1.openapi.json"
)
DASHBOARD_BFF_OPENAPI_PATH = (
    PROJECT_ROOT / "contracts" / "dashboard-bff-v1.openapi.json"
)
VERIFIED_PAIR_SNAPSHOT_PATH = (
    PROJECT_ROOT / "data" / "evaluation" / "verified_pair_snapshot_2024.json"
)
CAMEO_CROSSWALK_PATH = PROJECT_ROOT / "config" / "cameo_crosswalk.csv"
DASHBOARD_PUBLIC_PAIR_CONTRACT_PATH = (
    PROJECT_ROOT / "config" / "dashboard_public_pair_contract.json"
)
DASHBOARD_PUBLIC_PAIR_CONTRACT_VERSION = "dashboard-public-pair-presentation-v1"
PUBLIC_CAMEO_REACTIVITY_URL = "https://cameochemicals.noaa.gov/reactivity"


def _serialized(payload: dict[str, Any]) -> str:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_crosswalk() -> dict[str, dict[str, str]]:
    with CAMEO_CROSSWALK_PATH.open(
        encoding="utf-8-sig",
        newline="",
    ) as stream:
        rows = list(csv.DictReader(stream))
    return {row["cas_number"]: row for row in rows}


def build_dashboard_public_pair_contract() -> dict[str, Any]:
    """현재 공개검증 15쌍의 대시보드 표시 필드를 원천 snapshot에 고정한다."""

    snapshot = json.loads(VERIFIED_PAIR_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    crosswalk = _load_crosswalk()
    pairs: list[dict[str, Any]] = []

    for source_pair in snapshot["pairs"]:
        cas_numbers = sorted((source_pair["cas_a"], source_pair["cas_b"]))
        raw_class_id = str(source_pair["raw_class_id"])
        classification = CAMEO_CLASS_RESULTS[raw_class_id]
        if (
            classification["risk_level"] != source_pair["risk_level"]
            or classification["risk_level_ko"] != source_pair["risk_level_ko"]
        ):
            raise ValueError(
                "검증 pair snapshot의 CAMEO class와 위험등급이 일치하지 않습니다."
            )

        mappings: list[dict[str, str]] = []
        for cas_number in cas_numbers:
            row = crosswalk.get(cas_number)
            if row is None:
                raise ValueError(
                    f"검증 pair CAS가 CAMEO crosswalk에 없습니다: {cas_number}"
                )
            mappings.append(
                {
                    field: row[field]
                    for field in (
                        "cas_number",
                        "cameo_chemical_id",
                        "selected_form",
                        "verification_status",
                        "verification_method",
                        "evidence_url",
                        "source_product",
                        "source_version",
                        "checked_at_utc",
                    )
                }
            )

        pairs.append(
            {
                "pair_key": "|".join(cas_numbers),
                "cas_numbers": cas_numbers,
                "status": source_pair["status"],
                "scope": "PUBLIC_SOURCE_CAMEO_SCREENING",
                "policy_mode": snapshot["policy_mode"],
                "rule_id": "CAMEO-REACTIVE-GROUP-COMPATIBILITY-MATRIX",
                "rule_version": "RUNTIME_MANIFEST_PINNED",
                "severity": classification["severity"],
                "risk_level": source_pair["risk_level"],
                "risk_level_ko": source_pair["risk_level_ko"],
                "raw_class_id": source_pair["raw_class_id"],
                "hazard_codes": source_pair["hazard_codes"],
                "gas_products": source_pair["gas_products"],
                "brief_text": PUBLIC_SCREENING_BRIEF_TEMPLATE.format(
                    risk_level_ko=source_pair["risk_level_ko"]
                ),
                "required_checks": list(PUBLIC_SCREENING_REQUIRED_CHECKS),
                "evidence_urls": source_pair["evidence_urls"],
                "limitations": list(PUBLIC_SCREENING_LIMITATIONS),
                "final_decision": "현장 지휘관 판단",
                "expert_reviewed": source_pair["expert_reviewed"],
                "human_confirmation_required": True,
                "mappings": mappings,
                "compatibility_evidence_urls": [PUBLIC_CAMEO_REACTIVITY_URL],
            }
        )

    expected_pair_count = snapshot["expected_unique_pair_count"]
    if len(pairs) != expected_pair_count:
        raise ValueError(
            "검증 pair snapshot의 pair 수와 expected_unique_pair_count가 다릅니다."
        )

    return {
        "contract_version": DASHBOARD_PUBLIC_PAIR_CONTRACT_VERSION,
        "source_snapshot": str(VERIFIED_PAIR_SNAPSHOT_PATH.relative_to(PROJECT_ROOT)),
        "source_snapshot_sha256": _sha256(VERIFIED_PAIR_SNAPSHOT_PATH),
        "crosswalk": str(CAMEO_CROSSWALK_PATH.relative_to(PROJECT_ROOT)),
        "crosswalk_sha256": _sha256(CAMEO_CROSSWALK_PATH),
        "source_metrics_version": snapshot["metrics_version"],
        "policy_mode": snapshot["policy_mode"],
        "pair_count": len(pairs),
        "is_probability": False,
        "offline_regression_only": True,
        "does_not_confirm_on_site_presence": True,
        "pairs": pairs,
    }


def expected_contracts() -> dict[Path, str]:
    return {
        MODEL_OPENAPI_PATH: _serialized(
            create_app(runtime=None, allow_anonymous=False).openapi()
        ),
        DASHBOARD_BFF_OPENAPI_PATH: _serialized(build_dashboard_bff_openapi()),
        DASHBOARD_PUBLIC_PAIR_CONTRACT_PATH: _serialized(
            build_dashboard_public_pair_contract()
        ),
    }


def write_contracts() -> list[Path]:
    written: list[Path] = []
    for path, content in expected_contracts().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def check_contracts() -> list[Path]:
    stale: list[Path] = []
    for path, expected in expected_contracts().items():
        if not path.exists() or path.read_text(encoding="utf-8") != expected:
            stale.append(path)
    return stale


def main() -> int:
    parser = argparse.ArgumentParser(
        description="버전 고정 OpenAPI 계약을 생성하거나 drift를 검사합니다."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="파일을 수정하지 않고 현재 코드와 snapshot이 같은지 검사",
    )
    args = parser.parse_args()

    if args.check:
        stale = check_contracts()
        if stale:
            for path in stale:
                print(f"STALE: {path.relative_to(PROJECT_ROOT)}")
            return 1
        print("계약 snapshot이 현재 코드와 일치합니다.")
        return 0

    for path in write_contracts():
        print(f"생성: {path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
