"""Add durable global scientific-source request accounting.

Revision ID: 0010_scientific_source_request_governor
Revises: 0009_discovery_strategy_semantics_v2
"""

from alembic import op

revision = "0010_scientific_source_request_governor"
down_revision = "0009_discovery_strategy_semantics_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scientific_source_request_budgets (
            id VARCHAR(128) NOT NULL PRIMARY KEY,
            workflow_id VARCHAR(128) NOT NULL,
            discovery_round INTEGER NOT NULL,
            maximum_requests INTEGER NOT NULL,
            reserved_requests INTEGER NOT NULL DEFAULT 0,
            completed_transport_attempts INTEGER NOT NULL DEFAULT 0,
            failed_transport_attempts INTEGER NOT NULL DEFAULT 0,
            cache_hits INTEGER NOT NULL DEFAULT 0,
            blocked_requests INTEGER NOT NULL DEFAULT 0,
            prevented_by_cancellation INTEGER NOT NULL DEFAULT 0,
            created_at VARCHAR(40) NOT NULL,
            updated_at VARCHAR(40) NOT NULL,
            FOREIGN KEY(workflow_id) REFERENCES endpoint_builds (id) ON DELETE CASCADE,
            CONSTRAINT uq_scientific_source_budget_workflow_round
                UNIQUE (workflow_id, discovery_round),
            CONSTRAINT ck_scientific_source_budget_nonnegative
                CHECK (
                    maximum_requests >= 0
                    AND reserved_requests >= 0
                    AND reserved_requests <= maximum_requests
                    AND completed_transport_attempts >= 0
                    AND failed_transport_attempts >= 0
                    AND cache_hits >= 0
                    AND blocked_requests >= 0
                    AND prevented_by_cancellation >= 0
                )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scientific_source_request_attempts (
            id VARCHAR(128) NOT NULL PRIMARY KEY,
            budget_id VARCHAR(128) NOT NULL,
            workflow_id VARCHAR(128) NOT NULL,
            discovery_round INTEGER NOT NULL,
            task_id VARCHAR(160) NOT NULL,
            tool_name VARCHAR(80) NOT NULL,
            reservation_sequence INTEGER,
            request_fingerprint VARCHAR(64) NOT NULL,
            source_host VARCHAR(253) NOT NULL,
            safe_url_path VARCHAR(500) NOT NULL,
            outcome VARCHAR(80) NOT NULL,
            request_left_process INTEGER NOT NULL DEFAULT 0,
            started_at VARCHAR(40),
            completed_at VARCHAR(40),
            http_status INTEGER,
            final_approved_host VARCHAR(253),
            error_category VARCHAR(120),
            created_at VARCHAR(40) NOT NULL,
            FOREIGN KEY(budget_id)
                REFERENCES scientific_source_request_budgets (id) ON DELETE CASCADE,
            FOREIGN KEY(workflow_id) REFERENCES endpoint_builds (id) ON DELETE CASCADE,
            CONSTRAINT uq_scientific_source_budget_reservation
                UNIQUE (budget_id, reservation_sequence)
        )
        """
    )
    for statement in (
        "CREATE INDEX IF NOT EXISTS ix_scientific_source_budget_workflow "
        "ON scientific_source_request_budgets (workflow_id)",
        "CREATE INDEX IF NOT EXISTS ix_scientific_source_attempt_budget "
        "ON scientific_source_request_attempts (budget_id)",
        "CREATE INDEX IF NOT EXISTS ix_scientific_source_attempt_workflow "
        "ON scientific_source_request_attempts (workflow_id)",
        "CREATE INDEX IF NOT EXISTS ix_scientific_source_attempt_task "
        "ON scientific_source_request_attempts (task_id)",
        "CREATE INDEX IF NOT EXISTS ix_scientific_source_attempt_tool "
        "ON scientific_source_request_attempts (tool_name)",
        "CREATE INDEX IF NOT EXISTS ix_scientific_source_attempt_fingerprint "
        "ON scientific_source_request_attempts (request_fingerprint)",
        "CREATE INDEX IF NOT EXISTS ix_scientific_source_attempt_outcome "
        "ON scientific_source_request_attempts (outcome)",
    ):
        op.execute(statement)


def downgrade() -> None:
    # Durable request accounting is audit evidence and is intentionally retained.
    pass
