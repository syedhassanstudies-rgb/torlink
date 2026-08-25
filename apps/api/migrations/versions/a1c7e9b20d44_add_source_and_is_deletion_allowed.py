"""Add source and is_deletion_allowed to download_records.

Revision ID: a1c7e9b20d44
Revises: f7edb26305a1
Create Date: 2026-08-25
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1c7e9b20d44"
down_revision: Union[str, None] = "f7edb26305a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "download_records",
        sa.Column("source", sa.String(length=64), nullable=False, server_default="manual"),
    )
    op.add_column(
        "download_records",
        sa.Column(
            "is_deletion_allowed", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )


def downgrade() -> None:
    op.drop_column("download_records", "is_deletion_allowed")
    op.drop_column("download_records", "source")
