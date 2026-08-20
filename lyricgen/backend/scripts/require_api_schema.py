#!/usr/bin/env python3
"""Fail API startup when the Alembic-managed schema is incomplete."""

from database import assert_runtime_schema_contract


assert_runtime_schema_contract()
print("[schema] API requirements satisfied")
