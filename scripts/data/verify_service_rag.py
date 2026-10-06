"""Verify actual incident step RAG contracts using synthetic local requests."""

import argparse
import json
from copy import deepcopy
from pathlib import Path
from fastapi.testclient import TestClient
from chemiguard119.api import ModelRuntime, create_app
from chemiguard119.action_examples import BOTH_CONFIRMED, ONE_CONFIRMED, UNCONFIRMED
from chemiguard119.rag import GroundedRagService, RagConfig
from chemiguard119.utils import sha256_file, write_json
from prepare_response_candidate import PINNED, ROOT, tree_hashes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-metadata", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Fresh output directory required")
    expected = PINNED
    if args.candidate_metadata:
        metadata = json.loads(args.candidate_metadata.read_text())
        if (
            metadata.get("baseline_hashes") != PINNED
            or metadata.get("status") != "UNAPPROVED_LOCAL_CANDIDATE"
        ):
            raise RuntimeError("CANDIDATE_BASELINE_MISMATCH")
        expected = metadata["runtime_hashes"]
        if set(expected) != set(PINNED):
            raise RuntimeError("CANDIDATE_FILES_MISMATCH")
    for name, checksum in expected.items():
        if sha256_file(args.runtime / name) != checksum:
            raise RuntimeError("PINNED_RUNTIME_MISMATCH")
    before = tree_hashes(args.runtime)
    runtime = ModelRuntime.load(
        db_path=args.runtime / "chemiguard119.sqlite",
        resolver_model_path=args.runtime / "resolver.joblib",
        retriever_model_path=args.runtime / "retriever.joblib",
        config_dir=ROOT / "config",
        environment="development",
    )
    args.output.mkdir(parents=True)
    rows = []
    app = create_app(
        runtime=runtime,
        allow_anonymous=True,
        deployment_environment="development",
        rag_service=GroundedRagService(RagConfig(mode="extractive")),
    )
    with TestClient(app) as client:
        scenarios = [
            ("unconfirmed", UNCONFIRMED, None),
            ("one_confirmed", ONE_CONFIRMED, None),
        ]
        scenarios += [
            (name, BOTH_CONFIRMED, text)
            for name, text in [
                (
                    "leak",
                    "차아염소산나트륨 탱크에서 누출이 있고 옆 저장고에는 염산이 있습니다.",
                ),
                (
                    "fire",
                    "차아염소산나트륨 탱크에 화재가 발생했고 옆 저장고에는 염산이 있습니다.",
                ),
                (
                    "exposure_unknown_route",
                    "차아염소산나트륨에 작업자가 노출되었습니다. 옆 저장고에는 염산이 있습니다.",
                ),
                (
                    "exposure",
                    "차아염소산나트륨에 작업자의 피부가 노출되었습니다. 피부에 닿았습니다. 옆 저장고에는 염산이 있습니다.",
                ),
            ]
        ]
        for name, base, text in scenarios:
            analysis_request = deepcopy(base["analysis"])
            if text:
                analysis_request["input"]["text"] = text
            response = client.post(
                "/api/v1/agents/incidents/step", json={"analysis": analysis_request}
            )
            body = response.json()
            write_json(
                args.output / f"{name}.json",
                {"synthetic_request": analysis_request, "response": body},
            )
            assert response.status_code == 200, (name, response.status_code)
            analysis = body.get("analysis") or {}
            rag = analysis.get("grounded_rag") or {}
            statements = rag.get("statements") or []
            citations = rag.get("citations") or []
            ids = {r["source_id"] for r in citations}
            assert all(
                r.get("source_ids") and set(r["source_ids"]) <= ids for r in statements
            ), name
            if name in {"unconfirmed", "one_confirmed"}:
                assert (
                    not statements
                    and rag.get("status") == "NOT_RUN_REQUIRES_CONFIRMED_PAIR"
                ), name
            else:
                assert statements and rag.get("used_llm") is False, name
                if args.candidate_metadata:
                    assert {"7681-52-9", "7647-01-0"} <= {
                        r.get("cas_number") for r in citations
                    }, name
                    if name == "exposure_unknown_route":
                        assert not any("MSDS 04장" in c["title"] for c in citations), (
                            name
                        )
                        assert any(
                            "노출 경로를 확인" in note for note in rag["limitations"]
                        ), name
                    chapter = {
                        "leak": "06",
                        "fire": "05",
                        "exposure": "04",
                        "exposure_unknown_route": "08",
                    }[name]
                    assert all(
                        f"MSDS {chapter}장"
                        in next(
                            c["title"]
                            for c in citations
                            if c["source_id"] == s["source_ids"][0]
                        )
                        for s in statements[:2]
                    ), name
            rows.append(
                {
                    "scenario": name,
                    "http_status": response.status_code,
                    "agent_status": body.get("status"),
                    "rag_status": rag.get("status"),
                    "statement_count": len(statements),
                    "citation_ids_valid": True,
                    "citation_titles": [r.get("title") for r in citations],
                    "citation_ids": sorted(ids),
                    "citation_cas": sorted(
                        {r.get("cas_number") for r in citations if r.get("cas_number")}
                    ),
                    "semantic_grounding_verified": rag.get(
                        "semantic_grounding_verified"
                    ),
                    "field_relevance_reviewed": False,
                    "response_notes": [
                        note
                        for note in rag.get("limitations", [])
                        if note.startswith("자료 확인:")
                    ],
                }
            )
    assert before == tree_hashes(args.runtime)
    write_json(
        args.output / "verification.json",
        {
            "scope": "LOCAL_REAL_MODEL_STEP_SYNTHETIC_REQUESTS_EXTRACTIVE",
            "runtime_unchanged": True,
            "runtime_hashes": expected,
            "script_sha256": sha256_file(Path(__file__)),
            "results": rows,
            "live_call_verified": False,
            "bff_frontend_e2e_verified": False,
            "source_section_relevance_verified": False,
            "section_selection_contract_verified": bool(args.candidate_metadata),
        },
    )
    print(
        f"{len(rows)} local model-step scenarios passed; field relevance remains unverified"
    )


if __name__ == "__main__":
    main()
