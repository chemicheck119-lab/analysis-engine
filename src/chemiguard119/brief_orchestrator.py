"""결정적 역할 조율과 유한 작업 슬롯. 서버 세션이나 승인 memory를 저장하지 않는다."""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from chemiguard119 import pipeline
from chemiguard119.action_brief import failed_brief, final_brief
from chemiguard119.action_models import BriefRequest, BriefResponse
from chemiguard119.action_policy import (
    BriefHeld,
    before_rule,
    official_cas_link,
    usable_source,
)
from chemiguard119.api_models import AnalysisResponse
from chemiguard119.facility import search_facility_history
from chemiguard119.discovery import discover_substances


class CapacityExceeded(Exception):
    pass


class BoundedPool:
    """timeout 응답 후에도 실제 작업이 끝나야 슬롯을 반환한다. 대기열은 없다."""

    def __init__(self, capacity: int, name: str) -> None:
        self.capacity = capacity
        self.executor = ThreadPoolExecutor(
            max_workers=capacity, thread_name_prefix=name
        )
        self.slots = threading.BoundedSemaphore(capacity)
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def submit(self, function: Callable[..., Any], *args: Any) -> Future:
        if not self.slots.acquire(blocking=False):
            raise CapacityExceeded()
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            future = self.executor.submit(function, *args)
        except BaseException:
            self._release()
            raise
        future.add_done_callback(lambda _: self._release())
        return future

    def _release(self) -> None:
        with self.lock:
            self.active -= 1
        self.slots.release()

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)


@dataclass
class BriefJob:
    initial: BriefResponse
    deadline_seconds: float
    started: float = field(default_factory=time.perf_counter)
    cancelled: threading.Event = field(default_factory=threading.Event)
    trace: list[dict[str, Any]] = field(default_factory=list)
    trace_lock: threading.Lock = field(default_factory=threading.Lock)
    future: Future | None = None

    def check(self) -> None:
        if (
            self.cancelled.is_set()
            or time.perf_counter() - self.started >= self.deadline_seconds
        ):
            raise BriefHeld("DEADLINE_EXCEEDED")

    def record(self, event: dict[str, Any]) -> None:
        with self.trace_lock:
            self.trace.append(event)

    def processing(self) -> dict[str, Any]:
        with self.trace_lock:
            trace = list(self.trace)
        return {
            "analysis_latency_ms": round(
                (time.perf_counter() - self.started) * 1000, 3
            ),
            "stt_included": False,
            "deadline_seconds": self.deadline_seconds,
            "tasks": trace,
            "llm": "SKIPPED_BY_POLICY",
            "cancel_mode": "COOPERATIVE_BOUNDED_INFLIGHT",
            "retries": 0,
        }


class BriefOrchestrator:
    def __init__(
        self, *, deadline_seconds: float = 15.0, parallel: bool = True
    ) -> None:
        if not 0 < deadline_seconds <= 30:
            raise ValueError("제한시간은 0초 초과 30초 이하입니다.")
        self.deadline_seconds = deadline_seconds
        self.parallel = parallel
        self.coordinators = BoundedPool(2, "brief-plan")
        self.tools = BoundedPool(6, "brief-tool")

    def close(self) -> None:
        self.coordinators.close()
        self.tools.close()

    def start(
        self,
        payload: BriefRequest,
        initial: BriefResponse,
        runtime: Any,
        rule_policy: str,
        finalize: Callable[..., AnalysisResponse],
    ) -> BriefJob:
        job = BriefJob(initial, self.deadline_seconds)
        job.record(
            {
                "event": "configured",
                "role": "결정적 정책",
                "tool": "EXECUTION_LIMITS",
                "parallel": self.parallel,
                "coordinator_slots": 2,
                "tool_slots": 6,
                "max_role_retrievals": 2,
                "max_cas_link_fallbacks": 2,
                "max_discovery_calls": 1,
                "max_discovery_internal_searches": 3,
                "max_facility_history_calls": 1,
                "max_rule_calls": 1,
                "retries": 0,
            }
        )
        job.future = self.coordinators.submit(
            self._run, job, payload, runtime, rule_policy, finalize
        )
        return job

    def _tool(
        self,
        job: BriefJob,
        role: str,
        name: str,
        function: Callable[[], Any],
        arguments: dict[str, Any],
    ) -> Any:
        job.check()
        started = time.perf_counter()
        job.record(
            {"event": "started", "role": role, "tool": name, "arguments": arguments}
        )
        try:
            result = function()
            job.check()
        except Exception:
            job.record({"event": "failed_or_cancelled", "role": role, "tool": name})
            raise
        job.record(
            {
                "event": "completed",
                "role": role,
                "tool": name,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        )
        return result

    def _wait_tool(self, job: BriefJob, future: Future) -> Any:
        while not future.done():
            job.check()
            job.cancelled.wait(0.01)
        job.check()
        return future.result()

    def _run(
        self,
        job: BriefJob,
        payload: BriefRequest,
        runtime: Any,
        rule_policy: str,
        finalize: Callable[..., AnalysisResponse],
    ) -> BriefResponse:
        effective = payload.effective_analysis()
        child_futures: list[Future] = []
        try:
            location = effective.location
            facility_query = (
                (location.facility_name or location.address) if location else None
            )

            def history_lookup() -> dict[str, Any]:
                try:
                    return self._tool(
                        job,
                        "대응근거",
                        "FACILITY_HISTORY",
                        lambda: search_facility_history(
                            facility_query,
                            runtime.db_path,
                            province=location.province,
                            top_k=10,
                        ),
                        {"query_present": True, "top_k": 10},
                    )
                except BriefHeld:
                    raise
                except Exception:
                    return {"status": "UNAVAILABLE", "results": []}

            history_future = (
                self.tools.submit(history_lookup)
                if facility_query and self.parallel
                else None
            )
            if history_future:
                child_futures.append(history_future)

            def dispatch(
                function: Callable, targets: list[dict[str, Any]]
            ) -> list[dict[str, Any]]:
                # Parser/Resolver가 반환한 역할별 후보 이후에만 호출된다. 최대 2회.
                job.record(
                    {
                        "event": "completed",
                        "role": "상황정리·물질식별",
                        "tool": "PARSER_RESOLVER",
                        "candidate_promotion": False,
                    }
                )
                if len(targets) > 2:
                    raise BriefHeld("TOOL_LIMIT_EXCEEDED")

                def fetch(target: dict[str, Any]) -> dict[str, Any]:
                    found = self._tool(
                        job,
                        "대응근거",
                        "OFFICIAL_EVIDENCE",
                        lambda: function(target),
                        {
                            "role": target["role"],
                            "cas_hint": target["cas_hint"],
                            "top_k": effective.evidence_top_k,
                        },
                    )
                    rows = found.get("retrieval", {}).get("results", [])
                    if target.get("cas_basis") == "RESPONDER_CONFIRMED" and not any(
                        usable_source(row, target["cas_hint"]) for row in rows
                    ):
                        fallback = self._tool(
                            job,
                            "대응근거",
                            "OFFICIAL_CAS_LINK_FALLBACK",
                            lambda: official_cas_link(
                                runtime.retriever_artifact, target["cas_hint"]
                            ),
                            {
                                "cas_hint": target["cas_hint"],
                                "top_k": 1,
                                "section_relevance_verified": False,
                            },
                        )
                        if fallback:
                            # 기존 잘못된 CAS/상충 행은 검증에서 사라지지 않도록 보존한다.
                            found["retrieval"]["results"] = [*rows, *fallback]
                            found["retrieval"]["status"] = "COMPLETED"
                    return found

                if not self.parallel:
                    return [fetch(target) for target in targets]
                futures = []
                for target in targets:
                    future = self.tools.submit(fetch, target)
                    futures.append(future)
                    child_futures.append(future)
                return [self._wait_tool(job, future) for future in futures]

            job.record(
                {
                    "event": "started",
                    "role": "상황정리·물질식별",
                    "tool": "PARSER_RESOLVER",
                    "input_characters": len(effective.input.text),
                }
            )
            analysis = pipeline.analyze_incident(
                effective.input.text,
                db_path=runtime.db_path,
                resolver_artifact=runtime.resolver_artifact,
                retriever_artifact=runtime.retriever_artifact,
                confirmed_incident_cas=effective.confirmed_incident_substance.cas_number
                if effective.confirmed_incident_substance
                else None,
                confirmed_facility_cas=effective.confirmed_facility_substance.cas_number
                if effective.confirmed_facility_substance
                else None,
                planned_actions=[item.raw_text for item in effective.planned_actions],
                policy_mode=rule_policy,
                config_dir=runtime.config_dir,
                evidence_top_k=effective.evidence_top_k,
                evidence_dispatcher=dispatch,
                cancellation_check=job.check,
                before_rule_check=lambda result: before_rule(effective, result),
            )
            job.check()
            job.record(
                {
                    "event": "completed",
                    "role": "결정적 정책",
                    "tool": "RULE_GATE",
                    "executed": bool(analysis.get("rule_review", {}).get("executed")),
                }
            )
            history = (
                self._wait_tool(job, history_future)
                if history_future
                else history_lookup()
                if facility_query
                else None
            )
            public = finalize(effective, analysis, history, job.started)
            job.check()
            discovery = None
            if not public.model_outputs.get("substance_candidates") and not (
                effective.confirmed_incident_substance
                or effective.confirmed_facility_substance
            ):
                # 역할별 이름 후보가 없을 때만 별도 후보 탐색. 이 결과로 Rule을 재실행하지 않는다.
                def discovery_search(*args: Any, **kwargs: Any) -> dict[str, Any]:
                    return self._tool(
                        job,
                        "물질식별",
                        "DISCOVERY_EVIDENCE",
                        lambda: pipeline.search_evidence(*args, **kwargs),
                        {"cas_hint": kwargs.get("cas_hint"), "top_k": 1},
                    )

                try:
                    discovered = self._tool(
                        job,
                        "물질식별",
                        "DISCOVERY",
                        lambda: discover_substances(
                            effective.input.text,
                            db_path=runtime.db_path,
                            resolver_artifact=runtime.resolver_artifact,
                            retriever_artifact=runtime.retriever_artifact,
                            top_k=3,
                            evidence_top_k=1,
                            evidence_searcher=discovery_search,
                        ),
                        {"top_k": 3, "max_internal_evidence_calls": 3},
                    )
                    discovery = {
                        "status": discovered["status"],
                        "requires_responder_confirmation": True,
                        "rule_eligible": False,
                        "candidates": [
                            {
                                key: row.get(key)
                                for key in (
                                    "rank",
                                    "cas_number",
                                    "display_name",
                                    "match_basis",
                                    "requires_responder_confirmation",
                                    "rule_eligible",
                                )
                            }
                            for row in discovered.get("candidates", [])
                        ],
                    }
                except BriefHeld:
                    raise
                except Exception:
                    discovery = {
                        "status": "UNAVAILABLE",
                        "requires_responder_confirmation": True,
                        "rule_eligible": False,
                        "candidates": [],
                    }
            result = final_brief(job.initial, public, job.processing())
            result.discovery = discovery
            result.processing["analysis_latency_ms"] = round(
                (time.perf_counter() - job.started) * 1000, 3
            )
            result.processing["initial_build_ms"] = job.initial.processing.get(
                "initial_build_ms"
            )
            return BriefResponse.model_validate(result.model_dump())
        except BriefHeld as error:
            return failed_brief(job.initial, error.code, job.processing())
        except CapacityExceeded:
            return failed_brief(job.initial, "TOOL_CAPACITY_EXCEEDED", job.processing())
        except Exception:
            return failed_brief(job.initial, "OUTPUT_REJECTED", job.processing())
        finally:
            job.cancelled.set()
            for future in child_futures:
                future.cancel()  # 실행 중인 thread를 죽이지 않는다. 슬롯은 실제 종료 때만 반환.

    async def finish(
        self, job: BriefJob, disconnected: Callable | None = None
    ) -> BriefResponse:
        try:
            if disconnected and await disconnected():
                raise asyncio.CancelledError()
            while not job.future.done():
                if disconnected and await disconnected():
                    raise asyncio.CancelledError()
                if time.perf_counter() - job.started >= job.deadline_seconds:
                    job.cancelled.set()
                    return failed_brief(
                        job.initial, "DEADLINE_EXCEEDED", job.processing()
                    )
                await asyncio.sleep(0.01)
            result = job.future.result()
            if time.perf_counter() - job.started >= job.deadline_seconds:
                return failed_brief(job.initial, "DEADLINE_EXCEEDED", job.processing())
            return result
        finally:
            job.cancelled.set()
            job.future.cancel()
