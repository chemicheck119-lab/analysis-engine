from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from chemiguard119 import preprocessing


KOSHA_COLUMNS = (
    "레코드ID",
    "화학물질ID",
    "CAS번호",
    "화학물질명_국문",
    "MSDS_장번호",
    "MSDS_항목명_국문",
    "상세내용",
    "최종개정일",
    "시나리오역할",
    "검색기준_화학물질명",
    "UN번호",
    "자료출처",
)

CAMEO_COLUMNS = (
    "CAMEO_화학물질ID",
    "화학물질명_원문",
    "반응성그룹수",
    "원본_상세URL",
    "출처버전",
    *(column for column, _ in preprocessing.CAMEO_BODY_FIELDS),
)

FACILITY_COLUMNS = tuple(sorted(preprocessing.FACILITY_REQUIRED_COLUMNS))

ICIS_COLUMNS = (
    "레코드ID",
    "조사연도",
    "화학물질명_원문",
    "CAS번호_정규화",
    "CAS체크섬유효",
    "자료성격",
    "현재보유확정여부",
    "원본데이터셋_URL",
)


def _write_csv(
    path: Path, columns: tuple[str, ...], rows: list[dict[str, object]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _patch_expected_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preprocessing, "EXPECTED_KOSHA_SUBSTANCE_COUNT", 2)
    monkeypatch.setattr(preprocessing, "EXPECTED_ICIS_VALID_CAS_COUNT", 2)
    monkeypatch.setattr(preprocessing, "EXPECTED_ICIS_SPLIT_ALIAS_COUNT", 4)
    monkeypatch.setattr(preprocessing, "EXPECTED_CAMEO_CHEMICAL_COUNT", 2)
    monkeypatch.setattr(preprocessing, "EXPECTED_REACTIVE_GROUP_COUNT", 2)
    monkeypatch.setattr(preprocessing, "EXPECTED_CAMEO_MAPPING_COUNT", 2)
    monkeypatch.setattr(preprocessing, "EXPECTED_COMPATIBILITY_PAIR_COUNT", 3)


def _facility_row(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {column: "" for column in FACILITY_COLUMNS}
    row.update(
        {
            "레코드ID": "FAC-1",
            "기준매핑_레코드ID": "MAP-1",
            "조사연도": "2024",
            "CAS번호": "7647-01-0",
            "화학물질명_선정기준": "염화수소",
            "업체명": "가나다화학",
            "주소": "울산광역시 남구 산업로 1",
            "시도명": "울산광역시",
            "업종": "화학제품 제조업",
            "선정근거": "ICIS 과거 취급 이력",
            "울산소방_사고자료행수": "2",
            "CAS검색검증상태": "EXACT",
            "정확CAS모델링사용여부": "Y",
            "KOSHA_MSDS_CAS정확매칭": "Y",
            "PRTR_업체_업체명주소정확매칭": "N",
            "PRTR_물질_CAS정확매칭": "N",
            "현재보유확정여부": "Y",
            "원본데이터셋_URL": "https://example.test/icis",
            "모델출력용도": "현장 확인 후보 설명",
            "안전제약": "현재 재고로 간주하지 않음",
        }
    )
    row.update(updates)
    return row


def _make_fixture(root: Path) -> tuple[Path, Path, Path]:
    data_dir = root / "data"
    config_dir = root / "config"
    artifact_dir = root / "artifacts"

    _write_csv(
        data_dir / preprocessing.SOURCE_FILES["kosha"],
        KOSHA_COLUMNS,
        [
            {
                "레코드ID": "KOSHA-HCL-1",
                "화학물질ID": "K-HCL",
                "CAS번호": "7647-01-0",
                "화학물질명_국문": "염화수소",
                "MSDS_장번호": "1",
                "MSDS_항목명_국문": "화학제품과 회사에 관한 정보",
                "상세내용": "염화수소 누출 시 바람을 등지고 접근한다.",
                "최종개정일": "2024-01-01",
                "시나리오역할": "acid",
                "검색기준_화학물질명": "염산",
                "UN번호": "1050",
                "자료출처": "https://example.test/kosha/hcl",
            },
            {
                "레코드ID": "KOSHA-NAOCL-1",
                "화학물질ID": "K-NAOCL",
                "CAS번호": "7681-52-9",
                "화학물질명_국문": "차아염소산나트륨",
                "MSDS_장번호": "10",
                "MSDS_항목명_국문": "안정성 및 반응성",
                "상세내용": "산과 접촉하면 chlorine gas가 발생할 수 있다.",
                "최종개정일": "2024-01-02",
                "시나리오역할": "oxidizer",
                "검색기준_화학물질명": "차아염소산나트륨",
                "UN번호": "1791",
                "자료출처": "https://example.test/kosha/naocl",
            },
            {
                "레코드ID": "KOSHA-NAOCL-BLANK",
                "화학물질ID": "K-NAOCL",
                "CAS번호": "7681-52-9",
                "화학물질명_국문": "차아염소산나트륨",
                "MSDS_장번호": "16",
                "MSDS_항목명_국문": "그 밖의 참고사항",
                "상세내용": "  ",
                "최종개정일": "2024-01-02",
                "시나리오역할": "oxidizer",
                "검색기준_화학물질명": "차아염소산나트륨",
                "UN번호": "1791",
                "자료출처": "https://example.test/kosha/naocl",
            },
            {
                "레코드ID": "KOSHA-HCL-NOINFO",
                "화학물질ID": "K-HCL",
                "CAS번호": "7647-01-0",
                "화학물질명_국문": "염화수소",
                "MSDS_장번호": "16",
                "MSDS_항목명_국문": "그 밖의 참고사항",
                "상세내용": "|자료없음|",
                "최종개정일": "2024-01-01",
                "시나리오역할": "acid",
                "검색기준_화학물질명": "염산",
                "UN번호": "1050",
                "자료출처": "https://example.test/kosha/hcl",
            },
        ],
    )

    _write_csv(
        data_dir / preprocessing.SOURCE_FILES["cameo_chemical"],
        CAMEO_COLUMNS,
        [
            {
                "CAMEO_화학물질ID": "3598",
                "화학물질명_원문": "HYDROCHLORIC ACID",
                "반응성그룹수": "1",
                "원본_상세URL": "https://example.test/cameo/3598",
                "출처버전": "2024",
                "물질설명": "A corrosive acid.",
                "비화재사고대응": "Isolate the spill area.",
            },
            {
                "CAMEO_화학물질ID": "4503",
                "화학물질명_원문": "SODIUM HYPOCHLORITE",
                "반응성그룹수": "1",
                "원본_상세URL": "https://example.test/cameo/4503",
                "출처버전": "2024",
                "화학반응성_상세": "Contact with acid may release chlorine.",
                "특수위험": "Toxic gas generation.",
            },
        ],
    )

    _write_csv(
        data_dir / preprocessing.SOURCE_FILES["cameo_mapping"],
        ("CAMEO_화학물질ID", "반응성그룹ID", "반응성그룹명_원문"),
        [
            {
                "CAMEO_화학물질ID": "3598",
                "반응성그룹ID": "1",
                "반응성그룹명_원문": "Acids, Strong Non-oxidizing",
            },
            {
                "CAMEO_화학물질ID": "4503",
                "반응성그룹ID": "8",
                "반응성그룹명_원문": "Oxidizing Agents, Strong",
            },
        ],
    )

    _write_csv(
        data_dir / preprocessing.SOURCE_FILES["cameo_group"],
        ("반응성그룹ID", "반응성그룹명_원문"),
        [
            {
                "반응성그룹ID": "1",
                "반응성그룹명_원문": "Acids, Strong Non-oxidizing",
            },
            {
                "반응성그룹ID": "8",
                "반응성그룹명_원문": "Oxidizing Agents, Strong",
            },
        ],
    )

    _write_csv(
        data_dir / preprocessing.SOURCE_FILES["compatibility"],
        (
            "고유조합ID",
            "그룹A_ID",
            "그룹B_ID",
            "호환성_판정",
            "호환성_클래스ID",
            "위험코드",
            "위험문구",
            "발생가스",
            "원본URL",
        ),
        [
            {
                "고유조합ID": "P-1-1",
                "그룹A_ID": "1",
                "그룹B_ID": "1",
                "호환성_판정": "compatible",
                "호환성_클래스ID": "C",
                "원본URL": "https://example.test/cameo/1/1",
            },
            {
                "고유조합ID": "P-1-8",
                "그룹A_ID": "1",
                "그룹B_ID": "8",
                "호환성_판정": "incompatible",
                "호환성_클래스ID": "I",
                "위험코드": "G",
                "위험문구": "독성 가스 발생 가능",
                "발생가스": "Chlorine",
                "원본URL": "https://example.test/cameo/1/8",
            },
            {
                "고유조합ID": "P-8-8",
                "그룹A_ID": "8",
                "그룹B_ID": "8",
                "호환성_판정": "caution",
                "호환성_클래스ID": "W",
                "원본URL": "https://example.test/cameo/8/8",
            },
        ],
    )

    _write_csv(
        data_dir / preprocessing.SOURCE_FILES["ulsan_substance"],
        ("CAS번호", "화학물질명_한글", "화학물질명_영문"),
        [
            {
                "CAS번호": "7647-01-0",
                "화학물질명_한글": "염산가스",
                "화학물질명_영문": "Hydrogen chloride",
            },
            {
                "CAS번호": "7681-52-9",
                "화학물질명_한글": "미확인 세정제 원액",
                "화학물질명_영문": "Sodium hypochlorite",
            },
            {
                "CAS번호": "2024-01-01",
                "화학물질명_한글": "날짜형 잘못된 CAS",
                "화학물질명_영문": "Invalid CAS alias",
            },
            {
                "CAS번호": "7732-18-5",
                "화학물질명_한글": "물",
                "화학물질명_영문": "Water",
            },
        ],
    )

    _write_csv(
        data_dir / preprocessing.SOURCE_FILES["icis_material"],
        ICIS_COLUMNS,
        [
            {
                "레코드ID": "ICIS-HCL",
                "조사연도": "2024",
                "화학물질명_원문": "염화수소; 염산",
                "CAS번호_정규화": "7647-01-0",
                "CAS체크섬유효": "Y",
                "자료성격": "2024년 공개 요약",
                "현재보유확정여부": "N",
                "원본데이터셋_URL": "https://example.test/icis/materials",
            },
            {
                "레코드ID": "ICIS-WATER",
                "조사연도": "2024",
                "화학물질명_원문": "물; Water",
                "CAS번호_정규화": "7732-18-5",
                "CAS체크섬유효": "Y",
                "자료성격": "2024년 공개 요약",
                "현재보유확정여부": "N",
                "원본데이터셋_URL": "https://example.test/icis/materials",
            },
            {
                "레코드ID": "ICIS-INVALID",
                "조사연도": "2024",
                "화학물질명_원문": "체크섬 오류 물질",
                "CAS번호_정규화": "540-86-5",
                "CAS체크섬유효": "N",
                "자료성격": "2024년 공개 요약",
                "현재보유확정여부": "N",
                "원본데이터셋_URL": "https://example.test/icis/materials",
            },
        ],
    )

    _write_csv(
        data_dir / preprocessing.SOURCE_FILES["facility_candidate"],
        FACILITY_COLUMNS,
        [
            _facility_row(),
            _facility_row(
                레코드ID="FAC-2",
                기준매핑_레코드ID="MAP-2",
                CAS번호="7681-52-9",
                화학물질명_선정기준="차아염소산나트륨",
                업체명="라마바전자",
                주소="울산광역시 북구 산업로 2",
                현재보유확정여부="N",
                PRTR_업체_업체명주소정확매칭="Y",
                PRTR_물질_CAS정확매칭="Y",
                PRTR_시설전체_총배출량_kg_년="12.5",
                PRTR_시설전체_자가매립량_kg_년="0",
                PRTR_시설전체_총이동량_kg_년="3",
                PRTR_시설전체_보고흐름합계_kg_년="15.5",
                PRTR_해당물질_배출업체수="7",
                PRTR_해당물질_전국총배출량_kg_년="100.25",
                PRTR_해당물질_전국자가매립량_kg_년="5",
                PRTR_해당물질_전국총이동량_kg_년="20",
            ),
            _facility_row(
                레코드ID="FAC-EXCLUDED",
                정확CAS모델링사용여부="N",
                업체명="제외업체",
            ),
        ],
    )

    _write_csv(
        config_dir / preprocessing.OVERRIDE_FILE,
        (
            "cas_number",
            "canonical_name_ko",
            "canonical_name_en",
            "formula",
            "un_number",
            "scenario_role",
            "aliases",
        ),
        [
            {
                "cas_number": "7647-01-0",
                "canonical_name_ko": "염화수소",
                "canonical_name_en": "Hydrogen chloride",
                "formula": "HCl",
                "un_number": "1050",
                "scenario_role": "acid",
                "aliases": "염산|Hydrochloric acid",
            },
            {
                "cas_number": "7681-52-9",
                "canonical_name_ko": "차아염소산나트륨",
                "canonical_name_en": "Sodium hypochlorite",
                "formula": "NaOCl",
                "un_number": "1791",
                "scenario_role": "oxidizer",
                "aliases": "상용표백제|차염",
            },
        ],
    )
    _write_csv(
        config_dir / preprocessing.CROSSWALK_FILE,
        (
            "cas_number",
            "cameo_chemical_id",
            "selected_form",
            "verification_status",
            "evidence_url",
            "notes",
        ),
        [
            {
                "cas_number": "7647-01-0",
                "cameo_chemical_id": "3598",
                "selected_form": "HYDROCHLORIC ACID",
                "verification_status": "PUBLIC_SOURCE_VERIFIED",
                "evidence_url": "https://example.test/crosswalk/3598",
                "notes": "공개 출처에서 CAS와 물질 형태를 확인",
            },
            {
                "cas_number": "7681-52-9",
                "cameo_chemical_id": "4503",
                "selected_form": "SODIUM HYPOCHLORITE",
                "verification_status": "CANDIDATE_UNVERIFIED",
                "evidence_url": "https://example.test/crosswalk/4503",
                "notes": "이름 기반 후보이므로 스크리닝 입력 금지",
            },
        ],
    )
    return data_dir, config_dir, artifact_dir


def test_prepare_dataset_builds_safe_lookup_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_expected_counts(monkeypatch)
    data_dir, config_dir, artifact_dir = _make_fixture(tmp_path)

    manifest = preprocessing.prepare_dataset(data_dir, config_dir, artifact_dir)

    db_path = artifact_dir / preprocessing.DEFAULT_DB_FILE
    feature_path = artifact_dir / preprocessing.FEATURE_FILE
    manifest_path = artifact_dir / preprocessing.MANIFEST_FILE
    assert db_path.is_file()
    assert feature_path.is_file()
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM substance").fetchone()[0] == 3
        assert (
            connection.execute("SELECT COUNT(*) FROM cameo_chemical").fetchone()[0] == 2
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM cameo_mapping").fetchone()[0] == 2
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM compatibility").fetchone()[0] == 3
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM facility_candidate").fetchone()[0]
            == 2
        )

        evidence_counts = dict(
            connection.execute("SELECT source, COUNT(*) FROM evidence GROUP BY source")
        )
        assert evidence_counts == {"CAMEO": 2, "KOSHA": 2}
        cameo_evidence_links = dict(
            connection.execute(
                "SELECT cameo_chemical_id, cas_number FROM evidence WHERE source = 'CAMEO'"
            )
        )
        assert cameo_evidence_links == {
            "3598": "7647-01-0",
            "4503": "7681-52-9",
        }
        cameo_link_statuses = dict(
            connection.execute(
                "SELECT cameo_chemical_id, cas_link_status FROM evidence WHERE source = 'CAMEO'"
            )
        )
        assert cameo_link_statuses == {
            "3598": "PUBLIC_SOURCE_VERIFIED",
            "4503": "CANDIDATE_UNVERIFIED",
        }
        assert (
            connection.execute("SELECT COUNT(*) FROM evidence_fts").fetchone()[0] == 4
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM evidence_fts WHERE evidence_fts MATCH 'chlorine'"
            ).fetchone()[0]
            >= 1
        )

        alias_texts = {
            row[0] for row in connection.execute("SELECT alias_text FROM alias")
        }
        assert {"염산", "상용표백제", "염산가스", "Hydrogen chloride"} <= alias_texts
        assert "날짜형 잘못된 CAS" not in alias_texts
        assert {"물", "Water"} <= alias_texts

        water_scope = connection.execute(
            """
            SELECT catalog_scope, has_kosha_detail, resolver_candidate_only
            FROM substance WHERE cas_number = '7732-18-5'
            """
        ).fetchone()
        assert water_scope == ("ICIS_PUBLIC_CATALOG_CANDIDATE", 0, 1)
        hcl_scope = connection.execute(
            """
            SELECT catalog_scope, has_kosha_detail, resolver_candidate_only
            FROM substance WHERE cas_number = '7647-01-0'
            """
        ).fetchone()
        assert hcl_scope == ("KOSHA_CORE_WITH_DETAIL", 1, 0)
        assert (
            connection.execute(
                "SELECT verification_status FROM alias WHERE alias_text = '물'"
            ).fetchone()[0]
            == "PUBLIC_CATALOG_CANDIDATE"
        )
        assert connection.execute(
            """
            SELECT source, verification_status, alias_type
            FROM alias
            WHERE cas_number = '7647-01-0' AND alias_text = '염산'
            """
        ).fetchone() == (
            preprocessing.SOURCE_FILES["kosha"],
            "SOURCE_EXACT",
            "search_name",
        )

        assert (
            connection.execute(
                "SELECT typeof(cameo_chemical_id) FROM cameo_chemical LIMIT 1"
            ).fetchone()[0]
            == "text"
        )
        assert (
            connection.execute(
                "SELECT typeof(reactive_group_id) FROM cameo_mapping LIMIT 1"
            ).fetchone()[0]
            == "text"
        )
        assert (
            connection.execute(
                "SELECT typeof(group_a_id) FROM compatibility LIMIT 1"
            ).fetchone()[0]
            == "text"
        )

        mapping_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(cameo_mapping)")
        }
        assert {
            "cameo_chemical_id",
            "reactive_group_id",
            "reactive_group_name",
        } <= mapping_columns
        compatibility_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(compatibility)")
        }
        assert {
            "pair_id",
            "group_a_id",
            "group_b_id",
            "compatibility_label",
            "compatibility_class_id",
            "hazard_codes",
            "hazard_text",
            "gases",
            "source_url",
        } <= compatibility_columns

        assert (
            connection.execute(
                "SELECT SUM(current_inventory_confirmed) FROM facility_candidate"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM facility_candidate "
                "WHERE evidence_class = 'REPORTED_HANDLING_HISTORY'"
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT prtr_facility_total_release_kg_year "
                "FROM facility_candidate WHERE candidate_id = 'FAC-1'"
            ).fetchone()[0]
            is None
        )

    with feature_path.open(encoding="utf-8-sig", newline="") as handle:
        features = list(csv.DictReader(handle))
    assert len(features) == 2
    blank_prtr_row = next(
        row for row in features if row["facility_name"] == "가나다화학"
    )
    assert blank_prtr_row["prtr_facility_total_release_kg_year"] == ""
    assert blank_prtr_row["current_inventory_confirmed"] == "0"

    assert manifest["counts"]["kosha_blank_details_excluded"] == 1
    assert manifest["counts"]["kosha_no_information_details_excluded"] == 1
    assert manifest["counts"]["cameo_evidence"] == 2
    assert manifest["counts"]["cameo_crosswalk_rows"] == 2
    assert manifest["counts"]["cameo_crosswalk_evidence_links"] == 2
    assert manifest["counts"]["icis_source_rows"] == 3
    assert manifest["counts"]["icis_valid_cas_rows"] == 2
    assert manifest["counts"]["icis_invalid_cas_rows"] == 1
    assert manifest["counts"]["icis_split_alias_rows"] == 4
    assert manifest["counts"]["icis_unique_aliases"] == 4
    assert manifest["counts"]["excluded_non_exact_cas_rows"] == 1
    assert manifest["counts"]["source_current_inventory_y_rows"] == 1
    assert manifest["safety_constraints"]["risk_level_training_included"] is False
    assert (
        manifest["safety_constraints"]["current_inventory_prediction_included"] is False
    )
    assert (
        manifest["safety_constraints"]["missing_prtr_values_imputed_to_zero"] is False
    )
    assert (
        manifest["safety_constraints"][
            "cameo_crosswalk_public_source_screening_enabled"
        ]
        is True
    )
    assert (
        manifest["safety_constraints"]["cameo_crosswalk_implies_expert_approval"]
        is False
    )
    assert (
        manifest["safety_constraints"]["icis_catalog_candidates_are_current_inventory"]
        is False
    )
    assert (
        manifest["safety_constraints"]["icis_catalog_aliases_used_for_rule_promotion"]
        is False
    )
    crosswalk_manifest = manifest["config_files"]["cameo_crosswalk"]
    assert crosswalk_manifest["sha256"]
    assert crosswalk_manifest["usage"] == "SEARCH_METADATA_AND_PUBLIC_SOURCE_SCREENING"
    assert crosswalk_manifest["verification_status_counts"] == {
        "CANDIDATE_UNVERIFIED": 1,
        "PUBLIC_SOURCE_VERIFIED": 1,
    }


def test_prepare_dataset_rejects_git_lfs_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_expected_counts(monkeypatch)
    data_dir, config_dir, artifact_dir = _make_fixture(tmp_path)
    pointer_path = data_dir / preprocessing.SOURCE_FILES["kosha"]
    pointer_path.write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:0123456789abcdef\n"
        "size 12345\n",
        encoding="utf-8",
    )

    with pytest.raises(FileNotFoundError, match="Git LFS"):
        preprocessing.prepare_dataset(data_dir, config_dir, artifact_dir)

    assert not (artifact_dir / preprocessing.DEFAULT_DB_FILE).exists()
    assert not (artifact_dir / preprocessing.MANIFEST_FILE).exists()


def test_prepare_dataset_failure_preserves_existing_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_expected_counts(monkeypatch)
    data_dir, config_dir, artifact_dir = _make_fixture(tmp_path)
    artifact_dir.mkdir(parents=True)
    db_path = artifact_dir / "existing.sqlite"
    db_path.write_bytes(b"original-database-bytes")

    mapping_path = data_dir / preprocessing.SOURCE_FILES["cameo_mapping"]
    _write_csv(
        mapping_path,
        ("CAMEO_화학물질ID", "반응성그룹ID", "반응성그룹명_원문"),
        [
            {
                "CAMEO_화학물질ID": "3598",
                "반응성그룹ID": "999",
                "반응성그룹명_원문": "Unknown group",
            },
            {
                "CAMEO_화학물질ID": "4503",
                "반응성그룹ID": "8",
                "반응성그룹명_원문": "Oxidizing Agents, Strong",
            },
        ],
    )

    with pytest.raises(
        preprocessing.PreprocessingError, match="알 수 없는 반응성 그룹"
    ):
        preprocessing.prepare_dataset(
            data_dir,
            config_dir,
            artifact_dir,
            db_path=db_path,
        )

    assert db_path.read_bytes() == b"original-database-bytes"
    assert not (artifact_dir / preprocessing.FEATURE_FILE).exists()
    assert not (artifact_dir / preprocessing.MANIFEST_FILE).exists()
