"""명세서·합성 요청 검증. 모델 성능이나 현장 안전성 평가가 아니다."""

import json
import re

import pytest
from pydantic import ValidationError

from chemiguard119 import action_models, agent_loop, api_models
from scripts.contracts import export_postman as exporter

SPEC = json.loads(exporter.CONTRACT.read_text())
COLLECTION = exporter.build_collection()
ITEMS = [item for group in COLLECTION["item"] for item in group["item"]]


def test_collection_is_reproducible_and_credentials_are_empty():
    assert exporter.OUTPUT.read_text() == exporter.serialize()
    variables = {item["key"]: item["value"] for item in COLLECTION["variable"]}
    assert variables == {
        "base_url": "http://127.0.0.1:8087",
        "api_key": "",
        "id_token": "",
    }
    assert COLLECTION["auth"]["type"] == "apikey"
    for item in ITEMS:
        auth = next(
            header
            for header in item["request"]["header"]
            if header["key"] == "Authorization"
        )
        assert auth["disabled"] is True
        assert auth["value"] == "Bearer {{id_token}}"
        assert all(event["listen"] == "test" for event in item["event"])


def test_every_deployed_operation_is_in_spec_and_collection():
    expected = {
        (method.upper(), path)
        for path, methods in SPEC["paths"].items()
        for method in methods
        if method in {"get", "post"}
    }
    actual = {
        (item["request"]["method"], item["request"]["url"].removeprefix("{{base_url}}"))
        for item in ITEMS
    }
    assert actual == expected
    document = (exporter.ROOT / "docs/API.md").read_text()
    documented = set(re.findall(r"\| `(GET|POST)` \| `([^`]+)` \|", document))
    assert documented == expected


@pytest.mark.parametrize("item", ITEMS, ids=[item["name"] for item in ITEMS])
def test_postman_requests_match_real_validators(item):
    request = item["request"]
    path = request["url"].removeprefix("{{base_url}}")
    if request["method"] == "GET":
        assert "body" not in request
        assert request["auth"]["type"] == "noauth"
        return
    operation = SPEC["paths"][path]["post"]
    model_name = operation["requestBody"]["content"]["application/json"]["schema"][
        "$ref"
    ].rsplit("/", 1)[-1]
    model = next(
        getattr(module, model_name)
        for module in (api_models, action_models, agent_loop)
        if hasattr(module, model_name)
    )
    payload = json.loads(request["body"]["raw"])
    if "CAS checksum 오류" in item["name"]:
        with pytest.raises(ValidationError) as error:
            model.model_validate(payload)
        assert any(detail["loc"][-1] == "cas_number" for detail in error.value.errors())
    else:
        model.model_validate(payload)


def test_brief_cli_example_and_response_projection_match_contract():
    document = (exporter.ROOT / "docs/API.md").read_text()
    raw = re.search(r"--data '(\{\"revision\"[^\n]+)'", document).group(1)
    request = action_models.BriefRequest.model_validate_json(raw)
    assert request.analysis.incident_id == "INC-SYNTHETIC-1"
    assert request.analysis.input.type.value == "VOICE_TRANSCRIPT"
    block = document.split("#### 응답 · BriefResponse", 1)[1].split(
        "#### 카드 · ActionCard", 1
    )[0]
    for field in action_models.BriefResponse.model_fields:
        assert f"`{field}`" in block
    block = document.split("#### 카드 · ActionCard", 1)[1].split(
        "#### 출처 · BriefSource", 1
    )[0]
    for field in action_models.ActionCard.model_fields:
        assert f"`{field}`" in block


def test_handoff_links_and_private_public_separation():
    root = exporter.ROOT
    page = (root / "docs/public-swagger/index.html").read_text()
    assert (
        "https://github.com/chemicheck119-lab/analysis-engine/blob/main/docs/API.md"
        in page
    )
    document = (root / "docs/API.md").read_text()
    for required in (
        "roles/run.invoker",
        "X-API-Key",
        "Last-Event-ID",
        "BRIEF_CAPACITY_EXCEEDED",
        "DRAFT_NOT_EXPERT_REVIEWED",
        "EXACT_CAS_LINK_FALLBACK_NOT_SECTION_RELEVANCE",
        "Postman GUI에서 모든 요청을 실행했다고 주장하지 않습니다.",
    ):
        assert required in document
    assert str(exporter.OUTPUT.relative_to(root)) in document
