"""invites and access requests

Revision ID: 6d2e3f4a5b6c
Revises: 5c1d2e3f4a5b
Create Date: 2026-11-06 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6d2e3f4a5b6c'
down_revision: Union[str, Sequence[str], None] = '5c1d2e3f4a5b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'invites',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=True),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=True),
        sa.Column('created_by', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('emailed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )
    with op.batch_alter_table('invites', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_invites_email'), ['email'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('invites', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_invites_email'))
    op.drop_table('invites')
