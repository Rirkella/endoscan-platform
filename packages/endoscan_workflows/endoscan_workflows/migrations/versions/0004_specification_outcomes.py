"""Persist specification drafts and terminal planner outcomes."""

from alembic import op
from sqlalchemy import inspect

revision = "0004_specification_outcomes"
down_revision = "0003_training_dataset_discovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        item["name"] for item in inspect(op.get_bind()).get_columns("training_dataset_workflows")
    }
    if "specification_draft_json" not in columns:
        op.execute(
            "ALTER TABLE training_dataset_workflows ADD COLUMN specification_draft_json TEXT"
        )
    if "specification_outcome_json" not in columns:
        op.execute(
            "ALTER TABLE training_dataset_workflows ADD COLUMN specification_outcome_json TEXT"
        )


def downgrade() -> None:
    # Additive nullable audit columns are retained for SQLite/history compatibility.
    pass
