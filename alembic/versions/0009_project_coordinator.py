"""project coordinator on projects

Revision ID: 5c1d2e3f4a5b
Revises: 841191a5ad31
Create Date: 2026-11-06 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5c1d2e3f4a5b'
down_revision: Union[str, Sequence[str], None] = '841191a5ad31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pc_name', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('pc_email', sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_column('pc_email')
        batch_op.drop_column('pc_name')
