from __future__ import annotations

import json
from typing import Any

import pytest

from chemiguard119.api_models import GroundedRagAnswer
from chemiguard119.rag import GroundedRagService, RagConfig


def _rule_review() -> dict[str, Any]:
    return {
        "executed": True,
        "status": "SCREENING_COMPLETED",
        "result": {
            "status": "SCREENING_COMPLETED",
            "risk_level": "HIGH",
            "risk_level_ko": "높음",
            "brief_text": "공개 CAMEO 근거에서 높은 충돌 위험이 확인됐습니다.",
            "required_checks": ["실제 혼합 여부 확인", "배수로 연결 여부 확인"],
            "limitations": ["최종 결정은 현장 지휘관이 수행합니다."],
            "evidence_urls": [
                "https://cameochemicals.noaa.gov/chemical/4503",
                "https://cameochemicals.noaa.gov/reactivity",
            ],
        },
    }


def _evidence() -> list[dict[str, Any]]:
    return [
        {
            "role": "INCIDENT",
            "retrieval": {
                "results": [
                    {
                        "evidence_id": "KOSHA-7681-52-9-08",
                        "source": "KOSHA",
                        "title": "노출방지 및 개인보호구",
                        "body_preview": "적절한 보호구를 착용하고 누출 구역을 통제한다.",
                        "cas_number": "7681-52-9",
                        "source_url": "https://msds.kosha.or.kr/example/7681-52-9",
                    }
                ]
            },
        }
    ]


def test_unconfirmed_pair_never_calls_llm() -> None:
    def forbidden_request(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise AssertionError("현장 확인 전에 LLM을 호출했습니다.")

    service = GroundedRagService(
        RagConfig(mode="llm", model="test-model"), requester=forbidden_request
    )

    answer = service.answer(
        _evidence(),
        {
            "executed": False,
            "status": "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS",
        },
    )

    assert answer["status"] == "NOT_RUN_REQUIRES_CONFIRMED_PAIR"
    assert answer["used_llm"] is False
    assert answer["statements"] == []
    GroundedRagAnswer.model_validate(answer)


def test_extractive_mode_returns_rule_and_official_evidence() -> None:
    service = GroundedRagService(RagConfig(mode="extractive"))

    answer = service.answer(_evidence(), _rule_review())

    assert answer["status"] == "FALLBACK_EXTRACTIVE"
    assert answer["fallback_reason"] == "EXTRACTIVE_MODE"
    assert answer["used_llm"] is False
    assert {item["source_id"] for item in answer["citations"]} == {
        "RULE_RESULT",
        "KOSHA-7681-52-9-08",
    }
    assert all(item["source_ids"] for item in answer["statements"])
    GroundedRagAnswer.model_validate(answer)


def test_completed_rule_without_public_urls_does_not_create_fake_citation() -> None:
    review = _rule_review()
    review["result"]["evidence_urls"] = []
    service = GroundedRagService(RagConfig(mode="extractive"))

    answer = service.answer([], review)

    assert answer["status"] == "NO_GROUNDED_EVIDENCE"
    assert answer["statements"] == []
    assert answer["citations"] == []
    GroundedRagAnswer.model_validate(answer)


def test_non_official_evidence_url_is_not_sent_to_rag() -> None:
    evidence = _evidence()
    evidence[0]["retrieval"]["results"][0]["source_url"] = (
        "https://untrusted.example.test/kosha-copy"
    )
    service = GroundedRagService(RagConfig(mode="extractive"))

    answer = service.answer(evidence, _rule_review())

    assert {item["source_id"] for item in answer["citations"]} == {"RULE_RESULT"}
    assert "untrusted.example.test" not in json.dumps(answer)
    GroundedRagAnswer.model_validate(answer)


def test_llm_mode_accepts_only_provided_citation_ids() -> None:
    captured: dict[str, Any] = {}

    def requester(
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]:
        captured.update(url=url, payload=payload, headers=headers, timeout=timeout)
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "statements": [
                                    {
                                        "text": "CAMEO 스크리닝 등급은 높음입니다.",
                                        "source_ids": ["RULE_RESULT"],
                                    },
                                    {
                                        "text": "보호구 착용과 누출 구역 통제가 필요합니다.",
                                        "source_ids": ["KOSHA-7681-52-9-08"],
                                    },
                                ]
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

    service = GroundedRagService(
        RagConfig(
            mode="llm",
            base_url="https://llm.example.test/v1",
            model="small-grounded-model",
            api_key="secret-value",
            timeout_seconds=5,
        ),
        requester=requester,
    )

    answer = service.answer(_evidence(), _rule_review())

    assert answer["status"] == "COMPLETED"
    assert answer["used_llm"] is True
    assert answer["model"] == "small-grounded-model"
    assert captured["url"] == "https://llm.example.test/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret-value"
    assert captured["timeout"] == 5
    assert captured["payload"]["temperature"] == 0
    assert {item["source_id"] for item in answer["citations"]} == {
        "RULE_RESULT",
        "KOSHA-7681-52-9-08",
    }
    GroundedRagAnswer.model_validate(answer)


@pytest.mark.parametrize(
    "content",
    [
        {
            "statements": [
                {"text": "출처가 없는 주장", "source_ids": ["MADE_UP_SOURCE"]}
            ]
        },
        {
            "statements": [
                {"text": "위험등급은 낮음입니다.", "source_ids": ["RULE_RESULT"]}
            ]
        },
        {
            "statements": [
                {
                    "text": "충돌 위험은 낮습니다.",
                    "source_ids": ["RULE_RESULT"],
                }
            ]
        },
        {
            "statements": [
                {
                    "text": "현재 상태는 안전합니다.",
                    "source_ids": ["RULE_RESULT"],
                }
            ]
        },
    ],
)
def test_invalid_llm_output_falls_back_without_breaking_analysis(
    content: dict[str, Any],
) -> None:
    service = GroundedRagService(
        RagConfig(mode="llm", model="test-model"),
        requester=lambda *_args, **_kwargs: {
            "choices": [{"message": {"content": json.dumps(content)}}]
        },
    )

    answer = service.answer(_evidence(), _rule_review())

    assert answer["status"] == "FALLBACK_EXTRACTIVE"
    assert answer["fallback_reason"] == "LLM_REQUEST_OR_OUTPUT_FAILED"
    assert answer["used_llm"] is False
    GroundedRagAnswer.model_validate(answer)


def test_llm_timeout_falls_back_without_exposing_exception() -> None:
    def timeout(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise TimeoutError("internal host and secret must not be exposed")

    service = GroundedRagService(
        RagConfig(mode="llm", model="test-model"), requester=timeout
    )

    answer = service.answer(_evidence(), _rule_review())

    assert answer["status"] == "FALLBACK_EXTRACTIVE"
    assert answer["fallback_reason"] == "LLM_REQUEST_OR_OUTPUT_FAILED"
    assert "internal host" not in json.dumps(answer, ensure_ascii=False)
    GroundedRagAnswer.model_validate(answer)


def test_invalid_environment_configuration_uses_safe_extractive_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHEMIGUARD119_RAG_MODE", "unknown-mode")
    monkeypatch.setenv("CHEMIGUARD119_RAG_TIMEOUT_SECONDS", "999")

    config = RagConfig.from_env()

    assert config.mode == "extractive"
    assert config.timeout_seconds == 8.0
    assert config.configuration_error == "UNSUPPORTED_RAG_MODE"


def test_metadata_never_exposes_endpoint_or_api_key() -> None:
    service = GroundedRagService(
        RagConfig(
            mode="llm",
            base_url="https://private-llm.example.test/v1",
            model="small-grounded-model",
            api_key="top-secret",
        )
    )

    metadata = service.metadata()
    serialized = json.dumps(metadata)

    assert metadata["llm_configured"] is True
    assert metadata["model"] == "small-grounded-model"
    assert "private-llm" not in serialized
    assert "top-secret" not in serialized
