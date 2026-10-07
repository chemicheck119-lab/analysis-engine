-- Head and evidence are joined in one statement, so each response has one version.
SELECT version,evidence_id,source_record_id,cas_number,title,body
FROM reference_lab.service_evidence
WHERE stream=%(stream)s AND (%(cas)s::text IS NULL OR cas_number=%(cas)s)
ORDER BY evidence_id;
