"""Manually started synthetic-mail LAN pilot, never a production service.

Schema and agents must already exist in the isolated test database. The operator
must restrict access to the private key and write access to the JSON bindings
file; Windows ACLs are not verified here. No schema/data is created or deleted.
This program does not provision certificates, tokens, or real user mail.
"""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import threading

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from experiments.lan_tls_proxy import create_server as create_tls_server, validate_bind
from experiments.loopback_gateway import create_server as create_gateway
from experiments.postgres_mailbox import Mailbox


SCHEMA_RE = re.compile(r"test_lan_[0-9a-f]{32}\Z")
HEX_RE = re.compile(r"[0-9a-f]{64}\Z")
REQUIRED_TABLES = frozenset(("agent_registry", "lanes", "credentials", "messages", "session_leases"))
MAX_BINDINGS_BYTES = 65536


def isolated_dsn(value):
    """Pin this pilot to the explicitly provisioned local test service/account."""
    try:
        parts = conninfo_to_dict(value)
    except Exception:
        raise ValueError("invalid test database configuration") from None
    allowed = {"host", "hostaddr", "port", "dbname", "user", "password"}
    if (set(parts) - allowed or parts.get("host") != "127.0.0.1" or
            parts.get("hostaddr", "127.0.0.1") != "127.0.0.1" or parts.get("port") != "55432" or
            parts.get("dbname") != "hardline_test" or parts.get("user") != "hardline_test"):
        raise ValueError("database must be the local hardline_test service and account")
    # Explicit libpq arguments also override corresponding PG* environment values.
    safe = dict(parts, hostaddr="127.0.0.1", connect_timeout="5", options="-c search_path=pg_catalog")
    return make_conninfo(**safe)


def _pairs_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate certificate binding")
        result[key] = value
    return result


def read_bindings(path):
    """Read only an explicit file. Windows ACL correctness is operator-managed."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("bindings must be an existing regular file")
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BINDINGS_BYTES:
            raise ValueError("invalid bindings file")
        if os.name != "nt" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError("bindings file must belong to the operator and deny group/other access")
        contents = stream.read(MAX_BINDINGS_BYTES + 1)
    if len(contents) > MAX_BINDINGS_BYTES:
        raise ValueError("invalid bindings file")
    try:
        bindings = json.loads(contents, object_pairs_hook=_pairs_no_duplicates)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("invalid bindings JSON") from None
    if (not isinstance(bindings, dict) or not 1 <= len(bindings) <= 256 or
            any(not isinstance(k, str) or not isinstance(v, str) or
                not HEX_RE.fullmatch(k) or not HEX_RE.fullmatch(v) for k, v in bindings.items())):
        raise ValueError("bindings must map certificate SHA-256 to agent-token SHA-256")
    return bindings


def verify_existing_schema(dsn, schema):
    if not SCHEMA_RE.fullmatch(schema):
        raise ValueError("a unique test_lan_<32 hex> schema is required")
    with psycopg.connect(dsn, autocommit=True) as conn:
        database, user = conn.execute("SELECT current_database(), current_user").fetchone()
        if database != "hardline_test" or user != "hardline_test":
            raise ValueError("unexpected test database or account")
        rows = conn.execute("""SELECT c.relname FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relkind = 'r'""", (schema,)).fetchall()
        if not REQUIRED_TABLES.issubset({row[0] for row in rows}):
            raise ValueError("test schema is missing required tables; provision it separately")


def run(args):
    if not SCHEMA_RE.fullmatch(args.schema):
        raise ValueError("a unique test_lan_<32 hex> schema is required")
    bind = validate_bind(args.bind)
    if bind != "127.0.0.1" and not args.allow_lan_test:
        raise ValueError("LAN binding requires --allow-lan-test")
    if type(args.port) is not int or not 0 <= args.port <= 65535:
        raise ValueError("invalid listener port")
    dsn = isolated_dsn(os.environ.get("HARDLINE_TEST_POSTGRES_DSN", ""))
    bindings = read_bindings(args.bindings_file)
    verify_existing_schema(dsn, args.schema)
    box = Mailbox(dsn, args.schema)
    gateway = create_gateway(box)
    gateway_thread = None
    tls = None
    try:
        if gateway.server_address[0] != "127.0.0.1":
            raise ValueError("gateway must listen on loopback")
        tls = create_tls_server(certfile=args.cert, keyfile=args.key, client_ca=args.client_ca,
                                backend_port=gateway.server_port, bindings=bindings, bind=bind, port=args.port)
        gateway_thread = threading.Thread(target=gateway.serve_forever, name="lan-pilot-gateway", daemon=True)
        gateway_thread.start()
        print("Synthetic-mail pilot only; Windows ACLs are not checked. Restrict private key access and bindings-file write access.", flush=True)
        print(f"Listening on {bind}:{tls.server_port}; schema {args.schema}", flush=True)
        tls.serve_forever()
    finally:
        if tls is not None:
            tls.server_close()
        if gateway_thread is not None and gateway_thread.is_alive():
            gateway.shutdown()
            gateway_thread.join(timeout=10)
        gateway.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--cert", required=True, help="operator-provided server certificate")
    parser.add_argument("--key", required=True, help="operator-provided server private key")
    parser.add_argument("--client-ca", required=True, help="operator-provided client CA")
    parser.add_argument("--bindings-file", required=True, help="operator-protected certificate-to-token hash JSON")
    parser.add_argument("--bind", default="127.0.0.1", help="explicit IPv4 interface")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--allow-lan-test", action="store_true", help="enable non-loopback synthetic testing")
    args = parser.parse_args(argv)
    try:
        run(args)
    except KeyboardInterrupt:
        return 0
    except Exception:
        # Do not expose database DSNs, credentials, certificate material, or mail.
        print("Pilot could not start; check isolated test configuration and file permissions.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
