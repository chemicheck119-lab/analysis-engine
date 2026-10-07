-- Trino. Replace TABLE and SNAPSHOT from verified lake_receipts, never from client input.
-- Full keys, not CAS: one chemical has multiple MSDS sections/items.
SELECT version,evidence_id,count(*) AS n FROM reference.approved.TABLE
GROUP BY version,evidence_id HAVING count(*) <> 1;
SELECT count(*) AS invalid FROM reference.approved.TABLE
WHERE evidence_id IS NULL OR cas_number IS NULL OR trim(body) = '' OR body IS NULL;
-- Compare all values/keys with PostgreSQL in the Python integration test, not counts alone.
SELECT version,count(*) AS n FROM reference.approved.TABLE GROUP BY version;
SELECT * FROM reference.approved.TABLE FOR VERSION AS OF SNAPSHOT ORDER BY evidence_id;
-- Prefer receipt-selected snapshots; overwrite can retain intermediate DELETE snapshots.
SELECT snapshot_id,operation,summary FROM reference.approved."TABLE$snapshots";
