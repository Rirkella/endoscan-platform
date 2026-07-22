"""Add the persistent scientific source-response cache.

The historical revision identifier remains unchanged for Alembic compatibility.
"""

from alembic import op

revision = "0002_phase1_source_cache"
down_revision = "0001_phase0_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 intentionally creates current metadata for fresh databases. IF NOT EXISTS
    # keeps upgrades correct for both a fresh database and an existing foundation database.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS source_response_cache (
            cache_key VARCHAR(64) NOT NULL PRIMARY KEY,
            tool_name VARCHAR(80) NOT NULL,
            normalized_arguments_json TEXT NOT NULL,
            policy_version VARCHAR(40) NOT NULL,
            source_version VARCHAR(120),
            source_url VARCHAR(1000) NOT NULL,
            http_metadata_json TEXT NOT NULL,
            content_hash VARCHAR(64) NOT NULL,
            parsed_output_json TEXT NOT NULL,
            raw_artifact_id VARCHAR(128) NOT NULL,
            retrieved_at VARCHAR(40) NOT NULL,
            expires_at VARCHAR(40) NOT NULL,
            FOREIGN KEY(raw_artifact_id) REFERENCES artifacts (id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_source_response_cache_tool_name "
        "ON source_response_cache (tool_name)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_source_response_cache_content_hash "
        "ON source_response_cache (content_hash)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_source_response_cache_expires_at "
        "ON source_response_cache (expires_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_source_response_cache_raw_artifact_id "
        "ON source_response_cache (raw_artifact_id)"
    )


def downgrade() -> None:
    op.drop_table("source_response_cache")
