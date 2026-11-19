"""merge fork and upstream heads

Revision ID: 8c2e9dc56aa9
Revises: 20c613f2a8df, 3f6b9c2d8e41

The upstream sync of 2026-11-19 (RHISE-7850) brings upstream's chain from c4e7a1d93b52 to
3f6b9c2d8e41 next to the fork's own 7b2e5d91c4a8 and 20c613f2a8df. Neither chain is re-pointed:
staging and production already recorded 20c613f2a8df, so upgrading to this revision applies
upstream's five migrations on top and then this one, which changes nothing itself.

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "8c2e9dc56aa9"
down_revision: Union[str, Sequence[str], None] = ("20c613f2a8df", "3f6b9c2d8e41")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
