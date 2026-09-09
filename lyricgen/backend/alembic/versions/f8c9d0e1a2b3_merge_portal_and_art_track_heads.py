"""Merge the portal and bulk Art Track migration branches.

Revision ID: f8c9d0e1a2b3
Revises: f3b5d7e9a1c3, ef6a7b8c9d01, b6f2c1a70d84
"""

from typing import Sequence, Union


revision: str = "f8c9d0e1a2b3"
down_revision: Union[str, Sequence[str], None] = (
    "f3b5d7e9a1c3",
    "ef6a7b8c9d01",
    "b6f2c1a70d84",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
