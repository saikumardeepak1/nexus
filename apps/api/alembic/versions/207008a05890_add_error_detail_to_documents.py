"""add error_detail to documents

Revision ID: 207008a05890
Revises: 5fc608668c2c
Create Date: 2026-07-23 04:06:05.124359

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "207008a05890"
down_revision: str | None = "5fc608668c2c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, additive column: populated only when a document's ingestion
    # job fails (see app/workers/jobs.py process_document), so the dashboard
    # and API can surface why a document ended up in status="failed" instead
    # of just the bare status string.
    op.add_column("documents", sa.Column("error_detail", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "error_detail")
