"""Versioned endpoint discovery scope.

Revision ID: 0008_endpoint_discovery_scope
Revises: 0007_reviewed_source_discovery
"""

from alembic import op
from sqlalchemy import inspect

revision = "0008_endpoint_discovery_scope"
down_revision = "0007_reviewed_source_discovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        item["name"] for item in inspect(op.get_bind()).get_columns("training_dataset_workflows")
    }
    if "endpoint_discovery_scope_json" not in columns:
        op.execute(
            "ALTER TABLE training_dataset_workflows "
            "ADD COLUMN endpoint_discovery_scope_json TEXT"
        )


def downgrade() -> None:
    # Additive nullable audit column remains for SQLite/history compatibility.
    pass
