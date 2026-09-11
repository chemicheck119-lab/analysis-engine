"""공개 qrel 감사가 폐기한 run이 아니라 채택한 재실행에 연결되는지 검사한다."""

import json
import re
from pathlib import Path


def test_pool_audit_points_to_adopted_full_revision_and_artifacts():
    root = Path(__file__).resolve().parents[1] / "data/evaluation"
    audit = json.loads((root / "retriever_qrel_pool_audit_2026-09-09.json").read_text())
    readiness = json.loads((root / audit["adopted_evidence"]).read_text())
    revision = audit["source_commit"]
    assert re.fullmatch(r"[a-f0-9]{40}", revision)
    assert revision == readiness["source_commit"]
    assert audit["system"]["system_version"].endswith("@" + revision)
    assert (
        audit["system"]["private_run_sha256"] == readiness["hashes"]["adopted_pool_run"]
    )
    assert audit["private_pool_audit_sha256"] == readiness["hashes"]["pool_audit"]
    assert audit["candidate_sha256"] == readiness["hashes"]["candidates"]
    assert audit["database_sha256"] == readiness["hashes"]["database"]
    assert audit["system"]["system_artifact_sha256"] == readiness["hashes"]["retriever"]
    assert (
        audit["system"]["returned_occurrence_count"]
        == readiness["pool_audit"]["returned_occurrence_count"]
    )
    assert audit["candidate_count"] == readiness["review_progress"]["candidate_count"]
    assert (
        audit["system"]["private_run_sha256"]
        != audit["superseded_evidence"]["private_run_sha256"]
    )
    assert audit["independent_review_complete"] is False
    assert audit["is_performance_result"] is False
