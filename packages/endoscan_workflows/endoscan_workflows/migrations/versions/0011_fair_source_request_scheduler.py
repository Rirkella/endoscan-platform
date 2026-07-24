"""Add durable fair allocation above the global scientific-source governor.

Revision ID: 0011_fair_source_request_scheduler
Revises: 0010_scientific_source_request_governor
"""

from alembic import op

revision = "0011_fair_source_request_scheduler"
down_revision = "0010_scientific_source_request_governor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scientific_source_allocation_policies (
            id VARCHAR(128) NOT NULL PRIMARY KEY,
            workflow_id VARCHAR(128) NOT NULL,
            discovery_round INTEGER NOT NULL,
            policy_version VARCHAR(40) NOT NULL,
            plan_fingerprint VARCHAR(64) NOT NULL,
            maximum_requests INTEGER NOT NULL,
            initial_shared_remainder INTEGER NOT NULL,
            available_shared_remainder INTEGER NOT NULL,
            policy_json TEXT NOT NULL,
            created_at VARCHAR(40) NOT NULL,
            updated_at VARCHAR(40) NOT NULL,
            FOREIGN KEY(workflow_id) REFERENCES endpoint_builds (id) ON DELETE CASCADE,
            CONSTRAINT uq_scientific_source_allocation_policy_workflow_round
                UNIQUE (workflow_id, discovery_round)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scientific_source_allocation_buckets (
            id VARCHAR(128) NOT NULL PRIMARY KEY,
            policy_id VARCHAR(128) NOT NULL,
            workflow_id VARCHAR(128) NOT NULL,
            discovery_round INTEGER NOT NULL,
            bucket_kind VARCHAR(40) NOT NULL,
            bucket_key VARCHAR(160) NOT NULL,
            maximum_requests INTEGER NOT NULL,
            consumed_requests INTEGER NOT NULL DEFAULT 0,
            created_at VARCHAR(40) NOT NULL,
            updated_at VARCHAR(40) NOT NULL,
            FOREIGN KEY(policy_id)
                REFERENCES scientific_source_allocation_policies(id) ON DELETE CASCADE,
            FOREIGN KEY(workflow_id) REFERENCES endpoint_builds(id) ON DELETE CASCADE,
            CONSTRAINT uq_scientific_source_allocation_bucket
                UNIQUE (policy_id, bucket_kind, bucket_key)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scientific_source_task_allocations (
            id VARCHAR(128) NOT NULL PRIMARY KEY,
            policy_id VARCHAR(128) NOT NULL,
            workflow_id VARCHAR(128) NOT NULL,
            discovery_round INTEGER NOT NULL,
            task_id VARCHAR(160) NOT NULL,
            provider VARCHAR(120) NOT NULL,
            source_family VARCHAR(120) NOT NULL,
            evidence_role VARCHAR(80) NOT NULL,
            modality VARCHAR(120),
            execution_phase VARCHAR(80) NOT NULL,
            sequence INTEGER NOT NULL,
            initial_allocation INTEGER NOT NULL,
            allocated_requests INTEGER NOT NULL,
            maximum_requests INTEGER NOT NULL,
            consumed_requests INTEGER NOT NULL DEFAULT 0,
            blocked_requests INTEGER NOT NULL DEFAULT 0,
            released_requests INTEGER NOT NULL DEFAULT 0,
            status VARCHAR(80) NOT NULL,
            terminal_outcome VARCHAR(120),
            prerequisite_json TEXT NOT NULL,
            completion_reason TEXT,
            created_at VARCHAR(40) NOT NULL,
            updated_at VARCHAR(40) NOT NULL,
            FOREIGN KEY(policy_id)
                REFERENCES scientific_source_allocation_policies(id) ON DELETE CASCADE,
            FOREIGN KEY(workflow_id) REFERENCES endpoint_builds(id) ON DELETE CASCADE,
            CONSTRAINT uq_scientific_source_task_allocation UNIQUE(policy_id, task_id)
        )
        """
    )
    for statement in (
        "CREATE INDEX IF NOT EXISTS ix_source_allocation_policy_workflow "
        "ON scientific_source_allocation_policies(workflow_id)",
        "CREATE INDEX IF NOT EXISTS ix_source_allocation_bucket_policy "
        "ON scientific_source_allocation_buckets(policy_id)",
        "CREATE INDEX IF NOT EXISTS ix_source_task_allocation_policy "
        "ON scientific_source_task_allocations(policy_id)",
        "CREATE INDEX IF NOT EXISTS ix_source_task_allocation_task "
        "ON scientific_source_task_allocations(task_id)",
        "CREATE INDEX IF NOT EXISTS ix_source_task_allocation_sequence "
        "ON scientific_source_task_allocations(sequence)",
    ):
        op.execute(statement)


def downgrade() -> None:
    # Allocation decisions are audit evidence and are intentionally retained.
    pass
