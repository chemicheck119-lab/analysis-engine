from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CROSSWALK_PATH = ROOT / "config" / "cameo_crosswalk.csv"
PUBLIC_VERIFIED = "PUBLIC_SOURCE_VERIFIED"
EXACT_METHOD = "EXACT_CAS_AND_FORM_ON_OFFICIAL_DATASHEET"


def _rows() -> list[dict[str, str]]:
    with CROSSWALK_PATH.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_public_crosswalk_contains_six_officially_checked_core_substances() -> None:
    expected = {
        "7681-52-9": "4503",
        "7647-01-0": "3598",
        "64-17-5": "667",
        "67-64-1": "8",
        "108-88-3": "4654",
        "7440-23-5": "7794",
    }
    verified = {
        row["cas_number"]: row["cameo_chemical_id"]
        for row in _rows()
        if row["verification_status"] == PUBLIC_VERIFIED
    }

    assert expected.items() <= verified.items()


def test_every_public_verified_crosswalk_has_reproducible_provenance() -> None:
    verified = [
        row for row in _rows() if row["verification_status"] == PUBLIC_VERIFIED
    ]

    assert verified
    for row in verified:
        assert row["verification_method"] == EXACT_METHOD
        assert row["evidence_url"] == (
            "https://cameochemicals.noaa.gov/chemical/"
            f"{row['cameo_chemical_id']}"
        )
        assert row["source_product"] == "NOAA/EPA CAMEO Chemicals"
        assert row["source_version"]
        checked_at = datetime.fromisoformat(row["checked_at_utc"])
        assert checked_at.tzinfo is not None
        assert row["notes"]
