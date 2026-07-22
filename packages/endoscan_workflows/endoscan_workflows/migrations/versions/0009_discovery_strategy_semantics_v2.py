"""Add workflow-semantics v2 current-document pointers.

Revision ID: 0009_discovery_strategy_semantics_v2
Revises: 0008_endpoint_discovery_scope
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_discovery_strategy_semantics_v2"
down_revision = "0008_endpoint_discovery_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = {item["name"] for item in sa.inspect(bind).get_columns("training_dataset_workflows")}
    columns = [
        sa.Column(
            "workflow_semantics_version",
            sa.String(40),
            nullable=False,
            server_default="1.0.0",
        ),
        *[
            sa.Column(name, sa.Text(), nullable=True)
            for name in (
                "discovery_plan_json",
                "discovery_execution_ledger_json",
                "source_candidates_json",
                "hydrated_sources_json",
                "combination_coverage_json",
                "strategy_proposals_json",
                "assembly_recipe_json",
                "provider_capability_findings_json",
                "strategy_set_rejection_json",
            )
        ],
    ]
    with op.batch_alter_table("training_dataset_workflows") as batch:
        for column in columns:
            if column.name in existing:
                continue
            batch.add_column(column)


def downgrade() -> None:
    # Historical workflow documents are immutable. Downgrade intentionally keeps
    # additive v2 columns rather than discarding audit data.
    pass
