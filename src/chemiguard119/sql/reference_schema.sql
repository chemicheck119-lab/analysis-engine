CREATE SCHEMA IF NOT EXISTS reference_lab;
CREATE TABLE IF NOT EXISTS reference_lab.versions (
 stream text NOT NULL, version text NOT NULL, bundle_sha256 text NOT NULL,
 metadata jsonb NOT NULL, staged_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(stream,version)
);
CREATE TABLE IF NOT EXISTS reference_lab.input_lines (
 stream text NOT NULL, version text NOT NULL, line integer NOT NULL,
 record_key text, disposition text NOT NULL CHECK(disposition IN ('normal','excluded')),
 reasons jsonb NOT NULL, raw_row jsonb NOT NULL,
 PRIMARY KEY(stream,version,line),
 FOREIGN KEY(stream,version) REFERENCES reference_lab.versions
);
CREATE TABLE IF NOT EXISTS reference_lab.accepted (
 stream text NOT NULL, version text NOT NULL, record_key text NOT NULL,
 cas_number text NOT NULL CHECK(length(cas_number)>0), payload jsonb NOT NULL,
 PRIMARY KEY(stream,version,record_key),
 FOREIGN KEY(stream,version) REFERENCES reference_lab.versions
);
CREATE TABLE IF NOT EXISTS reference_lab.evidence (
 stream text NOT NULL, version text NOT NULL, evidence_id text NOT NULL,
 source_record_id text NOT NULL, cas_number text NOT NULL,
 title text NOT NULL, body text NOT NULL CHECK(length(trim(body))>0),
 document_version text,
 search_document tsvector GENERATED ALWAYS AS
 (to_tsvector('simple'::regconfig, coalesce(title,'') || ' ' || body)) STORED,
 PRIMARY KEY(stream,version,evidence_id),
 FOREIGN KEY(stream,version) REFERENCES reference_lab.versions
);
CREATE INDEX IF NOT EXISTS evidence_reference_search ON reference_lab.evidence USING gin(search_document);
CREATE TABLE IF NOT EXISTS reference_lab.heads (
 stream text PRIMARY KEY, version text NOT NULL, activation_id text NOT NULL,
 FOREIGN KEY(stream,version) REFERENCES reference_lab.versions
);
CREATE TABLE IF NOT EXISTS reference_lab.activations (
 stream text NOT NULL, activation_id text PRIMARY KEY, version text NOT NULL,
 run_id text NOT NULL, operation text NOT NULL, at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(stream,version) REFERENCES reference_lab.versions
);
CREATE OR REPLACE VIEW reference_lab.service_evidence AS
 SELECT e.* FROM reference_lab.evidence e JOIN reference_lab.heads h
 ON h.stream=e.stream AND h.version=e.version;
