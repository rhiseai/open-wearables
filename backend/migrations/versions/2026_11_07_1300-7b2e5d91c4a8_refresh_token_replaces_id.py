"""refresh_token_replaces_id

Record, on a refresh token issued by rotation, the token it replaced. A mobile client whose
refresh request reached the server but whose response it never read (iOS suspended the app)
still holds the old token; with this link a replay of it is answered with the same successor
instead of a 401 that signs the client out.

Existing rows stay NULL: tokens rotated before this migration cannot be replayed.

Revision ID: 7b2e5d91c4a8
Revises: c4e7a1d93b52

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7b2e5d91c4a8"
down_revision: Union[str, None] = "c4e7a1d93b52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("refresh_token", sa.Column("replaces_id", sa.String(length=64), nullable=True))
    op.create_foreign_key(
        "refresh_token_replaces_id_fkey",
        "refresh_token",
        "refresh_token",
        ["replaces_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint("refresh_token_replaces_id_key", "refresh_token", ["replaces_id"])


def downgrade() -> None:
    op.drop_constraint("refresh_token_replaces_id_key", "refresh_token", type_="unique")
    op.drop_constraint("refresh_token_replaces_id_fkey", "refresh_token", type_="foreignkey")
    op.drop_column("refresh_token", "replaces_id")
