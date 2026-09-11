"""실제 loopback 소켓 + 지연 주입. 실제 모델 정확도 평가는 아니다."""

import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx2 as httpx
import pytest
import uvicorn

import test_api as api_fixtures
from chemiguard119 import pipeline
from chemiguard119.action_examples import BOTH_CONFIRMED
from chemiguard119.api import create_app
from chemiguard119.brief_orchestrator import BriefOrchestrator

runtime = api_fixtures.runtime
stub_pipeline_boundaries = api_fixtures.stub_pipeline_boundaries


@pytest.fixture()
def live_api(runtime, stub_pipeline_boundaries, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def block(*args, **kwargs):
        entered.set()
        release.wait(5)
        return {
            "status": "NO_EVIDENCE_FOUND",
            "cas_hint": kwargs.get("cas_hint"),
            "results": [],
        }

    monkeypatch.setattr(pipeline, "search_evidence", block)
    app = create_app(runtime=runtime, api_key="synthetic-loopback-key")
    app.state.brief_orchestrator.close()
    app.state.brief_orchestrator = BriefOrchestrator(deadline_seconds=1, parallel=True)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", access_log=False, lifespan="on")
    )
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.perf_counter() + 3
    while not server.started and time.perf_counter() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}", app, entered, release
    finally:
        release.set()
        server.should_exit = True
        thread.join(timeout=3)
        sock.close()
        assert not thread.is_alive()


def test_real_sse_disconnect_drains_and_never_runs_late_rule(
    live_api, stub_pipeline_boundaries
):
    url, app, entered, release = live_api
    with httpx.stream(
        "POST",
        url + "/api/v1/agents/incidents/brief/stream",
        json=BOTH_CONFIRMED,
        headers={"X-API-Key": "synthetic-loopback-key"},
        timeout=2,
    ) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith("data: "):
                assert json.loads(line[6:])["phase"] == "initial"
                assert entered.wait(timeout=0.5)
                break
    time.sleep(0.05)  # 연결 종료가 서버에 전달되도록 제한된 관찰 구간
    release.set()
    deadline = time.perf_counter() + 1
    while app.state.brief_orchestrator.tools.active and time.perf_counter() < deadline:
        time.sleep(0.01)
    assert app.state.brief_orchestrator.tools.active == 0
    assert not stub_pipeline_boundaries


def test_real_concurrent_capacity_timeout_and_recovery(
    live_api, stub_pipeline_boundaries
):
    url, app, entered, release = live_api

    def post():
        return httpx.post(
            url + "/api/v1/agents/incidents/brief",
            json=BOTH_CONFIRMED,
            headers={"X-API-Key": "synthetic-loopback-key"},
            timeout=3,
        )

    with ThreadPoolExecutor(max_workers=3) as threads:
        futures = [threads.submit(post) for _ in range(2)]
        assert entered.wait(1)
        deadline = time.perf_counter() + 0.5
        while (
            app.state.brief_orchestrator.coordinators.active < 2
            and time.perf_counter() < deadline
        ):
            time.sleep(0.005)
        assert app.state.brief_orchestrator.coordinators.active == 2
        rejected = post()
        assert rejected.status_code == 503
        results = [future.result(timeout=3) for future in futures]
    assert all(result.json()["status"] == "TIMEOUT" for result in results)
    assert app.state.brief_orchestrator.tools.active <= 6
    assert app.state.brief_orchestrator.coordinators.peak == 2
    release.set()
    deadline = time.perf_counter() + 1
    while app.state.brief_orchestrator.tools.active and time.perf_counter() < deadline:
        time.sleep(0.01)
    assert app.state.brief_orchestrator.tools.active == 0
    assert not stub_pipeline_boundaries
    assert post().status_code == 200
