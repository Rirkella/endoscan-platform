"""Reviewed source-discovery durable checkpoints.

Revision ID: 0007_reviewed_source_discovery
Revises: 0006_spec_compiler
"""

from alembic import op
from sqlalchemy import inspect

revision = "0007_reviewed_source_discovery"
down_revision = "0006_spec_compiler"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        item["name"] for item in inspect(op.get_bind()).get_columns("training_dataset_workflows")
    }
    for name in (
        "source_discovery_authorization_json",
        "source_discovery_budget_json",
        "source_observations_json",
        "source_fragments_json",
    ):
        if name not in columns:
            op.execute(f"ALTER TABLE training_dataset_workflows ADD COLUMN {name} TEXT")


def downgrade() -> None:
    # Additive nullable audit columns remain for SQLite/history compatibility.
    pass
