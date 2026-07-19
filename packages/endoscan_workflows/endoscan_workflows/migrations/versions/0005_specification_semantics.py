"""Persist specification semantic validation separately from immutable model outcomes."""

from alembic import op
from sqlalchemy import inspect

revision = "0005_specification_semantics"
down_revision = "0004_specification_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        item["name"]
        for item in inspect(op.get_bind()).get_columns("training_dataset_workflows")
    }
    if "specification_semantic_validation_json" not in columns:
        op.execute(
            "ALTER TABLE training_dataset_workflows "
            "ADD COLUMN specification_semantic_validation_json TEXT"
        )


def downgrade() -> None:
    # Additive nullable audit columns are retained for SQLite/history compatibility.
    pass
