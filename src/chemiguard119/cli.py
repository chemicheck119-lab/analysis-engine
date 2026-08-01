"""케미체크119 데이터·모델 파이프라인과 운영 전 검증용 통합 CLI.

이 모듈은 현장 명령을 내리는 도구가 아니다. LLM은 신고문을 구조화하는 선택적
보조 수단으로만 사용하고, 화학물질쌍 결과는 버전이 있는 Rule Engine에서만
가져온다. 모든 결과는 대원 확인과 현장 지휘관 판단을 전제로 한다.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
import platform
import re
import shlex
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Sequence

from chemiguard119 import __version__
from chemiguard119.paths import (
    CONFIG_DIR,
    DEFAULT_ARTIFACT_DIR,
    DEFAULT_DB_PATH,
    DEFAULT_REPORT_DIR,
    DEFAULT_RESOLVER_MODEL,
    DEFAULT_RETRIEVER_MODEL,
    EVALUATION_DIR,
    FINAL_DATA_DIR,
)


SAFETY_NOTICE = (
    "이 결과는 화학사고 의사결정 보조 정보이며 현장 명령이 아닙니다. "
    "물질·시설 상태를 대원이 확인하고 최종 결정은 현장 지휘관이 수행합니다."
)


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"JSON으로 변환할 수 없는 값입니다: {type(value).__name__}")


def _with_safety(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.setdefault("safety_notice", SAFETY_NOTICE)
    return result


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )


def _short(value: Any, limit: int = 180) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "예" if value else "아니요"
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _print_candidates(payload: dict[str, Any]) -> None:
    print(f"상태: {_short(payload.get('status'))}")
    print(f"확인 필요: {_short(payload.get('requires_responder_confirmation'))}")
    candidates = payload.get("candidates") or []
    if not candidates:
        print("후보: 없음")
        return
    print("후보:")
    for index, item in enumerate(candidates, 1):
        print(
            f"  {index}. CAS {item.get('cas_number', '-')} | "
            f"일치 표현 {_short(item.get('matched_alias'))} | "
            f"점수 {item.get('score', '-')} | {item.get('match_type', '-')}"
        )


def _print_human(command: str, payload: dict[str, Any]) -> None:
    titles = {
        "doctor": "환경 진단",
        "coverage": "전국 시설 이력 범위",
        "audit": "데이터 점검",
        "prepare": "데이터 전처리",
        "train": "기준선 모델 학습",
        "evaluate": "모델 평가",
        "evaluate-e2e": "사고 분석 E2E 안전 평가",
        "e2e-review": "E2E 독립 검수팩",
        "resolve": "물질 후보 검색",
        "search": "공식 근거 검색",
        "parse": "신고문 구조화",
        "incident": "사고 전체 분석",
        "review": "대응 충돌 검토",
        "finetune-check": "파인튜닝 준비도 점검",
        "pipeline": "전체 모델링 파이프라인",
        "release-manifest": "배포 무결성 Manifest",
        "interactive": "대화형 CLI",
        "error": "실행 오류",
    }
    print(f"\n[케미체크119] {titles.get(command, command)}")

    if command == "doctor":
        print(f"상태: {_short(payload.get('status'))}")
        print(
            f"Python: {_short(payload.get('python_version'))} "
            f"(요구 {payload.get('required_python', '>=3.11')}, "
            f"지원 {'예' if payload.get('python_supported') else '아니요'})"
        )
        print(
            f"필수 원천 CSV: {payload.get('final_csv_count', 0)}/"
            f"{payload.get('required_csv_count', 8)}"
        )
        for name, info in (payload.get("artifacts") or {}).items():
            print(
                f"- {name}: {'있음' if info.get('exists') else '없음'} ({info.get('path')})"
            )
        if payload.get("lmstudio"):
            lmstudio = payload["lmstudio"]
            print(
                f"LM Studio: {lmstudio.get('status')} / 모델 {lmstudio.get('model_count', 0)}개"
            )
    elif command == "coverage":
        print(f"범위: {_short(payload.get('scope'))}")
        print(f"시·도: {payload.get('covered_province_count', 0)}개")
        print(f"시설: {payload.get('distinct_facility_count', 0):,}개")
        print(f"후보 행: {payload.get('candidate_row_count', 0):,}개")
        print(f"CAS: {payload.get('distinct_cas_count', 0):,}개")
        print("의미: 과거 공개 이력 후보이며 현재 재고 확정 정보가 아닙니다.")
    elif command == "audit":
        print(f"파일: {payload.get('file_count', 0)}개")
        print(f"행: {payload.get('total_rows', 0):,}개")
        print(f"열 수 불일치 행: {payload.get('malformed_row_count', 0):,}개")
        print(f"첫 키 중복 행: {payload.get('duplicate_first_key_row_count', 0):,}개")
        decision = payload.get("modeling_decision") or {}
        for title, key in (
            ("지금 학습", "train_now"),
            ("결정 규칙", "deterministic_only"),
            ("학습 금지", "do_not_train"),
        ):
            print(f"{title}:")
            for item in decision.get(key, []):
                print(f"  - {item}")
    elif command == "resolve":
        _print_candidates(payload)
    elif command == "discover":
        print(f"질의: {_short(payload.get('query'))}")
        print(f"상태: {_short(payload.get('status'))}")
        print(f"검색 방식: {_short(payload.get('search_mode'))}")
        candidates = payload.get("candidates") or []
        print(f"현장 확인 전 후보: {len(candidates)}개")
        for item in candidates:
            print(
                f"  {item.get('rank', '-')}. {_short(item.get('display_name'))} "
                f"| CAS {_short(item.get('cas_number'))}"
            )
            matched = item.get("matched_properties") or []
            if matched:
                print(
                    "     일치 관찰: "
                    + ", ".join(
                        f"{match.get('label')}: {_short(match.get('value'), 80)}"
                        for match in matched
                    )
                )
            print(
                f"     공식 근거 카드: {len(item.get('evidence') or [])}개 / "
                f"{_short(item.get('evidence_status'))}"
            )
        print(f"주의: {_short(payload.get('notice'), 500)}")
    elif command == "search":
        print(f"질의: {_short(payload.get('query'))}")
        print(f"CAS 힌트: {_short(payload.get('cas_hint'))}")
        results = payload.get("results") or []
        print(f"검색 결과: {len(results)}개")
        for index, item in enumerate(results, 1):
            print(
                f"  {index}. [{item.get('source', '-')}] {_short(item.get('title'))} "
                f"| CAS {_short(item.get('cas_number'))} | RRF {item.get('rrf_score', '-')}"
            )
            print(f"     {_short(item.get('body_preview'), 220)}")
            if item.get("source_url"):
                print(f"     출처: {item['source_url']}")
    elif command == "parse":
        print(f"백엔드: {_short(payload.get('backend'))}")
        print(f"사고유형: {', '.join(payload.get('incident_types') or ['-'])}")
        print(f"화재 상태: {_short(payload.get('fire_status'))}")
        mentions = payload.get("substance_mentions") or []
        print(f"원문 물질 표현: {len(mentions)}개")
        for item in mentions:
            print(
                f"  - {_short(item.get('surface_text'))} | 역할 {item.get('role', '-')} "
                f"| 진술 {item.get('assertion', '-')}"
            )
        print(f"물질 확인 필요: {_short(payload.get('needs_substance_confirmation'))}")
    elif command == "incident":
        print(f"상태: {_short(payload.get('status'))}")
        parsed = payload.get("parsed_report") or {}
        print(f"사고유형: {', '.join(parsed.get('incident_types') or ['-'])}")
        candidates = payload.get("substance_candidates") or []
        print(f"물질 후보: {len(candidates)}개")
        for item in candidates:
            print(
                f"  - {_short(item.get('surface_text'))} | 역할 {item.get('role', '-')} "
                f"| 현장확인 {'필요' if item.get('requires_responder_confirmation') else '완료'}"
            )
        review = payload.get("rule_review") or {}
        print(f"충돌 검토: {_short(review.get('status'))}")
        if not review.get("executed"):
            print("  두 물질의 확인된 CAS가 모두 있어야 충돌 규칙을 실행합니다.")
        validation = payload.get("output_validation") or {}
        print(f"출력 안전검증: {_short(validation.get('status'))}")
    elif command == "review":
        review = payload
        if review:
            print(f"상태: {_short(review.get('status'))}")
            print(f"충돌 수준: {_short(review.get('severity'))}")
            print(f"Rule ID: {_short(review.get('rule_id'))}")
            print(f"위험 설명: {_short(review.get('brief_text'), 500)}")
            checks = review.get("required_checks") or []
            if checks:
                print("우선 확인:")
                for item in checks:
                    print(f"  - {item}")
            if review.get("reason"):
                print(f"이유: {review['reason']}")
            print(
                f"최종 결정: {_short(review.get('final_decision') or '현장 지휘관 판단')}"
            )
        elif payload.get("status"):
            print(f"상태: {payload['status']}")
            print(f"이유: {_short(payload.get('reason'))}")
            if payload.get("required_flag"):
                print(f"필요 옵션: {payload['required_flag']}")
    elif command == "train":
        for name in ("resolver", "retriever"):
            if payload.get(name):
                item = payload[name]
                print(
                    f"{name}: {item.get('model_path')} / 피처 {item.get('feature_count', '-')}"
                )
    elif command == "evaluate":
        resolver = payload.get("resolver")
        resolver_safety = payload.get("resolver_hint_safety")
        retriever = payload.get("retriever")
        if resolver:
            print(
                "Resolver: "
                f"Top-1 {resolver.get('top1_accuracy', 0):.3f}, "
                f"Top-3 {resolver.get('top3_recall', 0):.3f}, MRR {resolver.get('mrr', 0):.3f}"
            )
        if resolver_safety:
            print(
                "Resolver 자동 CAS 힌트 안전성: "
                f"통과율 {resolver_safety.get('safety_pass_rate', 0):.3f}, "
                "위험 힌트 "
                f"{resolver_safety.get('unsafe_auto_hint_count', 0)}건, "
                "Resolver Rule 입력 승인 위반 "
                f"{resolver_safety.get('resolver_rule_eligibility_violation_count', 0)}건"
            )
        if retriever:
            end_to_end = retriever.get("end_to_end") or retriever
            oracle = retriever.get("retriever_with_oracle_cas") or {}
            print(
                "Retriever 전체 흐름: "
                f"Recall@5 {end_to_end.get('recall_at_5', 0):.3f}, "
                f"Recall@8 {end_to_end.get('recall_at_8', 0):.3f}, "
                f"MRR@8 {end_to_end.get('mrr_at_8', 0):.3f}"
            )
            if oracle:
                print(
                    "Retriever 단독(정답 CAS 제공): "
                    f"Recall@5 {oracle.get('recall_at_5', 0):.3f}, "
                    f"Recall@8 {oracle.get('recall_at_8', 0):.3f}, "
                    f"MRR@8 {oracle.get('mrr_at_8', 0):.3f}"
                )
        print("주의: 내부 회귀 평가셋이며 현장 성능 주장에 사용할 수 없습니다.")
    elif command == "evaluate-e2e":
        metrics = payload.get("metrics") or {}
        latency = metrics.get("latency_ms") or {}
        print(f"시나리오: {payload.get('case_count', 0)}건")
        print(
            "통과: "
            f"{payload.get('passed_case_count', 0)}/{payload.get('case_count', 0)} "
            f"({metrics.get('scenario_pass_rate', 0):.3f})"
        )
        print(
            "안전 위반: "
            f"미확인 Rule 실행 {metrics.get('unsafe_conflict_execution_count', 0)}건, "
            f"미확인 위험 노출 {metrics.get('unconfirmed_risk_exposure_count', 0)}건"
        )
        print(
            "처리시간: "
            f"평균 {latency.get('mean', 0):.3f}ms, "
            f"p95 {latency.get('p95', 0):.3f}ms"
        )
        print(f"주장 범위: {_short(payload.get('claim_scope'))}")
        print("주의: DRAFT E2E 회귀 결과는 현장 정확도나 상용 성능이 아닙니다.")
    elif command == "e2e-review":
        print(f"작업: {_short(payload.get('action'))}")
        print(f"상태: {_short(payload.get('status'))}")
        count = payload.get("candidate_count", payload.get("case_count"))
        if count is not None:
            print(f"사례: {count}건")
        if payload.get("merged_case_count") is not None:
            print(f"병합된 검수 사례: {payload.get('merged_case_count')}건")
        if payload.get("disagreement_count") is not None:
            print(f"검수 불일치: {payload.get('disagreement_count')}건")
        if payload.get("output_path"):
            print(f"출력: {payload['output_path']}")
        if payload.get("warning"):
            print(f"주의: {_short(payload.get('warning'), 500)}")
        for blocker in payload.get("blockers") or []:
            print(f"차단: {_short(blocker, 500)}")
    elif command == "pipeline":
        print(f"상태: {_short(payload.get('status'))}")
        print(f"마지막 단계: {_short(payload.get('last_completed_stage'))}")
        print(f"보고서: {_short(payload.get('report_path'))}")
        if payload.get("error"):
            print(f"오류: {payload['error']}")
    elif command == "release-manifest":
        print(f"Manifest: {_short(payload.get('manifest_path'))}")
        print(f"SHA-256: {_short(payload.get('manifest_sha256'))}")
        print("운영 배포에는 이 SHA-256을 신뢰된 환경변수로 주입해야 합니다.")
    else:
        # 전처리·파인튜닝 점검처럼 구현 모듈에 따라 필드가 달라지는 결과도
        # 최소한 읽을 수 있도록 한국어 제목 아래 구조를 보존해 표시한다.
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))

    print(f"\n안전 안내: {payload.get('safety_notice', SAFETY_NOTICE)}")


def _emit(command: str, payload: dict[str, Any], as_json: bool) -> None:
    safe_payload = _with_safety(payload)
    if as_json:
        print(
            json.dumps(
                safe_payload, ensure_ascii=False, indent=2, default=_json_default
            )
        )
    else:
        _print_human(command, safe_payload)


def _load_optional_callable(
    module_name: str,
    candidate_names: Sequence[str],
    command: str,
) -> Callable[..., Any]:
    """선택 모듈을 해당 명령 실행 시에만 불러온다."""

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            names = ", ".join(candidate_names)
            raise RuntimeError(
                f"`{command}` 명령에 필요한 {module_name}.py가 없습니다. "
                f"모듈을 추가하고 다음 함수 중 하나를 구현하세요: {names}"
            ) from exc
        raise RuntimeError(
            f"`{command}` 모듈의 선택 의존성이 없습니다: {exc.name}. "
            "pyproject 선택 의존성을 설치한 뒤 다시 실행하세요."
        ) from exc
    for name in candidate_names:
        candidate = getattr(module, name, None)
        if callable(candidate):
            return candidate
    raise RuntimeError(
        f"{module_name}에 `{command}`용 함수가 없습니다. "
        f"지원 함수명: {', '.join(candidate_names)}"
    )


def _call_supported(function: Callable[..., Any], **kwargs: Any) -> Any:
    """선택 모듈의 작고 명시적인 시그니처 차이를 흡수한다."""

    signature = inspect.signature(function)
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    supported = (
        kwargs
        if accepts_kwargs
        else {
            key: value for key, value in kwargs.items() if key in signature.parameters
        }
    )
    try:
        return function(**supported)
    except TypeError as exc:
        missing = [
            name
            for name, parameter in signature.parameters.items()
            if parameter.default is inspect.Parameter.empty
            and parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
            and name not in supported
        ]
        if missing:
            raise RuntimeError(
                f"선택 모듈 함수 `{function.__name__}`의 필수 인자를 연결하지 못했습니다: {missing}"
            ) from exc
        raise


def _prepare(
    data_dir: Path,
    db_path: Path,
    config_dir: Path = CONFIG_DIR,
) -> dict[str, Any]:
    function = _load_optional_callable(
        "chemiguard119.preprocessing",
        ("prepare_database", "build_database", "prepare_dataset", "prepare_data"),
        "prepare",
    )
    signature = inspect.signature(function)
    aliases: dict[str, Any] = {
        "data_dir": data_dir,
        "final_data_dir": data_dir,
        "source_dir": data_dir,
        "config_dir": config_dir,
        "artifact_dir": db_path.parent,
        "artifacts_dir": db_path.parent,
        "db_path": db_path,
        "database_path": db_path,
        "output_path": db_path,
    }
    kwargs = {name: aliases[name] for name in signature.parameters if name in aliases}
    result = _call_supported(function, **kwargs)
    if result is None:
        return {"status": "COMPLETED", "database_path": str(db_path)}
    if isinstance(result, dict):
        return result
    return {"status": "COMPLETED", "database_path": str(db_path), "result": str(result)}


def _doctor(args: argparse.Namespace) -> dict[str, Any]:
    from chemiguard119.audit import final_csv_paths
    from chemiguard119.preprocessing import SOURCE_FILES
    from chemiguard119.utils import is_lfs_pointer

    data_paths = final_csv_paths(args.data_dir)
    pointers = [str(path) for path in data_paths if is_lfs_pointer(path)]
    packages: dict[str, str | None] = {}
    for package in ("numpy", "scikit-learn", "joblib"):
        try:
            packages[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            packages[package] = None
    artifacts = {
        "database": {"path": str(args.db), "exists": args.db.is_file()},
        "resolver_model": {
            "path": str(args.resolver_model),
            "exists": args.resolver_model.is_file(),
        },
        "retriever_model": {
            "path": str(args.retriever_model),
            "exists": args.retriever_model.is_file(),
        },
        "runtime_manifest": {
            "path": str(args.db.parent / "runtime_manifest.json"),
            "exists": (args.db.parent / "runtime_manifest.json").is_file(),
        },
    }
    python_supported = sys.version_info >= (3, 11)
    payload: dict[str, Any] = {
        "status": (
            "READY_FOR_PIPELINE"
            if len(data_paths) == len(SOURCE_FILES)
            and not pointers
            and python_supported
            else "NEEDS_SETUP"
        ),
        "chemiguard119_version": __version__,
        "python_version": platform.python_version(),
        "python_supported": python_supported,
        "required_python": ">=3.11",
        "platform": platform.platform(),
        "packages": packages,
        "data_dir": str(args.data_dir),
        "final_csv_count": len(data_paths),
        "required_csv_count": len(SOURCE_FILES),
        "lfs_pointer_count": len(pointers),
        "lfs_pointers": pointers,
        "artifacts": artifacts,
    }
    if args.check_lmstudio:
        try:
            from chemiguard119.lmstudio import list_models

            models = list_models(args.base_url, timeout=args.timeout)
            payload["lmstudio"] = {
                "status": "AVAILABLE",
                "base_url": args.base_url,
                "model_count": len(models),
                "models": [item.get("id") for item in models],
            }
        except Exception as exc:  # 로컬 서버 미실행도 진단 결과로 반환한다.
            payload["lmstudio"] = {
                "status": "UNAVAILABLE",
                "base_url": args.base_url,
                "error": str(exc),
            }
    return payload


def _coverage(args: argparse.Namespace) -> dict[str, Any]:
    from chemiguard119.coverage import facility_history_coverage

    return facility_history_coverage(args.db)


def _audit(args: argparse.Namespace) -> dict[str, Any]:
    from chemiguard119.audit import audit_dataset

    result = audit_dataset(args.data_dir, include_hash=args.include_hash)
    if args.report:
        _write_json(args.report, _with_safety(result))
        result["report_path"] = str(args.report)
    return result


def _prepare_command(args: argparse.Namespace) -> dict[str, Any]:
    return _prepare(args.data_dir, args.db, args.config_dir)


def _train(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "COMPLETED", "database_path": str(args.db)}
    if args.only in {"all", "resolver"}:
        from chemiguard119.resolver import train_resolver

        payload["resolver"] = train_resolver(args.db, args.resolver_model)
    if args.only in {"all", "retriever"}:
        from chemiguard119.retrieval import train_retriever

        payload["retriever"] = train_retriever(
            args.db,
            args.retriever_model,
            max_features_per_branch=args.max_features,
        )
    return payload


def _evaluate(args: argparse.Namespace) -> dict[str, Any]:
    from chemiguard119.evaluation_contract import audit_evaluation_dataset

    args.report_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "status": "COMPLETED",
        "evaluation_warning": "내부 회귀셋이며 현장 성능 주장 금지",
        "evaluation_profile": args.evaluation_profile,
        "evaluation_contracts": {},
    }
    contract_paths = {
        "resolver": args.resolver_evaluation,
        "resolver_hint_safety": args.resolver_safety_evaluation,
        "retriever_legacy": args.retriever_evaluation,
        "retriever_sections": args.retriever_section_evaluation,
    }
    payload["evaluation_contracts"] = {
        name: audit_evaluation_dataset(path, args.evaluation_profile)
        for name, path in contract_paths.items()
    }
    blocked_contracts = [
        name
        for name, report in payload["evaluation_contracts"].items()
        if not report["passed"]
    ]
    if blocked_contracts:
        payload["status"] = "BLOCKED_EVALUATION_GATE"
        payload["blocked_contracts"] = blocked_contracts
        return payload
    if args.only in {"all", "resolver"}:
        from chemiguard119.resolver import (
            evaluate_resolver,
            evaluate_resolver_hint_safety,
        )

        resolver_report = args.report_dir / "resolver_evaluation.json"
        resolver_safety_report = (
            args.report_dir / "resolver_hint_safety_evaluation.json"
        )
        payload["resolver"] = evaluate_resolver(
            args.resolver_model,
            args.resolver_evaluation,
            resolver_report,
        )
        payload["resolver_hint_safety"] = evaluate_resolver_hint_safety(
            args.resolver_model,
            args.resolver_safety_evaluation,
            resolver_safety_report,
        )
        if not payload["resolver_hint_safety"]["deployment_gate"]["passed"]:
            payload["status"] = "BLOCKED_SAFETY_GATE"
        payload["resolver_report_path"] = str(resolver_report)
        payload["resolver_safety_report_path"] = str(resolver_safety_report)
    if args.only in {"all", "retriever"}:
        from chemiguard119.retrieval import evaluate_retriever
        from chemiguard119.retrieval_evaluation import evaluate_retriever_sections

        retriever_report = args.report_dir / "retriever_evaluation.json"
        section_report = args.report_dir / "retriever_section_evaluation.json"
        payload["retriever"] = evaluate_retriever(
            args.db,
            args.retriever_model,
            args.resolver_model,
            args.retriever_evaluation,
            retriever_report,
        )
        payload["retriever_sections"] = evaluate_retriever_sections(
            args.db,
            args.retriever_model,
            args.retriever_section_evaluation,
            profile=args.evaluation_profile,
            report_path=section_report,
        )
        payload["retriever_report_path"] = str(retriever_report)
        payload["retriever_section_report_path"] = str(section_report)
    return payload


def _evaluate_e2e(args: argparse.Namespace) -> dict[str, Any]:
    from chemiguard119.e2e_evaluation import evaluate_incident_scenarios

    return evaluate_incident_scenarios(
        args.db,
        args.resolver_model,
        args.retriever_model,
        args.evaluation,
        config_dir=args.config_dir,
        profile=args.evaluation_profile,
        report_path=args.report,
    )


def _e2e_review(args: argparse.Namespace) -> dict[str, Any]:
    from chemiguard119.e2e_review import (
        export_review_sheet,
        generate_review_candidate_pool,
        merge_review_sheets,
        preflight_candidate_pool,
    )

    if args.e2e_review_action == "generate":
        return generate_review_candidate_pool(args.pair_snapshot, args.output)
    if args.e2e_review_action == "export":
        return export_review_sheet(
            args.candidates,
            args.output,
            actor_role=args.actor_role,
            actor_id=args.actor_id,
        )
    if args.e2e_review_action == "merge":
        return merge_review_sheets(
            args.candidates,
            args.labeler_sheet,
            args.reviewer_sheet,
            args.output,
            report_path=args.report,
        )
    if args.e2e_review_action == "preflight":
        return preflight_candidate_pool(
            args.candidates,
            args.db,
            args.resolver_model,
            args.retriever_model,
            config_dir=args.config_dir,
            report_path=args.report,
        )
    raise ValueError(f"지원하지 않는 e2e-review action={args.e2e_review_action!r}")


def _resolve(args: argparse.Namespace) -> dict[str, Any]:
    from chemiguard119.resolver import load_resolver, resolve_substance

    artifact = load_resolver(args.resolver_model)
    return resolve_substance(args.query, artifact, args.top_k, args.minimum_score)


def _discover(args: argparse.Namespace) -> dict[str, Any]:
    from chemiguard119.discovery import discover_substances
    from chemiguard119.resolver import load_resolver
    from chemiguard119.retrieval import load_retriever

    return discover_substances(
        args.query,
        db_path=args.db,
        resolver_artifact=load_resolver(args.resolver_model),
        retriever_artifact=load_retriever(args.retriever_model),
        top_k=args.top_k,
        evidence_top_k=args.evidence_top_k,
    )


def _search(args: argparse.Namespace) -> dict[str, Any]:
    from chemiguard119.retrieval import load_retriever, search_evidence

    cas_hint = args.cas_hint
    resolution = None
    if not cas_hint and not args.no_resolve_hint and args.resolver_model.is_file():
        from chemiguard119.resolver import (
            load_resolver,
            resolve_substance,
            select_evidence_cas_hint_from_text,
        )

        resolver = load_resolver(args.resolver_model)
        resolution = resolve_substance(args.query, resolver, top_k=3)
        cas_hint = select_evidence_cas_hint_from_text(args.query, resolver)
    artifact = load_retriever(args.retriever_model)
    payload = search_evidence(args.query, args.db, artifact, cas_hint, args.top_k)
    if resolution:
        payload["resolver_hint"] = resolution
    return payload


def _parse(args: argparse.Namespace) -> dict[str, Any]:
    if args.backend == "lmstudio":
        if not args.model:
            raise ValueError(
                "LM Studio 백엔드는 `--model <로드된 모델 ID>`가 필요합니다."
            )
        from chemiguard119.lmstudio import parse_with_lmstudio

        return parse_with_lmstudio(args.text, args.model, args.base_url, args.timeout)

    from chemiguard119.incident import deterministic_parse, validate_parser_output
    from chemiguard119.resolver import load_resolver

    resolver = load_resolver(args.resolver_model)
    result = deterministic_parse(args.text, resolver)
    errors = validate_parser_output(result, args.text)
    if errors:
        return {
            "status": "OUTPUT_VALIDATION_FAILED",
            "backend": "DETERMINISTIC_BLOCKED",
            "errors": errors,
            "source_text": args.text,
        }
    return result


def _incident(args: argparse.Namespace) -> dict[str, Any]:
    """임의 신고문을 운영 API와 같은 핵심 파이프라인으로 실행한다."""

    from chemiguard119.pipeline import analyze_incident
    from chemiguard119.resolver import load_resolver
    from chemiguard119.retrieval import load_retriever

    resolver = load_resolver(args.resolver_model)
    retriever = load_retriever(args.retriever_model)
    return analyze_incident(
        args.text,
        db_path=args.db,
        resolver_artifact=resolver,
        retriever_artifact=retriever,
        confirmed_incident_cas=args.confirmed_incident_cas,
        confirmed_facility_cas=args.confirmed_facility_cas,
        planned_actions=args.action,
        allow_demo_rules=False,
        policy_mode=args.rule_policy,
        config_dir=args.config_dir,
        evidence_top_k=args.evidence_top_k,
    )


def _review(args: argparse.Namespace) -> dict[str, Any]:
    from chemiguard119.rules import review_pair, validate_review_output

    result = review_pair(
        args.incident_cas,
        args.facility_cas,
        args.db,
        planned_actions=args.action,
        allow_demo_rules=False,
        policy_mode=args.rule_policy,
        config_dir=args.config_dir,
    )
    errors = validate_review_output(result)
    if errors:
        return {
            "status": "OUTPUT_VALIDATION_FAILED",
            "errors": errors,
            "blocked_output": result,
            "human_confirmation_required": True,
        }
    return result


def _finetune_check(args: argparse.Namespace) -> dict[str, Any]:
    function = _load_optional_callable(
        "chemiguard119.finetune",
        (
            "check_finetune_readiness",
            "finetune_readiness",
            "finetune_check",
            "run_finetune_check",
        ),
        "finetune-check",
    )
    result = _call_supported(
        function,
        dataset_path=args.dataset_path,
        report_path=args.report,
        base_model=args.base_model,
        output_dir=args.output_dir,
        execute=False,
        dry_run=True,
    )
    if isinstance(result, dict):
        payload = result
    else:
        payload = {"status": "COMPLETED", "result": str(result)}
    payload.setdefault(
        "scope",
        "신고문 구조화 후보 실험만 허용; 위험등급·대응판단 파인튜닝 금지",
    )
    return payload


def _release_manifest(args: argparse.Namespace) -> dict[str, Any]:
    from chemiguard119.release import create_runtime_manifest

    evaluation_evidence: dict[str, dict[str, Path]] = {}
    for name in (
        "resolver",
        "resolver_hint_safety",
        "retriever_sections",
        "parser_locked",
        "e2e_scenarios",
    ):
        report_path = getattr(args, f"{name}_report", None)
        dataset_path = getattr(args, f"{name}_dataset", None)
        if report_path is not None or dataset_path is not None:
            if report_path is None or dataset_path is None:
                raise ValueError(
                    f"{name}: evaluator report와 dataset 경로를 함께 제공해야 합니다."
                )
            evaluation_evidence[name] = {
                "report_path": report_path,
                "dataset_path": dataset_path,
            }
    return create_runtime_manifest(
        db_path=args.db,
        resolver_model_path=args.resolver_model,
        retriever_model_path=args.retriever_model,
        config_dir=args.config_dir,
        output_path=args.output,
        git_commit=args.git_commit,
        evaluation_evidence=evaluation_evidence,
        release_attestation_path=args.release_attestation,
    )


def _pipeline(args: argparse.Namespace) -> dict[str, Any]:
    from chemiguard119.audit import audit_dataset
    from chemiguard119.e2e_evaluation import evaluate_incident_scenarios
    from chemiguard119.evaluation_contract import require_evaluation_dataset
    from chemiguard119.resolver import (
        evaluate_resolver,
        evaluate_resolver_hint_safety,
        train_resolver,
    )
    from chemiguard119.retrieval import evaluate_retriever, train_retriever

    args.report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = args.report_dir / f"pipeline_{timestamp}.json"
    latest_path = args.report_dir / "pipeline_latest.json"
    report: dict[str, Any] = {
        "status": "RUNNING",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "parameters": {
            "data_dir": str(args.data_dir),
            "db_path": str(args.db),
            "resolver_model": str(args.resolver_model),
            "retriever_model": str(args.retriever_model),
            "report_dir": str(args.report_dir),
            "config_dir": str(args.config_dir),
            "evaluation_profile": args.evaluation_profile,
        },
        "stages": {},
        "last_completed_stage": None,
        "report_path": str(report_path),
        "safety_notice": SAFETY_NOTICE,
    }
    try:
        report["stages"]["evaluation_contract"] = {
            "resolver": require_evaluation_dataset(
                args.resolver_evaluation, args.evaluation_profile
            ),
            "resolver_hint_safety": require_evaluation_dataset(
                args.resolver_safety_evaluation, args.evaluation_profile
            ),
            "retriever_legacy": require_evaluation_dataset(
                args.retriever_evaluation, args.evaluation_profile
            ),
            "retriever_sections": require_evaluation_dataset(
                args.retriever_section_evaluation, args.evaluation_profile
            ),
            "e2e_scenarios": require_evaluation_dataset(
                args.e2e_evaluation, args.evaluation_profile
            ),
        }
        report["last_completed_stage"] = "evaluation_contract"
        _write_json(report_path, report)

        report["stages"]["audit"] = audit_dataset(
            args.data_dir,
            include_hash=args.include_hash,
        )
        report["last_completed_stage"] = "audit"
        _write_json(report_path, report)

        report["stages"]["prepare"] = _prepare(args.data_dir, args.db, args.config_dir)
        report["last_completed_stage"] = "prepare"
        _write_json(report_path, report)

        report["stages"]["train_resolver"] = train_resolver(
            args.db, args.resolver_model
        )
        report["last_completed_stage"] = "train_resolver"
        _write_json(report_path, report)

        report["stages"]["train_retriever"] = train_retriever(
            args.db,
            args.retriever_model,
            max_features_per_branch=args.max_features,
        )
        report["last_completed_stage"] = "train_retriever"
        _write_json(report_path, report)

        resolver_eval_path = args.report_dir / "resolver_evaluation_latest.json"
        resolver_safety_eval_path = (
            args.report_dir / "resolver_hint_safety_evaluation_latest.json"
        )
        retriever_eval_path = args.report_dir / "retriever_evaluation_latest.json"
        retriever_section_eval_path = (
            args.report_dir / "retriever_section_evaluation_latest.json"
        )
        e2e_eval_path = args.report_dir / "e2e_scenario_evaluation_latest.json"
        from chemiguard119.retrieval_evaluation import evaluate_retriever_sections

        report["stages"]["evaluate"] = {
            "resolver": evaluate_resolver(
                args.resolver_model,
                args.resolver_evaluation,
                resolver_eval_path,
            ),
            "resolver_hint_safety": evaluate_resolver_hint_safety(
                args.resolver_model,
                args.resolver_safety_evaluation,
                resolver_safety_eval_path,
            ),
            "retriever": evaluate_retriever(
                args.db,
                args.retriever_model,
                args.resolver_model,
                args.retriever_evaluation,
                retriever_eval_path,
            ),
            "retriever_sections": evaluate_retriever_sections(
                args.db,
                args.retriever_model,
                args.retriever_section_evaluation,
                profile=args.evaluation_profile,
                report_path=retriever_section_eval_path,
            ),
            "e2e_scenarios": evaluate_incident_scenarios(
                args.db,
                args.resolver_model,
                args.retriever_model,
                args.e2e_evaluation,
                config_dir=args.config_dir,
                profile=args.evaluation_profile,
                report_path=e2e_eval_path,
            ),
        }
        if not report["stages"]["evaluate"]["resolver_hint_safety"]["deployment_gate"][
            "passed"
        ]:
            raise RuntimeError("Resolver 자동 CAS 힌트 안전 회귀 gate가 실패했습니다.")
        report["last_completed_stage"] = "evaluate"
        from chemiguard119.release import (
            bind_evaluation_report,
            create_runtime_manifest,
        )

        release_commit = (os.getenv("CHEMIGUARD119_GIT_COMMIT") or "UNKNOWN").strip()
        report_bindings = {
            "resolver": (
                resolver_eval_path,
                args.resolver_evaluation,
                "resolver",
            ),
            "resolver_hint_safety": (
                resolver_safety_eval_path,
                args.resolver_safety_evaluation,
                "resolver_hint_safety",
            ),
            "retriever_sections": (
                retriever_section_eval_path,
                args.retriever_section_evaluation,
                "retriever_sections",
            ),
            "e2e_scenarios": (
                e2e_eval_path,
                args.e2e_evaluation,
                "e2e_scenarios",
            ),
        }
        if re.fullmatch(r"[0-9a-fA-F]{40}", release_commit):
            for report_name, (
                evaluation_report_path,
                evaluation_dataset_path,
                contract_name,
            ) in report_bindings.items():
                report["stages"]["evaluate"][report_name] = bind_evaluation_report(
                    report["stages"]["evaluate"][report_name],
                    report_path=evaluation_report_path,
                    dataset_path=evaluation_dataset_path,
                    evaluation_contract=report["stages"]["evaluation_contract"][
                        contract_name
                    ],
                    profile=args.evaluation_profile,
                    git_commit=release_commit,
                )

        evaluation_evidence: dict[str, dict[str, Path]] = {
            name: {
                "report_path": evaluation_report_path,
                "dataset_path": evaluation_dataset_path,
            }
            for name, (
                evaluation_report_path,
                evaluation_dataset_path,
                _contract_name,
            ) in report_bindings.items()
        }
        for name in ("parser_locked", "e2e_scenarios"):
            evaluation_report_path = getattr(args, f"{name}_report")
            evaluation_dataset_path = getattr(args, f"{name}_dataset")
            if (
                evaluation_report_path is not None
                or evaluation_dataset_path is not None
            ):
                if evaluation_report_path is None or evaluation_dataset_path is None:
                    raise ValueError(
                        f"{name}: evaluator report와 dataset 경로를 함께 제공해야 합니다."
                    )
                evaluation_evidence[name] = {
                    "report_path": evaluation_report_path,
                    "dataset_path": evaluation_dataset_path,
                }

        report["stages"]["release_manifest"] = create_runtime_manifest(
            db_path=args.db,
            resolver_model_path=args.resolver_model,
            retriever_model_path=args.retriever_model,
            config_dir=args.config_dir,
            git_commit=release_commit,
            evaluation_evidence=evaluation_evidence,
            release_attestation_path=args.release_attestation,
        )
        report["last_completed_stage"] = "release_manifest"
        report["status"] = "COMPLETED"
    except Exception as exc:
        report["status"] = "FAILED"
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_json(report_path, report)
        _write_json(latest_path, report)
    return report


def _interactive(args: argparse.Namespace) -> dict[str, Any]:
    print("\n케미체크119 대화형 CLI입니다. `help`를 입력하면 예시를 볼 수 있습니다.")
    print("종료: quit 또는 종료")
    print(f"안전 안내: {SAFETY_NOTICE}\n")
    while True:
        try:
            line = input("케미체크119> ").strip()
        except EOFError:
            break
        if not line:
            continue
        if line.lower() in {"quit", "exit", "q"} or line == "종료":
            break
        if line.lower() in {"help", "도움말"}:
            print("예: resolve 염산")
            print("예: search '차아염소산나트륨 누출 대응'")
            print("예: parse '차아염소산나트륨 탱크가 누출되고 옆에 염산이 있습니다'")
            print("예: incident '염산 탱크에서 누출 중'")
            print("예: review 7681-52-9 7647-01-0")
            continue
        try:
            tokens = shlex.split(line)
        except ValueError as exc:
            print(f"입력 해석 오류: {exc}")
            continue
        if tokens and tokens[0] not in {
            "doctor",
            "coverage",
            "audit",
            "prepare",
            "train",
            "evaluate",
            "evaluate-e2e",
            "e2e-review",
            "resolve",
            "search",
            "parse",
            "incident",
            "review",
            "finetune-check",
            "pipeline",
            "release-manifest",
            "interactive",
        }:
            tokens = ["parse", line]
        if tokens and tokens[0] == "interactive":
            print("이미 대화형 모드입니다.")
            continue
        try:
            main(tokens)
        except SystemExit:
            # argparse 도움말/입력 오류 뒤에도 REPL을 유지한다.
            continue
    return {"status": "CLOSED", "message": "대화형 CLI를 종료했습니다."}


def _add_json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="사람용 출력 대신 JSON으로 출력",
    )


def _add_artifact_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db", type=_path, default=DEFAULT_DB_PATH, help="SQLite DB 경로"
    )
    parser.add_argument(
        "--resolver-model",
        type=_path,
        default=DEFAULT_RESOLVER_MODEL,
        help="resolver artifact 경로",
    )
    parser.add_argument(
        "--retriever-model",
        type=_path,
        default=DEFAULT_RETRIEVER_MODEL,
        help="retriever artifact 경로",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chemiguard119",
        description="케미체크119 데이터·모델링·안전 검토 CLI",
        epilog="주의: 결과는 의사결정 보조용이며 최종 결정은 현장 지휘관이 수행합니다.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--json", action="store_true", help="사람용 출력 대신 JSON으로 출력"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser(
        "doctor", help="데이터·artifact·선택적 LM Studio 환경 진단"
    )
    doctor.add_argument("--data-dir", type=_path, default=FINAL_DATA_DIR)
    _add_artifact_arguments(doctor)
    doctor.add_argument(
        "--check-lmstudio", action="store_true", help="로컬 LM Studio 연결도 확인"
    )
    doctor.add_argument("--base-url", default="http://127.0.0.1:1234/v1")
    doctor.add_argument("--timeout", type=int, default=3)
    _add_json_option(doctor)
    doctor.set_defaults(handler=_doctor)

    coverage = subparsers.add_parser(
        "coverage", help="시설 과거 이력의 전국 시·도·시설·CAS 범위 확인"
    )
    coverage.add_argument(
        "--db", type=_path, default=DEFAULT_DB_PATH, help="SQLite DB 경로"
    )
    _add_json_option(coverage)
    coverage.set_defaults(handler=_coverage)

    audit = subparsers.add_parser("audit", help="필수 원천 CSV 8개 구조·결측·의미 점검")
    audit.add_argument("--data-dir", type=_path, default=FINAL_DATA_DIR)
    audit.add_argument(
        "--include-hash", action="store_true", help="재현성용 SHA-256 계산"
    )
    audit.add_argument("--report", type=_path, help="JSON 보고서 저장 경로")
    _add_json_option(audit)
    audit.set_defaults(handler=_audit)

    prepare = subparsers.add_parser(
        "prepare", help="CSV를 정규화하여 SQLite 검색 DB 생성"
    )
    prepare.add_argument("--data-dir", type=_path, default=FINAL_DATA_DIR)
    prepare.add_argument("--db", type=_path, default=DEFAULT_DB_PATH)
    prepare.add_argument("--config-dir", type=_path, default=CONFIG_DIR)
    _add_json_option(prepare)
    prepare.set_defaults(handler=_prepare_command)

    train = subparsers.add_parser("train", help="resolver와 근거 검색 기준선 학습")
    _add_artifact_arguments(train)
    train.add_argument(
        "--only", choices=("all", "resolver", "retriever"), default="all"
    )
    train.add_argument(
        "--max-features", type=int, default=30_000, help="검색기 분기별 최대 피처"
    )
    _add_json_option(train)
    train.set_defaults(handler=_train)

    evaluate = subparsers.add_parser(
        "evaluate", help="내부 회귀셋으로 후보검색·검색기 평가"
    )
    _add_artifact_arguments(evaluate)
    evaluate.add_argument(
        "--only", choices=("all", "resolver", "retriever"), default="all"
    )
    evaluate.add_argument(
        "--resolver-evaluation",
        type=_path,
        default=EVALUATION_DIR / "resolver_regression_queries.csv",
    )
    evaluate.add_argument(
        "--resolver-safety-evaluation",
        type=_path,
        default=EVALUATION_DIR / "resolver_hint_safety_queries.csv",
    )
    evaluate.add_argument(
        "--retriever-evaluation",
        type=_path,
        default=EVALUATION_DIR / "retrieval_regression_queries.csv",
    )
    evaluate.add_argument(
        "--retriever-section-evaluation",
        type=_path,
        default=EVALUATION_DIR / "retrieval_section_regression.jsonl",
    )
    evaluate.add_argument(
        "--evaluation-profile",
        choices=(
            "INTERNAL_REGRESSION",
            "COMPETITION_REVIEWED",
            "PILOT_REVIEWED",
        ),
        default="INTERNAL_REGRESSION",
        help="평가 데이터 검수·주장 범위 gate",
    )
    evaluate.add_argument("--report-dir", type=_path, default=DEFAULT_REPORT_DIR)
    _add_json_option(evaluate)
    evaluate.set_defaults(handler=_evaluate)

    evaluate_e2e = subparsers.add_parser(
        "evaluate-e2e",
        help="신고 입력부터 확인 gate·충돌 검토까지 실제 파이프라인 안전 평가",
    )
    _add_artifact_arguments(evaluate_e2e)
    evaluate_e2e.add_argument("--config-dir", type=_path, default=CONFIG_DIR)
    evaluate_e2e.add_argument(
        "--evaluation",
        type=_path,
        default=EVALUATION_DIR / "e2e_scenarios_draft.jsonl",
        help="E2E 시나리오 JSONL 경로",
    )
    evaluate_e2e.add_argument(
        "--evaluation-profile",
        choices=(
            "INTERNAL_REGRESSION",
            "COMPETITION_REVIEWED",
            "PILOT_REVIEWED",
        ),
        default="INTERNAL_REGRESSION",
        help="평가 데이터 검수·주장 범위 gate",
    )
    evaluate_e2e.add_argument(
        "--report",
        type=_path,
        default=DEFAULT_REPORT_DIR / "e2e_scenario_evaluation.json",
        help="JSON 평가 보고서 저장 경로",
    )
    _add_json_option(evaluate_e2e)
    evaluate_e2e.set_defaults(handler=_evaluate_e2e)

    e2e_review = subparsers.add_parser(
        "e2e-review",
        help="E2E 후보 생성·독립 검수 시트·합의 병합·모델 preflight",
    )
    e2e_review_actions = e2e_review.add_subparsers(
        dest="e2e_review_action",
        required=True,
    )
    e2e_review.set_defaults(handler=_e2e_review)

    e2e_review_dir = DEFAULT_REPORT_DIR / "e2e_review"
    review_generate = e2e_review_actions.add_parser(
        "generate",
        help="공개 검증 15쌍과 hard case로 검수 전 50건 후보 생성",
    )
    review_generate.add_argument(
        "--pair-snapshot",
        type=_path,
        default=EVALUATION_DIR / "verified_pair_snapshot_2024.json",
    )
    review_generate.add_argument(
        "--output",
        type=_path,
        default=e2e_review_dir / "e2e_competition_candidates.jsonl",
    )
    _add_json_option(review_generate)

    review_export = e2e_review_actions.add_parser(
        "export",
        help="정답이 비어 있는 라벨러 또는 독립 검수자 CSV 생성",
    )
    review_export.add_argument("--candidates", type=_path, required=True)
    review_export.add_argument(
        "--actor-role", choices=("LABELER", "REVIEWER"), required=True
    )
    review_export.add_argument("--actor-id", required=True)
    review_export.add_argument("--output", type=_path, required=True)
    _add_json_option(review_export)

    review_merge = e2e_review_actions.add_parser(
        "merge",
        help="두 독립 검수 시트가 완전히 일치할 때 reviewed JSONL 생성",
    )
    review_merge.add_argument("--candidates", type=_path, required=True)
    review_merge.add_argument("--labeler-sheet", type=_path, required=True)
    review_merge.add_argument("--reviewer-sheet", type=_path, required=True)
    review_merge.add_argument("--output", type=_path, required=True)
    review_merge.add_argument("--report", type=_path)
    _add_json_option(review_merge)

    review_preflight = e2e_review_actions.add_parser(
        "preflight",
        help="정답 없이 현재 모델 상태와 안전 위반만 관찰",
    )
    review_preflight.add_argument("--candidates", type=_path, required=True)
    _add_artifact_arguments(review_preflight)
    review_preflight.add_argument("--config-dir", type=_path, default=CONFIG_DIR)
    review_preflight.add_argument(
        "--report",
        type=_path,
        default=e2e_review_dir / "e2e_candidate_preflight.json",
    )
    _add_json_option(review_preflight)

    resolve = subparsers.add_parser(
        "resolve", help="물질명·CAS·별칭에서 후보 물질 검색"
    )
    resolve.add_argument("query", help="물질명, CAS, 화학식 또는 별칭")
    resolve.add_argument("--resolver-model", type=_path, default=DEFAULT_RESOLVER_MODEL)
    resolve.add_argument("--top-k", type=int, default=3)
    resolve.add_argument("--minimum-score", type=float, default=0.20)
    _add_json_option(resolve)
    resolve.set_defaults(handler=_resolve)

    discover = subparsers.add_parser(
        "discover",
        help="물질명·CAS·색상·냄새·상태 관찰에서 확인 전 후보와 근거 검색",
    )
    discover.add_argument("query", help="물질명, CAS 또는 두 가지 이상 관찰 정보")
    _add_artifact_arguments(discover)
    discover.add_argument("--top-k", type=int, choices=range(1, 6), default=5)
    discover.add_argument(
        "--evidence-top-k",
        type=int,
        choices=range(1, 6),
        default=3,
    )
    _add_json_option(discover)
    discover.set_defaults(handler=_discover)

    search = subparsers.add_parser("search", help="KOSHA·CAMEO 공식 근거 검색")
    search.add_argument("query", help="검색 질의")
    _add_artifact_arguments(search)
    search.add_argument("--cas-hint", help="검증된 CAS 검색 힌트")
    search.add_argument(
        "--no-resolve-hint", action="store_true", help="resolver 자동 CAS 힌트 비활성화"
    )
    search.add_argument("--top-k", type=int, default=8)
    _add_json_option(search)
    search.set_defaults(handler=_search)

    parse = subparsers.add_parser("parse", help="신고문 표면 표현을 제한적으로 구조화")
    parse.add_argument("text", help="신고 원문")
    parse.add_argument(
        "--backend", choices=("deterministic", "lmstudio"), default="deterministic"
    )
    parse.add_argument("--resolver-model", type=_path, default=DEFAULT_RESOLVER_MODEL)
    parse.add_argument("--model", help="LM Studio에 로드된 모델 ID")
    parse.add_argument("--base-url", default="http://127.0.0.1:1234/v1")
    parse.add_argument("--timeout", type=int, default=120)
    _add_json_option(parse)
    parse.set_defaults(handler=_parse)

    incident = subparsers.add_parser(
        "incident",
        help="임의 신고문을 구조화→근거검색→충돌검토 파이프라인으로 실행",
    )
    incident.add_argument("text", help="상황실 신고문 또는 현장 입력")
    _add_artifact_arguments(incident)
    incident.add_argument("--config-dir", type=_path, default=CONFIG_DIR)
    incident.add_argument(
        "--confirmed-incident-cas",
        help="용기 라벨·현장 MSDS 등으로 확인한 사고물질 CAS",
    )
    incident.add_argument(
        "--confirmed-facility-cas",
        help="현재 존재를 현장에서 확인한 시설물질 CAS",
    )
    incident.add_argument(
        "--action", action="append", default=[], help="검토 중인 대응(반복 가능)"
    )
    incident.add_argument("--evidence-top-k", type=int, default=5, choices=range(1, 11))
    incident.add_argument(
        "--rule-policy",
        choices=("PUBLIC_SOURCE_PILOT_V1", "APPROVED_ONLY"),
        default="PUBLIC_SOURCE_PILOT_V1",
        help="충돌 검토 정책(기본: 공개 근거 파일럿)",
    )
    _add_json_option(incident)
    incident.set_defaults(handler=_incident)

    review = subparsers.add_parser("review", help="결정적 Rule Engine으로 물질쌍 검토")
    review.add_argument("incident_cas", help="사고물질 CAS")
    review.add_argument("facility_cas", help="시설 확인물질 CAS")
    review.add_argument("--db", type=_path, default=DEFAULT_DB_PATH)
    review.add_argument("--config-dir", type=_path, default=CONFIG_DIR)
    review.add_argument(
        "--action",
        action="append",
        default=[],
        help="계획 대응(반복 가능, 현재는 미검증 표시)",
    )
    review.add_argument(
        "--rule-policy",
        choices=("PUBLIC_SOURCE_PILOT_V1", "APPROVED_ONLY"),
        default="PUBLIC_SOURCE_PILOT_V1",
        help="충돌 검토 정책(기본: 공개 근거 파일럿)",
    )
    _add_json_option(review)
    review.set_defaults(handler=_review)

    finetune = subparsers.add_parser(
        "finetune-check",
        help="선택적 로컬 신고문 구조화 파인튜닝 준비도 점검",
    )
    finetune.add_argument(
        "--dataset-path",
        type=_path,
        default=Path("data/finetune").resolve(),
        help="분할 정보와 전문가 승인 상태가 포함된 JSONL 파일 또는 디렉터리",
    )
    finetune.add_argument("--report", type=_path, help="준비도 JSON 보고서 저장 경로")
    finetune.add_argument("--base-model", default="Qwen/Qwen3.5-9B")
    finetune.add_argument(
        "--output-dir", type=_path, default=DEFAULT_ARTIFACT_DIR / "finetune"
    )
    _add_json_option(finetune)
    finetune.set_defaults(handler=_finetune_check)

    pipeline = subparsers.add_parser(
        "pipeline",
        help="audit→prepare→train→evaluate→release manifest 전체 파이프라인",
    )
    pipeline.add_argument("--data-dir", type=_path, default=FINAL_DATA_DIR)
    _add_artifact_arguments(pipeline)
    pipeline.add_argument("--report-dir", type=_path, default=DEFAULT_REPORT_DIR)
    pipeline.add_argument("--config-dir", type=_path, default=CONFIG_DIR)
    pipeline.add_argument("--include-hash", action="store_true")
    pipeline.add_argument("--max-features", type=int, default=30_000)
    pipeline.add_argument(
        "--resolver-evaluation",
        type=_path,
        default=EVALUATION_DIR / "resolver_regression_queries.csv",
    )
    pipeline.add_argument(
        "--resolver-safety-evaluation",
        type=_path,
        default=EVALUATION_DIR / "resolver_hint_safety_queries.csv",
    )
    pipeline.add_argument(
        "--retriever-evaluation",
        type=_path,
        default=EVALUATION_DIR / "retrieval_regression_queries.csv",
    )
    pipeline.add_argument(
        "--retriever-section-evaluation",
        type=_path,
        default=EVALUATION_DIR / "retrieval_section_regression.jsonl",
    )
    pipeline.add_argument(
        "--e2e-evaluation",
        type=_path,
        default=EVALUATION_DIR / "e2e_scenarios_draft.jsonl",
        help="실제 사고 분석 경로 E2E 시나리오 JSONL",
    )
    pipeline.add_argument(
        "--evaluation-profile",
        choices=(
            "INTERNAL_REGRESSION",
            "COMPETITION_REVIEWED",
            "PILOT_REVIEWED",
        ),
        default="INTERNAL_REGRESSION",
        help="reviewed profile은 DRAFT 평가 데이터에서 release 생성을 차단",
    )
    pipeline.add_argument(
        "--parser-locked-report",
        type=_path,
        help="별도 parser locked-test evaluator JSON 보고서",
    )
    pipeline.add_argument(
        "--parser-locked-dataset",
        type=_path,
        help="parser locked-test 원본 평가 데이터",
    )
    pipeline.add_argument(
        "--e2e-scenarios-report",
        type=_path,
        help="별도 E2E scenario evaluator JSON 보고서",
    )
    pipeline.add_argument(
        "--e2e-scenarios-dataset",
        type=_path,
        help="E2E scenario 원본 평가 데이터",
    )
    pipeline.add_argument(
        "--release-attestation",
        type=_path,
        help="독립 검수자가 서명한 release attestation JSON",
    )
    _add_json_option(pipeline)
    pipeline.set_defaults(handler=_pipeline)

    release_manifest = subparsers.add_parser(
        "release-manifest",
        help="DB·모델·설정 파일의 배포용 SHA-256 manifest 생성",
    )
    _add_artifact_arguments(release_manifest)
    release_manifest.add_argument("--config-dir", type=_path, default=CONFIG_DIR)
    release_manifest.add_argument("--output", type=_path)
    release_manifest.add_argument("--git-commit")
    for option, description in (
        ("resolver", "Resolver evaluator"),
        ("resolver-hint-safety", "Resolver CAS 안전 evaluator"),
        ("retriever-sections", "근거 section evaluator"),
        ("parser-locked", "신고문 parser locked-test evaluator"),
        ("e2e-scenarios", "통합 사고분석 E2E evaluator"),
    ):
        release_manifest.add_argument(
            f"--{option}-report",
            type=_path,
            help=f"{description} JSON 보고서",
        )
        release_manifest.add_argument(
            f"--{option}-dataset",
            type=_path,
            help=f"{description} 원본 평가 데이터",
        )
    release_manifest.add_argument(
        "--release-attestation",
        type=_path,
        help="독립 검수자가 서명한 release attestation JSON",
    )
    _add_json_option(release_manifest)
    release_manifest.set_defaults(handler=_release_manifest)

    interactive = subparsers.add_parser(
        "interactive", help="반복 실행 가능한 대화형 CLI"
    )
    _add_json_option(interactive)
    interactive.set_defaults(handler=_interactive)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    try:
        payload = args.handler(args)
        if not isinstance(payload, dict):
            payload = {"status": "COMPLETED", "result": payload}
        _emit(args.command, payload, as_json)
        return (
            1
            if payload.get("status")
            in {
                "FAILED",
                "BLOCKED_SAFETY_GATE",
                "BLOCKED_EVALUATION_GATE",
                "OUTPUT_VALIDATION_FAILED",
            }
            else 0
        )
    except KeyboardInterrupt:
        payload = {"status": "INTERRUPTED", "error": "사용자가 실행을 중단했습니다."}
        _emit("error", payload, as_json)
        return 130
    except (
        Exception
    ) as exc:  # CLI 경계에서는 traceback 대신 재실행 가능한 오류를 제공한다.
        payload = {
            "status": "ERROR",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "hint": "`chemiguard119 doctor`와 해당 명령의 `--help`를 확인하세요.",
        }
        _emit("error", payload, as_json)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
