-- Each query has exactly one scalar result; zero means no defect, except counts.
-- name: duplicate_keys
SELECT count(*) FROM (SELECT record_key FROM reference_lab.accepted
 WHERE stream=%(stream)s AND version=%(version)s GROUP BY record_key HAVING count(*)>1) d;
-- name: required_nulls
SELECT count(*) FROM reference_lab.accepted WHERE stream=%(stream)s AND version=%(version)s
 AND (cas_number IS NULL OR cas_number='' OR record_key IS NULL OR record_key=''
 OR NULLIF(trim(payload->>'화학물질ID'),'') IS NULL
 OR NULLIF(trim(payload->>'화학물질명_국문'),'') IS NULL
 OR NULLIF(trim(payload->>'MSDS_장번호'),'') IS NULL
 OR NULLIF(trim(payload->>'MSDS_항목명_국문'),'') IS NULL
 OR NULLIF(trim(payload->>'상세내용'),'') IS NULL
 OR NULLIF(trim(payload->>'최종개정일'),'') IS NULL);
-- name: input_count
SELECT count(*) FROM reference_lab.input_lines WHERE stream=%(stream)s AND version=%(version)s;
-- name: normal_count
SELECT count(*) FROM reference_lab.input_lines WHERE stream=%(stream)s AND version=%(version)s AND disposition='normal';
-- name: excluded_count
SELECT count(*) FROM reference_lab.input_lines WHERE stream=%(stream)s AND version=%(version)s AND disposition='excluded';
-- name: stored_count
SELECT count(*) FROM reference_lab.accepted WHERE stream=%(stream)s AND version=%(version)s;
-- name: missing_accepted
SELECT count(*) FROM reference_lab.input_lines i WHERE i.stream=%(stream)s AND i.version=%(version)s
 AND disposition='normal' AND NOT EXISTS (SELECT 1 FROM reference_lab.accepted a
 WHERE a.stream=i.stream AND a.version=i.version AND a.record_key=i.record_key);
-- name: extra_accepted
SELECT count(*) FROM reference_lab.accepted a WHERE a.stream=%(stream)s AND a.version=%(version)s
 AND NOT EXISTS (SELECT 1 FROM reference_lab.input_lines i WHERE i.stream=a.stream AND i.version=a.version
 AND i.disposition='normal' AND i.record_key=a.record_key);
-- name: missing_service_evidence
SELECT count(*) FROM reference_lab.accepted a WHERE a.stream=%(stream)s AND a.version=%(version)s
 AND NOT EXISTS (SELECT 1 FROM reference_lab.evidence e WHERE e.stream=a.stream AND e.version=a.version
 AND e.source_record_id=a.record_key AND e.cas_number=a.cas_number);
-- name: indexed_count
SELECT count(*) FROM reference_lab.evidence WHERE stream=%(stream)s AND version=%(version)s AND search_document IS NOT NULL;
