"""사전 작성 합성 설계 회귀. AI DRAFT이며 사람 검수/비공개 테스트가 아니다."""

from __future__ import annotations


def cases() -> list[dict]:
    result = []

    def add(identity, family, text, expected):
        occurrences = {}
        mentions = []
        for surface, assertion, role in expected:
            start = text.index(surface, occurrences.get(surface, 0))
            end = start + len(surface)
            occurrences[surface] = end
            mentions.append(
                {
                    "surface_text": surface,
                    "start": start,
                    "end": end,
                    "assertion": assertion,
                    "role": role,
                }
            )
        result.append(
            {
                "id": identity,
                "family": family,
                "text": text,
                "expected": mentions,
                "label_status": "AI_DRAFT_DESIGN_REGRESSION",
            }
        )

    templates = [
        (
            "possible_copula",
            "{m}인 것 같습니다. 아직 확인하지 못했습니다.",
            "POSSIBLE",
            "UNKNOWN",
        ),
        ("unconfirmed_question", "{m}인지 확인 못 했습니다.", "UNCONFIRMED", "UNKNOWN"),
        ("unconfirmed_label", "{m} 미확인", "UNCONFIRMED", "UNKNOWN"),
        ("negated", "{m}은 아닙니다.", "NEGATED", "NEGATED"),
        ("absent", "{m}은 없습니다.", "NEGATED", "NEGATED"),
        ("incident", "{m}이 누출됩니다.", "AFFIRMED", "INCIDENT"),
        ("facility", "옆 탱크에는 {m}이 있습니다.", "AFFIRMED", "FACILITY"),
        ("possible_leak", "{m}이 누출되는 것 같습니다.", "POSSIBLE", "INCIDENT"),
        ("copula_reported", "라벨에 {m}이라고 적혀 있습니다.", "AFFIRMED", "UNKNOWN"),
        ("unknown_identity", "{m}인지 모르겠습니다.", "UNCONFIRMED", "UNKNOWN"),
    ]
    for index, material in enumerate(("염산", "질산", "암모니아")):
        for family, template, assertion, role in templates:
            add(
                f"{family}-{index}",
                family,
                template.format(m=material),
                [(material, assertion, role)],
            )
    for index, text in enumerate(
        (
            "염산염 누출",
            "염산성 세척제 누출",
            "염산인형 제품",
            "염산이라고표시된제품명",
            "질산염 누출",
            "암모니아성 세척제",
            "염소소독제 냄새",
            "나 트륨 누출",
            "미등록제품XYZ 누출",
        )
    ):
        add(f"negative-{index}", "embedded_product_negative", text, [])
    add(
        "same_cas_sentences",
        "repeat_sentence",
        "염산은 없습니다. 염산이 누출됩니다.",
        [("염산", "NEGATED", "NEGATED"), ("염산", "AFFIRMED", "INCIDENT")],
    )
    add(
        "same_cas_clause",
        "repeat_clause",
        "염산은 없다고 했지만 라벨에는 염산이라고 적혀 있습니다.",
        [("염산", "NEGATED", "NEGATED"), ("염산", "AFFIRMED", "UNKNOWN")],
    )
    add(
        "different_cas_scope",
        "cross_sentence_scope",
        "염산은 없습니다. 질산이 누출됩니다.",
        [("염산", "NEGATED", "NEGATED"), ("질산", "AFFIRMED", "INCIDENT")],
    )
    add(
        "same_cas_roles",
        "repeat_roles",
        "염산이 누출됩니다. 옆 창고에 염산이 있습니다.",
        [("염산", "AFFIRMED", "INCIDENT"), ("염산", "AFFIRMED", "FACILITY")],
    )
    add(
        "negation_other_material",
        "cross_clause_scope",
        "염산은 없으며, 질산이 누출됩니다.",
        [("염산", "NEGATED", "NEGATED"), ("질산", "AFFIRMED", "INCIDENT")],
    )
    add(
        "nested_material",
        "longest_span",
        "차아염소산나트륨 탱크에서 누출됩니다.",
        [("차아염소산나트륨", "AFFIRMED", "INCIDENT")],
    )
    add(
        "spaced_material",
        "asr_spacing",
        "차아 염소산 나트륨 탱크에서 누출됩니다.",
        [("차아 염소산 나트륨", "AFFIRMED", "INCIDENT")],
    )
    add(
        "possible_vs_known",
        "repeated_modality",
        "염산인 것 같습니다. 옆 창고에 염산이 있습니다.",
        [("염산", "POSSIBLE", "UNKNOWN"), ("염산", "AFFIRMED", "FACILITY")],
    )
    return result
