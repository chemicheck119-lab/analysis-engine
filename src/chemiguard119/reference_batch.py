"""Local-only, reviewed KOSHA batch; immutable runtime bundles and atomic activation.

No incident API calls, no deployment, no replacement of release qualification.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from importlib.metadata import version as package_version
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from chemiguard119 import preprocessing
from chemiguard119.kosha_client import (
    KOSHA_SOURCE_PAGE,
    KOSHA_STAGING_COLUMNS,
    KoshaApiError,
    KoshaMsdsClient,
)
from chemiguard119.utils import (
    compact_text,
    normalize_cas,
    sha256_file,
    valid_cas_checksum,
)

SCHEMA = "reference-batch-v1"
STEPS = ("collect", "archive", "normalize", "validate", "stage", "activate")
REQUIRED = (
    "레코드ID",
    "화학물질ID",
    "CAS번호",
    "화학물질명_국문",
    "MSDS_장번호",
    "MSDS_항목명_국문",
    "상세내용",
    "최종개정일",
)
NO_INFORMATION = {"자료없음", "자료없다", "해당없음", "해당사항없음", "없음", "없다"}


class BatchError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def locked(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def write_csv(path, columns, rows):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def rows_from(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle, strict=True))


def tree_hashes(path):
    return {
        str(p.relative_to(path)): sha256_file(p)
        for p in sorted(Path(path).rglob("*"))
        if p.is_file()
    }


def current(root):
    path = Path(root) / "current.json"
    return read(path) if path.exists() else None


def event(root, run_id, step, status, started, **details):
    # Append-only per-attempt records; SQLite transactions serialize writers.
    with sqlite3.connect(Path(root) / "runs.sqlite", timeout=30) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS event (id INTEGER PRIMARY KEY, run_id TEXT, "
            "step TEXT, status TEXT, started TEXT, finished TEXT, seconds REAL, details TEXT)"
        )
        db.execute(
            "INSERT INTO event VALUES(NULL,?,?,?,?,?,?,?)",
            (
                run_id,
                step,
                status,
                started[0],
                now(),
                time.monotonic() - started[1],
                json.dumps(details, ensure_ascii=False),
            ),
        )


class Pipeline:
    def __init__(self, root, run_id):
        self.root = Path(root).resolve()
        self.run_id = run_id
        self.work = self.root / "runs" / digest(run_id)
        self.state_path = self.work / "state.json"

    def state(self):
        return read(self.state_path)

    def initialize(self, spec):
        allowed = {
            "baseline_dir",
            "config_dir",
            "cas_numbers",
            "fixture_csv",
            "reprocess_version",
            "max_excluded_fraction",
            "max_error_fraction",
            "expected_exclusion_review_threshold",
            "count_change_threshold",
        }
        if set(spec) - allowed:
            raise BatchError("UNKNOWN_SPEC_FIELDS_SECRETS_BELONG_IN_ENV")
        targets = spec.get("cas_numbers", [])
        if not targets or any(not valid_cas_checksum(cas) for cas in targets):
            raise BatchError("INVALID_TARGET_CAS")
        if len({normalize_cas(cas) for cas in targets}) != len(targets):
            raise BatchError("DUPLICATE_TARGET_CAS")
        if spec.get("fixture_csv") and spec.get("reprocess_version"):
            raise BatchError("SOURCE_MODE_CONFLICT")
        for field in ("baseline_dir", "config_dir"):
            source = Path(spec[field]).resolve()
            if self.work.is_relative_to(source) or source.is_relative_to(self.work):
                raise BatchError("INPUT_OUTPUT_PATH_OVERLAP")
        with locked(self.root):
            if self.state_path.exists():
                if self.state()["spec"] != spec:
                    raise BatchError("RUN_ID_SPEC_CONFLICT")
                return
            # No state means an interrupted initialization; safely recreate only
            # this run's scratch directory, never an archived version.
            if self.work.exists():
                shutil.rmtree(self.work)
            self.work.mkdir(parents=True)
            # Capture all baseline inputs/config, not paths that can mutate between tasks.
            for name, source in (
                ("baseline", spec["baseline_dir"]),
                ("config", spec["config_dir"]),
            ):
                shutil.copytree(source, self.work / name)
            baseline = self.work / "baseline"
            for name in preprocessing.SOURCE_FILES.values():
                if not (baseline / name).is_file():
                    raise BatchError("BASELINE_SOURCE_MISSING")
            atomic_json(
                self.state_path,
                {
                    "schema": SCHEMA,
                    "run_id": self.run_id,
                    "spec": spec,
                    "started_at": now(),
                    "expected_current": current(self.root),
                    "done": [],
                    "input_hashes": {
                        "baseline": tree_hashes(baseline),
                        "config": tree_hashes(self.work / "config"),
                    },
                    "runtime_versions": {
                        name: package_version(name)
                        for name in ("numpy", "scikit-learn", "joblib")
                    },
                    "code_hashes": {
                        p.name: sha256_file(p)
                        for p in sorted(Path(__file__).parent.glob("*.py"))
                    },
                },
            )

    def execute(self, step, *, client=None, fault=None):
        started = (now(), time.monotonic())
        with locked(self.root):
            state = self.state()
            if step in state["done"]:
                return state
            index = STEPS.index(step)
            if list(state["done"]) != list(STEPS[:index]):
                raise BatchError("STAGE_ORDER_VIOLATION")
            try:
                event(self.root, self.run_id, step, "RUNNING", started)
                getattr(self, "_" + step)(state, client=client, fault=fault)
                state["done"].append(step)
                state.pop("failure", None)
                atomic_json(self.state_path, state)
                event(
                    self.root,
                    self.run_id,
                    step,
                    "SUCCESS",
                    started,
                    counts=state.get("counts", {}),
                    request_count=state.get("collection", {}).get("request_count"),
                    retry_count=state.get("collection", {}).get("retry_count"),
                    version=state.get("version"),
                )
            except Exception as error:
                # Never log arbitrary provider messages/URLs or raw records.
                code = (
                    error.code
                    if isinstance(error, KoshaApiError)
                    else (
                        str(error)
                        if isinstance(error, BatchError)
                        else type(error).__name__
                    )
                )
                state["failure"] = {"step": step, "code": code, "at": now()}
                atomic_json(self.state_path, state)
                event(
                    self.root,
                    self.run_id,
                    step,
                    "FAILED",
                    started,
                    code=code,
                    request_count=state.get("collection", {}).get("request_count"),
                    retry_count=state.get("collection", {}).get("retry_count"),
                    counts=state.get("counts", {}),
                )
                raise BatchError(code) from None
            return state

    def _collect(self, state, *, client, fault):
        spec = state["spec"]
        incoming = self.work / "incoming.csv"
        if spec.get("reprocess_version"):
            version = verify_bundle(self.root, spec["reprocess_version"])
            shutil.copyfile(version / "incoming.csv", incoming)
            state["collection"] = read(version / "collection.json")
            state["collection"]["reprocessed_at"] = now()
        elif spec.get("fixture_csv"):
            shutil.copyfile(spec["fixture_csv"], incoming)
            state["collection"] = {
                "mode": "fixture",
                "source": KOSHA_SOURCE_PAGE,
                "collected_at": now(),
                "synthetic": True,
                "request_count": 0,
                "retry_count": 0,
            }
        else:
            response_dir = self.work / "responses"
            response_dir.mkdir(exist_ok=True)

            def fetch(url, timeout):
                # Query contains serviceKey; retain only endpoint name and response bytes.
                with urlopen(url, timeout=timeout) as response:
                    payload = response.read(10 * 1024 * 1024 + 1)
                if len(payload) > 10 * 1024 * 1024:
                    raise KoshaApiError(
                        "KOSHA_RESPONSE_TOO_LARGE", "response too large"
                    )
                name = f"{len(list(response_dir.iterdir())):05d}-{Path(urlsplit(url).path).name}.xml"
                (response_dir / name).write_bytes(payload)
                return payload

            client = client or KoshaMsdsClient(
                os.environ.get("KOSHA_API_SERVICE_KEY", ""),
                max_retries=2,
                request_interval_seconds=1,
                fetch_xml=fetch,
            )
            records, results = [], []
            for cas in spec["cas_numbers"]:
                try:
                    result = client.collect_cas(cas, sections=tuple(range(1, 17)))
                except KoshaApiError as error:
                    state["collection"] = {
                        "mode": "api",
                        "source": KOSHA_SOURCE_PAGE,
                        "collected_at": now(),
                        "request_count": client.request_count,
                        "retry_count": client.retry_count,
                        "results": results
                        + [{"cas_number": cas, "status": "FAILED", "code": error.code}],
                        "partial_record_count": len(records),
                        "run_id": self.run_id,
                    }
                    write_csv(self.work / "partial.csv", KOSHA_STAGING_COLUMNS, records)
                    atomic_json(
                        self.work / "collection-failed.json", state["collection"]
                    )
                    raise

                records.extend(result.pop("records"))
                results.append(result)
                # A missing/ambiguous CAS is not a complete successful refresh.
                if result["status"] != "COLLECTED":
                    state["collection"] = {
                        "results": results,
                        "source": KOSHA_SOURCE_PAGE,
                        "collected_at": now(),
                        "request_count": client.request_count,
                        "retry_count": client.retry_count,
                    }
                    raise BatchError("INCOMPLETE_COLLECTION")
            write_csv(incoming, KOSHA_STAGING_COLUMNS, records)
            state["collection"] = {
                "mode": "api",
                "source": KOSHA_SOURCE_PAGE,
                "collected_at": now(),
                "request_count": client.request_count,
                "retry_count": client.retry_count,
                "results": results,
            }
        state["raw_sha256"] = sha256_file(incoming)

    def _archive(self, state, **_):
        if sha256_file(self.work / "incoming.csv") != state["raw_sha256"]:
            raise BatchError("RAW_HASH_MISMATCH")
        state["collection"].update(
            {
                "sha256": state["raw_sha256"],
                "run_id": self.run_id,
                "schema": SCHEMA,
                "source_version": state["raw_sha256"],
            }
        )
        atomic_json(self.work / "collection.json", state["collection"])
        state["raw_response_hashes"] = tree_hashes(self.work / "responses")

    def _normalize(self, state, **_):
        if sha256_file(self.work / "incoming.csv") != state["raw_sha256"]:
            raise BatchError("RAW_HASH_MISMATCH")
        accepted, excluded, seen = [], [], set()
        source = self.work / "incoming.csv"
        try:
            with source.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle, strict=True)
                if not set(REQUIRED).issubset(reader.fieldnames or []):
                    raise BatchError("REQUIRED_COLUMNS_MISSING")
                input_rows = list(reader)
                parents = {
                    (
                        normalize_cas(r.get("CAS번호")),
                        r.get("화학물질ID"),
                        r.get("MSDS_장번호"),
                        r.get("상위항목코드"),
                    )
                    for r in input_rows
                    if r.get("상위항목코드")
                }
                for number, raw in enumerate(input_rows, 2):
                    reasons = []
                    if None in raw or any(v is None for v in raw.values()):
                        reasons.append("PARSE_ROW_SHAPE")
                    row = {
                        k: (v or "").strip() for k, v in raw.items() if k is not None
                    }
                    row["CAS번호"] = normalize_cas(row.get("CAS번호"))
                    if not row["CAS번호"]:
                        reasons.append("CAS_MISSING")
                    elif not valid_cas_checksum(row["CAS번호"]):
                        reasons.append("CAS_INVALID_FORMAT_OR_CHECKSUM")
                    for field in REQUIRED:
                        if field not in {"CAS번호", "상세내용"} and not row.get(field):
                            reasons.append("REQUIRED_MISSING:" + field)
                    if row.get("MSDS_장번호") not in {str(n) for n in range(1, 17)}:
                        reasons.append("SECTION_INVALID")
                    if not row.get("상세내용"):
                        parent_key = (
                            row["CAS번호"],
                            row.get("화학물질ID"),
                            row.get("MSDS_장번호"),
                            row.get("MSDS_항목코드"),
                        )
                        reasons.append(
                            "STRUCTURAL_PARENT_NO_DETAIL"
                            if row.get("MSDS_항목코드") and parent_key in parents
                            else "EMPTY_LEAF_DETAIL"
                        )
                    if compact_text(row.get("상세내용")) in NO_INFORMATION:
                        reasons.append("NO_INFORMATION")
                    key = row.get("레코드ID")
                    if key in seen:
                        reasons.append("DUPLICATE_RECORD_ID")
                    if key:
                        seen.add(key)
                    if reasons:
                        excluded.append({"line": number, "reasons": reasons})
                    else:
                        accepted.append(row)
        except (csv.Error, UnicodeError):
            # Cannot reliably enumerate records in malformed CSV: block entire file.
            state["parse_failure"] = "FILE_PARSE_ERROR_INPUT_COUNT_UNKNOWN"
            atomic_json(
                self.work / "exclusions.json", {"file_error": state["parse_failure"]}
            )
            raise BatchError("FILE_PARSE_ERROR") from None
        atomic_json(self.work / "normalized.json", accepted)
        atomic_json(self.work / "exclusions.json", excluded)
        counts = {
            "input": len(accepted) + len(excluded),
            "normal": len(accepted),
            "excluded": len(excluded),
            "stored": 0,
        }
        reasons = {}
        for item in excluded:
            for reason in item["reasons"]:
                reasons[reason] = reasons.get(reason, 0) + 1
        state["counts"], state["exclusion_reasons"] = counts, reasons
        expected_codes = {"STRUCTURAL_PARENT_NO_DETAIL", "NO_INFORMATION"}
        expected_rows = sum(
            set(item["reasons"]).issubset(expected_codes) for item in excluded
        )
        state["quality_summary"] = {
            "expected_excluded": expected_rows,
            "error_excluded": len(excluded) - expected_rows,
            "error_fraction": (len(excluded) - expected_rows) / counts["input"]
            if counts["input"]
            else None,
            "expected_fraction": expected_rows / counts["input"]
            if counts["input"]
            else None,
        }

    def _validate(self, state, **_):
        if (
            tree_hashes(self.work / "baseline") != state["input_hashes"]["baseline"]
            or tree_hashes(self.work / "config") != state["input_hashes"]["config"]
        ):
            raise BatchError("PINNED_INPUT_HASH_MISMATCH")
        counts = state["counts"]
        spec = state["spec"]
        maximum = float(
            spec.get("max_error_fraction", spec.get("max_excluded_fraction", 0.0))
        )
        expected_limit = float(spec.get("expected_exclusion_review_threshold", 0.25))
        drift_limit = float(spec.get("count_change_threshold", 0.25))
        if (
            not 0 <= maximum <= 1
            or not 0 <= drift_limit <= 1
            or not 0 <= expected_limit <= 1
        ):
            raise BatchError("THRESHOLD_INVALID")
        if (
            counts["input"] != counts["normal"] + counts["excluded"]
            or not counts["normal"]
        ):
            raise BatchError("COUNT_CONSERVATION_FAILED_OR_EMPTY")
        quality = state["quality_summary"]
        if (
            counts["excluded"]
            != quality["expected_excluded"] + quality["error_excluded"]
        ):
            raise BatchError("EXCLUSION_CLASS_CONSERVATION_FAILED")
        quality["review_required"] = quality["expected_fraction"] > expected_limit
        if quality["error_fraction"] > maximum:
            raise BatchError("EXCLUSION_THRESHOLD_EXCEEDED")
        rows = read(self.work / "normalized.json")
        expected = {normalize_cas(c) for c in spec["cas_numbers"]}
        found = {row["CAS번호"] for row in rows}
        if expected != found:
            raise BatchError("TARGET_CAS_COVERAGE_MISMATCH")
        previous = state["expected_current"]
        old = (
            sum(
                row["CAS번호"] in expected
                for row in read(
                    self.root / "versions" / previous["version"] / "normalized.json"
                )
            )
            if previous
            else None
        )
        drift = abs(counts["normal"] - old) / old if old else None
        state["count_change"] = {
            "baseline": old,
            "relative_change": drift,
            "threshold": drift_limit,
            "review_required": drift is not None and drift > drift_limit,
        }
        # Local candidate overlay: no inferred source deletion. Conflicting keys block.
        baseline = self.work / "baseline" / preprocessing.SOURCE_FILES["kosha"]
        existing = rows_from(baseline)
        by_id = {row["레코드ID"]: row for row in existing}
        if len(by_id) != len(existing):
            raise BatchError("BASELINE_DUPLICATE_KEY")
        reused = 0
        for row in rows:
            key = row["레코드ID"]
            if key in by_id:
                old_row = by_id[key]
                for field in REQUIRED:
                    before = (
                        normalize_cas(old_row[field])
                        if field == "CAS번호"
                        else old_row[field].strip()
                    )
                    if before != row[field]:
                        raise BatchError("BASELINE_RECORD_ID_CONTENT_CONFLICT")
                reused += 1
            else:
                by_id[key] = row
        state["counts"].update(
            {
                "baseline": len(existing),
                "reused": reused,
                "added": len(rows) - reused,
                "merged": len(by_id),
            }
        )
        if len(by_id) != len(existing) + len(rows) - reused:
            raise BatchError("MERGE_CONSERVATION_FAILED")
        merged = self.work / "merged"
        if merged.exists():
            shutil.rmtree(merged)
        shutil.copytree(self.work / "baseline", merged)
        columns = tuple(dict.fromkeys(k for row in by_id.values() for k in row))
        write_csv(
            merged / preprocessing.SOURCE_FILES["kosha"],
            columns,
            [{k: row.get(k, "") for k in columns} for row in by_id.values()],
        )
        state["validated_hashes"] = {
            "merged": tree_hashes(merged),
            "normalized": sha256_file(self.work / "normalized.json"),
        }
        state["quality_policy"] = {
            "max_error_fraction": maximum,
            "expected_exclusion_review_threshold": expected_limit,
            "count_change_threshold": drift_limit,
        }
        state["version"] = digest(
            {
                "schema": SCHEMA,
                "quality_policy": state["quality_policy"],
                "runtime_versions": state["runtime_versions"],
                "raw": state["raw_sha256"],
                "inputs": state["input_hashes"],
                "code": state["code_hashes"],
                "cas": sorted(expected),
            }
        )

    def _stage(self, state, *, fault, **_):
        from chemiguard119.release import create_runtime_manifest
        from chemiguard119.resolver import train_resolver
        from chemiguard119.retrieval import train_retriever

        if {
            p.name: sha256_file(p) for p in sorted(Path(__file__).parent.glob("*.py"))
        } != state["code_hashes"]:
            raise BatchError("PROCESSING_CODE_CHANGED_RESTART_WITH_NEW_RUN")
        if {
            name: package_version(name) for name in ("numpy", "scikit-learn", "joblib")
        } != state["runtime_versions"]:
            raise BatchError("PROCESSING_RUNTIME_CHANGED_RESTART_WITH_NEW_RUN")
        if (
            tree_hashes(self.work / "config") != state["input_hashes"]["config"]
            or sha256_file(self.work / "incoming.csv") != state["raw_sha256"]
        ):
            raise BatchError("PINNED_INPUT_HASH_MISMATCH")

        if (
            tree_hashes(self.work / "merged") != state["validated_hashes"]["merged"]
            or sha256_file(self.work / "normalized.json")
            != state["validated_hashes"]["normalized"]
        ):
            raise BatchError("VALIDATED_INPUT_HASH_MISMATCH")
        target = self.root / "versions" / state["version"]
        if target.exists():
            verify_bundle(self.root, state["version"])
            saved = read(target / "batch_manifest.json")
            state["counts"] = saved["counts"]
            state["bundle_sha256"] = sha256_file(target / "bundle.json")
            return
        pending = self.work / "staging"
        if pending.exists():
            shutil.rmtree(pending)
        pending.mkdir()
        shutil.copytree(self.work / "config", pending / "config")
        shutil.copytree(self.work / "merged", pending / "source_snapshot")
        db_path = pending / preprocessing.DEFAULT_DB_FILE
        preprocessing.prepare_dataset(self.work / "merged", pending / "config", pending)
        if fault == "stage":
            raise BatchError("INJECTED_STAGING_FAILURE")
        train_resolver(db_path, pending / "resolver.joblib")
        train_retriever(db_path, pending / "retriever.joblib")
        create_runtime_manifest(
            db_path=db_path,
            resolver_model_path=pending / "resolver.joblib",
            retriever_model_path=pending / "retriever.joblib",
            config_dir=pending / "config",
        )
        with sqlite3.connect(db_path) as db:
            stored = db.execute(
                "SELECT count(*) FROM evidence WHERE source='KOSHA'"
            ).fetchone()[0]
            base_rows = rows_from(
                self.work / "baseline" / preprocessing.SOURCE_FILES["kosha"]
            )
            base_excluded = sum(
                not r.get("상세내용", "").strip()
                or compact_text(r.get("상세내용")) in NO_INFORMATION
                for r in base_rows
            )
            if stored != state["counts"]["merged"] - base_excluded:
                raise BatchError("STORED_COUNT_MISMATCH")
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise BatchError("FOREIGN_KEY_CHECK_FAILED")
        state["counts"].update(
            {"stored": stored, "baseline_detail_excluded": base_excluded}
        )
        for name in (
            "incoming.csv",
            "collection.json",
            "normalized.json",
            "exclusions.json",
        ):
            shutil.copyfile(self.work / name, pending / name)
        for name in ("baseline", "responses"):
            if (self.work / name).exists():
                shutil.copytree(self.work / name, pending / name)
        atomic_json(
            pending / "batch_manifest.json",
            {
                "schema": SCHEMA,
                "version": state["version"],
                "run_id": self.run_id,
                "counts": state["counts"],
                "quality_policy": state["quality_policy"],
                "runtime_versions": state["runtime_versions"],
                "count_change": state["count_change"],
                "input_hashes": state["input_hashes"],
                "raw_sha256": state["raw_sha256"],
                "code_hashes": state["code_hashes"],
                "built_at": now(),
                "scope": "LOCAL_DEVELOPMENT_ONLY",
                "deletion_semantics": "APPEND_ONLY_NO_SOURCE_DELETION",
                "exclusion_reasons": state["exclusion_reasons"],
                "quality_summary": state["quality_summary"],
            },
        )
        atomic_json(pending / "bundle.json", tree_hashes(pending))
        target.parent.mkdir(exist_ok=True)
        os.replace(pending, target)
        state["bundle_sha256"] = sha256_file(target / "bundle.json")

    def check_activation_review(self, state):
        target = verify_bundle(self.root, state["version"])
        approval_path = self.root / "approvals" / f"{state['version']}.json"
        if not approval_path.exists():
            raise BatchError("HUMAN_REVIEW_REQUIRED")
        approval = read(approval_path)
        if (
            approval.get("bundle_sha256") != sha256_file(target / "bundle.json")
            or not approval.get("reviewer")
            or not approval.get("basis")
            or approval.get("scope") != "LOCAL_DEVELOPMENT_ONLY"
        ):
            raise BatchError("APPROVAL_INVALID_OR_STALE")
        if (
            state["count_change"]["review_required"]
            and approval.get("accept_count_change") is not True
        ):
            raise BatchError("COUNT_CHANGE_REVIEW_REQUIRED")
        if (
            state.get("quality_summary", {}).get("review_required")
            and approval.get("accept_expected_exclusions") is not True
        ):
            raise BatchError("EXPECTED_EXCLUSIONS_REVIEW_REQUIRED")
        return target

    def _activate(self, state, *, fault, **_):
        target = self.check_activation_review(state)
        active = current(self.root)
        if active and active["version"] == state["version"]:
            return
        if active != state["expected_current"]:
            raise BatchError("CONCURRENT_ACTIVATION_BASELINE_CHANGED")
        if active:
            previous_paths = active_runtime_paths(self.root)
            with (
                sqlite3.connect(previous_paths["db_path"]) as before,
                sqlite3.connect(target / preprocessing.DEFAULT_DB_FILE) as after,
            ):
                # Append-only contract must also hold across successive baselines.
                previous_rows = set(
                    before.execute("SELECT evidence_id, body FROM evidence")
                )
                next_rows = set(after.execute("SELECT evidence_id, body FROM evidence"))
                if not previous_rows.issubset(next_rows):
                    raise BatchError("BASELINE_WOULD_LOSE_ACTIVE_EVIDENCE")
        if fault == "activate":
            raise BatchError("INJECTED_ACTIVATION_FAILURE")
        atomic_json(
            self.root / "current.json",
            {
                "version": state["version"],
                "bundle_sha256": state["bundle_sha256"],
                "activation_id": uuid.uuid4().hex,
                "run_id": self.run_id,
                "at": now(),
                "scope": "LOCAL_DEVELOPMENT_ONLY",
            },
        )

    def finish(self):
        with locked(self.root):
            state = (
                self.state()
                if self.state_path.exists()
                else {
                    "done": [],
                    "failure": {
                        "step": "initialize",
                        "code": "INITIALIZATION_FAILED_NO_STATE",
                    },
                }
            )
            status = (
                "SUCCESS"
                if "activate" in state["done"] and not state.get("failure")
                else "STAGED"
                if "stage" in state["done"] and not state.get("failure")
                else "FAILED"
            )
            summary = {
                "run_id": self.run_id,
                "status": status,
                "counts": state.get("counts", {}),
                "failure": state.get("failure"),
                "version": state.get("version"),
                "finished_at": now(),
            }
            atomic_json(self.work / "result.json", summary)
            event(
                self.root,
                self.run_id,
                "result",
                status,
                (now(), time.monotonic()),
                counts=summary["counts"],
                failure=summary["failure"],
                version=summary["version"],
            )
            return summary


def verify_bundle(root, version):
    if len(version) != 64 or any(c not in "0123456789abcdef" for c in version):
        raise BatchError("VERSION_INVALID")
    target = Path(root) / "versions" / version
    expected = read(target / "bundle.json")
    actual = tree_hashes(target)
    actual.pop("bundle.json", None)
    if actual != expected:
        raise BatchError("BUNDLE_HASH_MISMATCH")
    return target


def approve(
    root,
    version,
    reviewer,
    basis,
    accept_count_change=False,
    accept_expected_exclusions=False,
):
    with locked(root):
        target = verify_bundle(root, version)
        if not reviewer.strip() or not basis.strip():
            raise BatchError("REVIEWER_AND_BASIS_REQUIRED")
        path = Path(root) / "approvals" / f"{version}.json"
        if path.exists():
            previous = read(path)
            atomic_json(
                Path(root) / "review_history" / f"{version}-{uuid.uuid4().hex}.json",
                previous,
            )
        atomic_json(
            path,
            {
                "version": version,
                "bundle_sha256": sha256_file(target / "bundle.json"),
                "reviewer": reviewer,
                "basis": basis,
                "reviewed_at": now(),
                "accept_count_change": accept_count_change,
                "accept_expected_exclusions": accept_expected_exclusions,
                "scope": "LOCAL_DEVELOPMENT_ONLY",
            },
        )


def rollback(root, version):
    started = (now(), time.monotonic())
    with locked(root):
        target = verify_bundle(root, version)
        approval = read(Path(root) / "approvals" / f"{version}.json")
        if approval.get("bundle_sha256") != sha256_file(target / "bundle.json"):
            raise BatchError("ROLLBACK_APPROVAL_INVALID")
        with sqlite3.connect(Path(root) / "runs.sqlite") as db:
            history = db.execute(
                "SELECT details FROM event WHERE step='result' AND status='SUCCESS'"
            ).fetchall()
        if not any(json.loads(row[0]).get("version") == version for row in history):
            raise BatchError("ROLLBACK_REQUIRES_PREVIOUS_SUCCESS")
        previous = current(root)
        atomic_json(
            Path(root) / "current.json",
            {
                "version": version,
                "bundle_sha256": sha256_file(target / "bundle.json"),
                "activation_id": uuid.uuid4().hex,
                "at": now(),
                "scope": "LOCAL_DEVELOPMENT_ONLY",
            },
        )
        event(
            root,
            "rollback-" + uuid.uuid4().hex,
            "rollback",
            "SUCCESS",
            started,
            previous=previous,
            version=version,
        )


def active_runtime_paths(root):
    """Resolve once per runtime start; pin DB, indexes and config to the same bundle.

    Existing ModelRuntime caches models; never hot-swap just its DB pathname.
    Pass these kwargs to ModelRuntime.load(environment='development').
    """
    pointer = current(root)
    if not pointer:
        raise BatchError("NO_ACTIVE_VERSION")
    target = verify_bundle(root, pointer["version"])
    if sha256_file(target / "bundle.json") != pointer["bundle_sha256"]:
        raise BatchError("ACTIVE_POINTER_HASH_MISMATCH")
    return {
        "db_path": target / preprocessing.DEFAULT_DB_FILE,
        "resolver_model_path": target / "resolver.joblib",
        "retriever_model_path": target / "retriever.joblib",
        "config_dir": target / "config",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--run-id", required=True)
    run.add_argument("--spec", type=Path)
    run.add_argument("--through", choices=STEPS, default="stage")
    review = sub.add_parser("approve")
    review.add_argument("--version", required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--basis", required=True)
    review.add_argument("--accept-count-change", action="store_true")
    review.add_argument("--accept-expected-exclusions", action="store_true")
    restore = sub.add_parser("rollback")
    restore.add_argument("--version", required=True)
    replay = sub.add_parser("reprocess")
    replay.add_argument("--version", required=True)
    replay.add_argument("--run-id", required=True)
    replay.add_argument("--through", choices=STEPS, default="stage")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8001)
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command in {"run", "reprocess"}:
        pipeline = Pipeline(args.root, args.run_id)
        if args.command == "reprocess":
            target = verify_bundle(args.root, args.version)
            manifest = read(target / "batch_manifest.json")
            pipeline.initialize(
                {
                    "baseline_dir": str(target / "baseline"),
                    "config_dir": str(target / "config"),
                    "reprocess_version": args.version,
                    "cas_numbers": sorted(
                        {row["CAS번호"] for row in read(target / "normalized.json")}
                    ),
                    **manifest["quality_policy"],
                }
            )
        elif args.spec:
            pipeline.initialize(read(args.spec))
        try:
            for step in STEPS[: STEPS.index(args.through) + 1]:
                pipeline.execute(step)
        finally:
            result = pipeline.finish()
            print(json.dumps(result, ensure_ascii=False))
    elif args.command == "approve":
        approve(
            args.root,
            args.version,
            args.reviewer,
            args.basis,
            args.accept_count_change,
            args.accept_expected_exclusions,
        )
    elif args.command == "rollback":
        rollback(args.root, args.version)
    elif args.command == "serve":
        if (
            os.getenv("CHEMIGUARD119_ENVIRONMENT", "development").strip().lower()
            != "development"
        ):
            raise BatchError("LOCAL_SERVER_REQUIRES_DEVELOPMENT_ENVIRONMENT")
        import uvicorn
        from chemiguard119.api import ModelRuntime, create_app

        runtime = ModelRuntime.load(
            **active_runtime_paths(args.root), environment="development"
        )
        uvicorn.run(
            create_app(runtime=runtime, deployment_environment="development"),
            host="127.0.0.1",
            port=args.port,
        )
    else:
        with sqlite3.connect(args.root / "runs.sqlite") as db:
            db.row_factory = sqlite3.Row
            print(
                json.dumps(
                    {
                        "current": current(args.root),
                        "events": [
                            dict(row)
                            for row in db.execute("SELECT * FROM event ORDER BY id")
                        ],
                    },
                    ensure_ascii=False,
                )
            )


if __name__ == "__main__":
    main()
