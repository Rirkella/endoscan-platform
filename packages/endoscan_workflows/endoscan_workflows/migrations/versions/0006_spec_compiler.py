"""Persist compiler-first specification and optional review records."""

from alembic import op
from sqlalchemy import inspect

revision = "0006_spec_compiler"
down_revision = "0005_specification_semantics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        item["name"] for item in inspect(op.get_bind()).get_columns("training_dataset_workflows")
    }
    for name in (
        "specification_compilation_outcome_json",
        "specification_review_record_json",
    ):
        if name not in columns:
            op.execute(f"ALTER TABLE training_dataset_workflows ADD COLUMN {name} TEXT")


def downgrade() -> None:
    # Additive nullable audit columns remain for SQLite/history compatibility.
    pass
