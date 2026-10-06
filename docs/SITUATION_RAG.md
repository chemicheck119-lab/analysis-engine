# 상황별 공식 대응 참고정보 — 로컬 구현 결과

2026-10-06. 서비스의 핵심은 확인된 물질과 신고 상황에 필요한 공식 자료를 짧게 전달하는 것이다. Airflow는 원천 수집·품질 검증·버전 반영·복구를 담당하는 배치 경로다. 이번 변경은 사고 요청 처리와 배치 실행을 합치지 않는다.

## 구현

기존 pipeline과 BM25/TF-IDF 검색을 재사용했다. response_retrieval.py는 확인된 CAS의 공식 KOSHA 자료에서 화재 5장, 누출 6장, 노출 응급조치 4장과 보호구 8장 항목을 선택한다. 확인 전 후보 검색 및 결정론적 두 CAS 게이트는 유지한다. 화재·누출은 기존 파서의 부정 처리 결과를 사용하며, 노출 표현·경로는 기존 부정 검사와 제한된 패턴을 사용한다. 키워드 해석은 현장 진단이나 사건 확정이 아니다. 복합 문장·교정·범위를 모두 이해한다는 주장은 하지 않는다.

노출 경로 미상이면 눈·피부·흡입·섭취 중 하나를 임의로 선택하지 않는다. 대응 장이나 유효한 공식 출처가 없으면 일반 자료나 다른 CAS 자료로 채우지 않고 누락 상태를 반환한다. 검색 장애도 대체 답변 없이 유지한다. 여러 요청 장의 대표 항목을 먼저 보존하며, 이는 장별 자료 존재 조건이고 전문가가 검수한 완전한 대응 순서가 아니다.

rag.py는 사고물질·시설물질의 자료를 번갈아 수집하고 공식 자료 발췌를 먼저 표시한다. LLM 호출은 이번 검증에서 사용하지 않았다. 최대 5문장 및 기존 인용 계약을 유지한다. 화면은 먼저 3문장과 원문 링크를 보여주며 나머지는 추가 자료로 제공한다. 노출 경로·자료 누락 안내는 기존 limitations 계약으로 BFF를 거쳐 표시한다. API 스키마 변경 없이 FE 타입의 기존 limitations 필드를 보완했다.

## 출처 복구

기존 DB와 고정 검색 모델을 대조하니 DB 출처는 올바르지만 모델의 KOSHA 764개 항목에 예전 설명 문자열이 남아 있었다. prepare_response_candidate.py는 원본 checksum을 확인하고 별도 후보 폴더를 만든다. DB의 레코드 ID·CAS·제목·본문·문서 버전·연결 상태가 같은 항목만 기존 DB의 공식 URL로 동기화한다. 본문·행 수·발행 날짜·resolver를 변경하지 않는다. URL은 원천 서비스 포털이며 물질별 직접 링크라고 표시하지 않는다. 새 문서 개정일을 만들거나 실제 원천을 재수집했다고 주장하지 않는다.

후보에는 새 artifact checksum, runtime manifest와 UNAPPROVED_LOCAL_CANDIDATE 메타데이터를 남긴다. 원본 런타임과 서비스 활성 버전은 변경하지 않았다. 개발 후보 manifest를 운영 승인·전문가 검토 완료로 취급하지 않는다. 전체 재수집본의 기존 품질 차단 조건도 완화하지 않았다.

## 실제 검증

실제 모델 /api/v1/agents/incidents/step에 합성 입력 6개를 실행했다. 미확인·한 물질 확인은 RAG 문장 0개였다. 두 물질 확인 뒤 누출은 두 CAS의 6장, 화재는 두 CAS의 5장, 피부 노출은 두 CAS의 피부 응급조치 항목이 처음 두 문장에 연결됐다. 노출 경로 미상은 4장 항목을 선택하지 않고 경로 확인·응급조치 자료 누락을 반환했다. 인용 ID와 반환 출처의 일치, 런타임 불변 checksum을 검사했다.

회귀검사는 다른 CAS·일반 자료 혼입, 설명 문자열 출처, 자료 없음, 부정된 노출, 입력 충돌, 검색 실패 보존, 보호구 장 유지, 두 물질 인용 분배와 누락 안내를 검사한다. 모델 828 passed/7 PostgreSQL skipped, FE 196 passed 및 전체 check 성공. 기존 Starlette/AnyIO 경고 1개. 초기에 pytest 실행 파일 직접 호출은 scripts 패키지 경로 문제로 수집에 실패했고, 저장소 문서의 python -m pytest로 전체 검사를 완료했다.

정확한 요약·hash·명령 범위는 SITUATION_RAG_VERIFICATION.json, 전체 합성 요청·응답은 /tmp/chemicheck119-response-rag-delivery-20261006에 있다. 원천 본문은 Git에 포함하지 않았다.

## 재현 명령

프로젝트 루트에서 실행한다. 출력 폴더는 반드시 새 경로를 사용한다.

```sh
.venv/bin/python scripts/data/prepare_response_candidate.py --runtime /Users/hywznn/Documents/chemicheck119-lab/private-data/analysis-runtime/action-brief-local-v1 --output /tmp/chemicheck119-response-candidate-new
.venv/bin/python scripts/data/verify_service_rag.py --runtime /tmp/chemicheck119-response-candidate-new --candidate-metadata /tmp/chemicheck119-response-candidate-new/candidate_metadata.json --output /tmp/chemicheck119-response-verification-new
.venv/bin/python -m pytest
```

화면 저장소 /Users/hywznn/.codex/worktrees/service-completion/fire-fe에서 `corepack pnpm check`를 실행한다. 두 저장소 브랜치는 fix/situation-rag-reference, fix/simple-rag-reference이며 GitHub 공개·병합·배포하지 않았다.

## 서비스 전환과 한계

현재 정상 버전을 유지하는 로컬 후보 검증까지 완료했다. 후보의 전문가 내용 검수 및 기존 승인 절차를 거치기 전 사용 중인 서비스 manifest/current pointer를 교체하지 않는다. 이전 정상 버전은 원래 경로에 보존되어 있다. Airflow의 기존 staging·승인·복구 경로와 별개로 직접 덮어쓰는 전환은 하지 않았다.

미검증: 실제 전화부터 BFF·화면까지 이번 후보의 전체 E2E, 소방대원 사용성, 현장 제품·혼합물·농도·상태에 대한 적합성, 상황별 필수 정보의 완전성, 시간 절감, 운영 가용성. CAS 및 장 번호 일치와 인용 ID 검증은 과학적 의미나 현장 안전성 보장이 아니다. 발췌는 원문 전체가 아니므로 출처를 확인해야 한다. 공식 자료 발췌를 현장 명령이나 LLM의 독자 판단으로 바꾸지 않았다.
