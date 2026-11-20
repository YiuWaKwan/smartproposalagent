-- Agent-owned persistence. Java business tables remain outside this database.
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id text NOT NULL,
    owner_user_id text NOT NULL,
    brand_id text NOT NULL,
    thread_id text NOT NULL UNIQUE,
    title text,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','completed','paused','blocked')),
    active_workflow_id uuid,
    schema_version integer NOT NULL DEFAULT 1,
    event_seq bigint NOT NULL DEFAULT 0,
    message_seq bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, owner_user_id, brand_id, conversation_id)
);

CREATE TABLE IF NOT EXISTS workflows (
    workflow_id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES conversations(conversation_id),
    tenant_id text NOT NULL,
    owner_user_id text NOT NULL,
    brand_id text NOT NULL,
    draft_json jsonb NOT NULL,
    form_revision integer NOT NULL DEFAULT 0,
    selection_revision integer NOT NULL DEFAULT 0,
    stage text NOT NULL DEFAULT 'campaign_draft',
    business_campaign_id bigint,
    business_task_id bigint,
    downstream_task_id text,
    candidate_snapshot_ref text,
    candidate_snapshot_hash text,
    pending_ui jsonb,
    version bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (conversation_id, workflow_id),
    UNIQUE (workflow_id, tenant_id, owner_user_id, brand_id),
    FOREIGN KEY (tenant_id, owner_user_id, brand_id, conversation_id)
        REFERENCES conversations(tenant_id, owner_user_id, brand_id, conversation_id)
);

ALTER TABLE conversations ADD CONSTRAINT conversations_active_workflow_fk
    FOREIGN KEY (conversation_id, active_workflow_id) REFERENCES workflows(conversation_id, workflow_id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE IF NOT EXISTS conversation_messages (
    message_id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES conversations(conversation_id),
    workflow_id uuid REFERENCES workflows(workflow_id),
    seq bigint NOT NULL,
    role text NOT NULL CHECK (role IN ('user','assistant','system','tool')),
    content text,
    content_json jsonb,
    source_event_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (conversation_id, seq),
    UNIQUE (conversation_id, message_id)
);

CREATE TABLE IF NOT EXISTS conversation_events (
    event_id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES conversations(conversation_id),
    workflow_id uuid NOT NULL REFERENCES workflows(workflow_id),
    seq bigint NOT NULL,
    client_event_id uuid NOT NULL,
    body_hash text NOT NULL CHECK (body_hash ~ '^[0-9a-f]{64}$'),
    event_type text NOT NULL,
    schema_version integer NOT NULL DEFAULT 1,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'accepted' CHECK (status IN ('accepted','processing','applied','conflict','rejected','unknown')),
    receipt jsonb,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    UNIQUE (conversation_id, seq),
    UNIQUE (conversation_id, client_event_id)
);

CREATE TABLE IF NOT EXISTS workflow_operations (
    operation_id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES conversations(conversation_id),
    workflow_id uuid NOT NULL REFERENCES workflows(workflow_id),
    tenant_id text NOT NULL,
    owner_user_id text NOT NULL,
    brand_id text NOT NULL,
    operation_type text NOT NULL,
    request_id uuid NOT NULL,
    confirmation_id uuid,
    form_revision integer NOT NULL,
    selection_revision integer,
    payload_hash text NOT NULL,
    status text NOT NULL,
    campaign_id bigint,
    business_task_id bigint,
    downstream_task_id text,
    result_ref text,
    receipt jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, workflow_id, operation_type, form_revision, request_id),
    UNIQUE (tenant_id, workflow_id, operation_type, confirmation_id)
);

CREATE TABLE IF NOT EXISTS selection_revisions (
    workflow_id uuid NOT NULL REFERENCES workflows(workflow_id),
    revision integer NOT NULL,
    snapshot_ref text,
    snapshot_hash text,
    selected_kol_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    rejected_kols jsonb NOT NULL DEFAULT '{}'::jsonb,
    cancelled_kol_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    source_event_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workflow_id, revision)
);

CREATE TABLE IF NOT EXISTS result_snapshots (
    snapshot_ref text PRIMARY KEY,
    workflow_id uuid NOT NULL REFERENCES workflows(workflow_id),
    tenant_id text NOT NULL,
    owner_user_id text NOT NULL,
    brand_id text NOT NULL,
    content_hash text NOT NULL,
    metadata jsonb NOT NULL,
    results jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workflow_id, content_hash)
);

CREATE TABLE IF NOT EXISTS outbox_events (
    outbox_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES conversations(conversation_id),
    workflow_id uuid NOT NULL REFERENCES workflows(workflow_id),
    source_event_id uuid,
    topic text NOT NULL,
    aggregate_id text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','leased','delivered','retry','failed')),
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text,
    lease_until timestamptz,
    fencing_token bigint NOT NULL DEFAULT 0,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz,
    UNIQUE (source_event_id, topic)
);

CREATE TABLE IF NOT EXISTS workflow_leases (
    workflow_id uuid PRIMARY KEY REFERENCES workflows(workflow_id),
    lease_owner text NOT NULL,
    lease_until timestamptz NOT NULL,
    fencing_token bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS context_summaries (
    summary_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES conversations(conversation_id),
    workflow_id uuid NOT NULL REFERENCES workflows(workflow_id),
    covered_until_message_id uuid,
    source_message_ids jsonb NOT NULL,
    summary_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workflow_id, covered_until_message_id)
);

CREATE INDEX IF NOT EXISTS conversations_owner_idx ON conversations (tenant_id, owner_user_id, brand_id, updated_at DESC, conversation_id);
CREATE INDEX IF NOT EXISTS messages_page_idx ON conversation_messages (conversation_id, seq DESC);
CREATE INDEX IF NOT EXISTS events_replay_idx ON conversation_events (conversation_id, seq);
CREATE INDEX IF NOT EXISTS outbox_scan_idx ON outbox_events (status, available_at, lease_until);
CREATE INDEX IF NOT EXISTS operations_workflow_idx ON workflow_operations (workflow_id, updated_at DESC);

CREATE OR REPLACE FUNCTION reject_message_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'conversation messages are append only';
END $$;
CREATE TRIGGER conversation_messages_append_only BEFORE UPDATE OR DELETE ON conversation_messages
    FOR EACH ROW EXECUTE FUNCTION reject_message_mutation();
