"""provider priority wearables over apple

Revision ID: b7e3f1a9c204
Revises: 9f0940493a9b

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7e3f1a9c204"
down_revision: Union[str, None] = "9f0940493a9b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CANONICAL = {
    "oura": 1,
    "whoop": 2,
    "garmin": 3,
    "polar": 4,
    "suunto": 5,
    "fitbit": 6,
    "ultrahuman": 7,
    "apple": 8,
}

_PREVIOUS = {
    "apple": 1,
    "garmin": 2,
    "polar": 3,
    "suunto": 4,
    "whoop": 5,
}


def upgrade() -> None:
    conn = op.get_bind()
    for provider, priority in _CANONICAL.items():
        conn.execute(
            sa.text(
                "INSERT INTO provider_priority (id, provider, priority, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :provider, :priority, now(), now()) "
                "ON CONFLICT (provider) DO UPDATE SET priority = EXCLUDED.priority, updated_at = now()"
            ),
            {"provider": provider, "priority": priority},
        )
    conn.execute(
        sa.text(
            "UPDATE provider_priority SET priority = priority + 100, updated_at = now() "
            "WHERE provider NOT IN :canonical AND priority <= 8"
        ).bindparams(sa.bindparam("canonical", expanding=True)),
        {"canonical": list(_CANONICAL)},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("UPDATE provider_priority SET priority = priority - 100, updated_at = now() WHERE priority > 100")
    )
    for provider, priority in _PREVIOUS.items():
        conn.execute(
            sa.text("UPDATE provider_priority SET priority = :priority, updated_at = now() WHERE provider = :provider"),
            {"provider": provider, "priority": priority},
        )
