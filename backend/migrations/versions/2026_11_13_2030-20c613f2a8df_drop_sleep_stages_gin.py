"""Drop the unused GIN index on sleep stages.

Revision ID: 20c613f2a8df
Revises: 7b2e5d91c4a8
"""

from alembic import op

revision: str = "20c613f2a8df"
down_revision: str = "7b2e5d91c4a8"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    # No API uses JSONB containment on sleep stages. Its pending-list cleanup
    # adds synchronous work to sleep writes, so remove the index without blocking them.
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_sleep_details_stages_gin",
            table_name="sleep_details",
            if_exists=True,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        # If the previous concurrent build was interrupted, it can leave an invalid index.
        op.drop_index(
            "ix_sleep_details_stages_gin",
            table_name="sleep_details",
            if_exists=True,
            postgresql_concurrently=True,
        )
        op.create_index(
            "ix_sleep_details_stages_gin",
            "sleep_details",
            ["sleep_stages"],
            postgresql_using="gin",
            postgresql_ops={"sleep_stages": "jsonb_path_ops"},
            postgresql_concurrently=True,
        )
