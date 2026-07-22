"""Add source-neutral training-dataset discovery workflow persistence."""

from alembic import op
from sqlalchemy import inspect

revision = "0003_training_dataset_discovery"
down_revision = "0002_phase1_source_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    build_columns = {item["name"] for item in inspector.get_columns("endpoint_builds")}
    if "workflow_kind" not in build_columns:
        op.execute(
            "ALTER TABLE endpoint_builds ADD COLUMN workflow_kind VARCHAR(80) "
            "NOT NULL DEFAULT 'legacy_single_source_discovery'"
        )
    if "benchmark_mode" not in build_columns:
        op.execute(
            "ALTER TABLE endpoint_builds ADD COLUMN benchmark_mode VARCHAR(120) "
            "NOT NULL DEFAULT 'none'"
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_endpoint_builds_workflow_kind "
        "ON endpoint_builds (workflow_kind)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_endpoint_builds_benchmark_mode "
        "ON endpoint_builds (benchmark_mode)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS training_dataset_workflows (
            workflow_id VARCHAR(128) NOT NULL PRIMARY KEY,
            contract_version VARCHAR(40) NOT NULL DEFAULT '1.0.0',
            benchmark_mode VARCHAR(120) NOT NULL,
            initial_context_json TEXT NOT NULL,
            specification_json TEXT,
            component_requirements_json TEXT,
            source_inventory_json TEXT,
            capability_matrix_json TEXT,
            assembly_strategies_json TEXT,
            joinability_diagnostics_json TEXT,
            gap_report_json TEXT,
            preparation_plan_json TEXT,
            assembly_review_json TEXT,
            discovery_round INTEGER NOT NULL DEFAULT 0,
            created_at VARCHAR(40) NOT NULL,
            updated_at VARCHAR(40) NOT NULL,
            FOREIGN KEY(workflow_id) REFERENCES endpoint_builds (id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_training_dataset_workflows_benchmark_mode "
        "ON training_dataset_workflows (benchmark_mode)"
    )


def downgrade() -> None:
    op.drop_table("training_dataset_workflows")
    # SQLite cannot safely drop these columns while preserving immutable-history
    # triggers.  The downgrade intentionally retains the two additive metadata fields.
