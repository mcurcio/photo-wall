"""Test doubles for the kernel ports that are not the database.

A port whose real implementation is SQL is tested against PostgreSQL instead (`content_db`), never
against an in-memory copy of its queries. What stays here: the `Publisher` double (checked
against the real one by `test_publisher_conformance.py`), the release origin, a fixed content
catalog, and the transaction seam for code paths that never reach a repository.
"""

from __future__ import annotations
