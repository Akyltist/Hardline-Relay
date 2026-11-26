"""Provision into an isolated temporary directory and test-only PostgreSQL schema."""

import os
import re

import psycopg
import pytest
from psycopg import sql

from experiments.lan_pilot import verify_existing_schema
from experiments.lan_pilot_provision import provision
from experiments.postgres_mailbox import Mailbox


def test_provisioned_test_schema_has_two_independent_credentials(tmp_path):
    dsn = os.environ.get("HARDLINE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("isolated PostgreSQL DSN not configured")
    tmp_path.chmod(0o700)
    schema = provision(tmp_path, "synthetic", "127.0.0.1", dsn, acknowledge_private_parent=True)
    try:
        assert re.fullmatch(r"test_lan_[0-9a-f]{32}", schema)
        verify_existing_schema(dsn, schema)
        mailbox = Mailbox(dsn, schema)
        tokens = [(tmp_path / "synthetic" / name / "agent.token").read_text().strip() for name in ("local", "remote")]
        assert tokens[0] != tokens[1]
        assert mailbox.identify(tokens[0]) == "pilot:local"
        assert mailbox.identify(tokens[1]) == "pilot:remote"
    finally:
        # The provisioner created this unique schema for this test alone.
        if re.fullmatch(r"test_lan_[0-9a-f]{32}", schema):
            with psycopg.connect(dsn, autocommit=True) as conn:
                assert conn.execute("SELECT current_database()").fetchone()[0] == "hardline_test"
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
