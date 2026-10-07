-- Incoming accepted records only. Absence is diagnostic, never a delete instruction.
-- FULL JOIN on version-scoped unique keys cannot multiply rows.
SELECT COALESCE(n.record_key,o.record_key) AS record_key,
 CASE WHEN o.record_key IS NULL THEN 'added'
      WHEN n.record_key IS NULL THEN 'absent_from_selected_input'
      WHEN o.payload IS DISTINCT FROM n.payload THEN 'changed'
      ELSE 'unchanged' END AS change
FROM (SELECT * FROM reference_lab.accepted WHERE stream=%(stream)s AND version=%(old)s) o
FULL JOIN (SELECT * FROM reference_lab.accepted WHERE stream=%(stream)s AND version=%(new)s) n
ON o.record_key=n.record_key
ORDER BY record_key;
