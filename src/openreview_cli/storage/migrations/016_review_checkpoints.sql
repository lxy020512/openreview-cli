CREATE TABLE IF NOT EXISTS review_checkpoint_runs (
    identity_digest TEXT PRIMARY KEY,
    document_hash TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','incomplete','completed')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS review_checkpoint_document_idx
ON review_checkpoint_runs(document_hash);
CREATE TABLE IF NOT EXISTS review_checkpoint_steps (
    run_id TEXT NOT NULL REFERENCES review_checkpoint_runs(identity_digest) ON DELETE CASCADE,
    clause_id TEXT NOT NULL,
    clause_hash TEXT NOT NULL,
    category_id TEXT NOT NULL,
    step TEXT NOT NULL CHECK(step IN ('extraction','qa')),
    status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
    payload BLOB,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(run_id, clause_id, step),
    CHECK ((status='completed' AND payload IS NOT NULL)
        OR (status!='completed' AND payload IS NULL))
);
