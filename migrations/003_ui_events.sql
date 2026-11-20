CREATE TABLE ui_events (
    conversation_id uuid NOT NULL REFERENCES conversations(conversation_id),
    workflow_id uuid NOT NULL REFERENCES workflows(workflow_id),
    seq bigint NOT NULL,
    event_id uuid NOT NULL UNIQUE,
    source_event_id uuid REFERENCES conversation_events(event_id),
    event_type text NOT NULL CHECK (event_type IN ('form_snapshot','selection_snapshot','event_rejected')),
    schema_version integer NOT NULL DEFAULT 1,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(conversation_id, seq)
);
CREATE INDEX ui_events_replay_idx ON ui_events(conversation_id, seq);
