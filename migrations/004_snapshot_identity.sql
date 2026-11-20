-- Identical Java responses can be fetched more than once; their local refs differ.
ALTER TABLE result_snapshots DROP CONSTRAINT IF EXISTS result_snapshots_workflow_id_content_hash_key;
