"""Create the durable Phase-0 workflow schema."""

from alembic import op

from endoscan_workflows.models import Base

revision = "0001_phase0_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    op.execute(
        "CREATE TRIGGER workflow_events_no_update BEFORE UPDATE ON workflow_events "
        "BEGIN SELECT RAISE(ABORT, 'workflow events are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER workflow_events_no_delete BEFORE DELETE ON workflow_events "
        "BEGIN SELECT RAISE(ABORT, 'workflow events are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER human_decisions_no_update BEFORE UPDATE ON human_decisions "
        "BEGIN SELECT RAISE(ABORT, 'human decisions are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER human_decisions_no_delete BEFORE DELETE ON human_decisions "
        "BEGIN SELECT RAISE(ABORT, 'human decisions are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER approval_decision_no_rewrite BEFORE UPDATE ON approvals "
        "WHEN OLD.status != 'pending' "
        "BEGIN SELECT RAISE(ABORT, 'decided approvals are immutable'); END"
    )


def downgrade() -> None:
    for trigger in (
        "approval_decision_no_rewrite",
        "human_decisions_no_delete",
        "human_decisions_no_update",
        "workflow_events_no_delete",
        "workflow_events_no_update",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    Base.metadata.drop_all(bind=op.get_bind())
