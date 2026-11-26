"""Provision ONLY a synthetic two-person LAN pilot in a fresh directory/schema.

The operator must pre-create a private parent directory restricted to the
intended OS user. Windows ACLs are NOT verified. Transfer only remote/ securely.
No service is started; real mail, existing schemas, and files are never changed.
Incomplete attempts are retained for inspection, never automatically deleted.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import ssl
import subprocess
import sys

import psycopg
from psycopg import sql

from experiments.lan_pilot import isolated_dsn
from experiments.lan_tls_proxy import validate_bind
from experiments.postgres_mailbox import Mailbox
from experiments.postgres_sessions import Sessions


def _write(path, text):
    with path.open("x", encoding="ascii", newline="\n") as stream:
        stream.write(text)
    if os.name != "nt":
        path.chmod(0o600)


def _openssl():
    candidate = shutil.which("openssl")
    if not candidate and os.name == "nt":
        path = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/mingw64/bin/openssl.exe"
        if path.is_file():
            candidate = str(path)
    if not candidate:
        raise RuntimeError("OpenSSL executable is required")
    return candidate


def _run_openssl(executable, cwd, *args):
    # Private material is never put on the command line or written to logs.
    subprocess.run([executable, *map(str, args)], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)


def _generate_certificates(root, address):
    exe = _openssl()
    run = lambda *args: _run_openssl(exe, root, *args)
    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=Hardline Pilot Test CA", "-keyout", "server/ca.key", "-out", "server/ca.crt",
        "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    for name in ("server", "local", "remote"):
        run("req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=pilot-" + name,
            "-keyout", name + "/identity.key", "-out", name + "/identity.csr")
        ext = ("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
               + ("extendedKeyUsage=serverAuth\nsubjectAltName=IP:" + address + "\n" if name == "server"
                  else "extendedKeyUsage=clientAuth\n"))
        _write(root / name / "certificate-ext.cnf", ext)
        run("x509", "-req", "-in", name + "/identity.csr", "-CA", "server/ca.crt",
            "-CAkey", "server/ca.key", "-set_serial", "0x" + secrets.token_hex(16), "-days", "1",
            "-extfile", name + "/certificate-ext.cnf", "-out", name + "/identity.crt")
        if os.name != "nt":
            (root / name / "identity.key").chmod(0o600)
    if os.name != "nt":
        (root / "server/ca.key").chmod(0o600)
    ca = (root / "server/ca.crt").read_text(encoding="ascii")
    _write(root / "local/ca.crt", ca)
    _write(root / "remote/ca.crt", ca)


def _fingerprint(path):
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(path.read_text(encoding="ascii"))).hexdigest()


def _initialize_database(dsn, schema):
    # Never use IF NOT EXISTS: even an astronomically unlikely collision fails closed.
    with psycopg.connect(dsn, autocommit=True) as conn:
        if conn.execute("SELECT current_database(), current_user").fetchone() != ("hardline_test", "hardline_test"):
            raise ValueError("unexpected test database or account")
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    box = Mailbox(dsn, schema)
    box.initialize()
    Sessions(dsn, schema).initialize()
    tokens = {}
    for name in ("local", "remote"):
        if not box.register_agent("pilot:" + name, "agent"):
            raise RuntimeError("pilot agent registration failed")
        tokens[name] = box.issue_credential("pilot:" + name)
    return tokens


def provision(parent, name, address, dsn, *, acknowledge_private_parent=False):
    """Create exactly one fresh pilot; credentials are stored only in its files."""
    if acknowledge_private_parent is not True:
        raise ValueError("operator must acknowledge the restricted private parent and Windows ACL limitation")
    address = validate_bind(address)
    dsn = isolated_dsn(dsn)
    parent = Path(parent)
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("private parent must be an existing directory")
    if not isinstance(name, str) or not name or name in (".", "..") or Path(name).name != name or ":" in name or "\\" in name or "/" in name:
        raise ValueError("name must be one directory component")
    if os.name != "nt" and (parent.stat().st_uid != os.geteuid() or parent.stat().st_mode & 0o077):
        raise ValueError("private parent must be owned by the operator and deny group/other access")
    root = parent / name
    root.mkdir(mode=0o700, exist_ok=False)
    for directory in ("server", "local", "remote"):
        (root / directory).mkdir(mode=0o700)
    _generate_certificates(root, address)
    schema = "test_lan_" + secrets.token_hex(16)
    tokens = _initialize_database(dsn, schema)
    bindings = {}
    for agent in ("local", "remote"):
        _write(root / agent / "agent.token", tokens[agent] + "\n")
        _write(root / agent / "client.json", json.dumps({"agent": "pilot:" + agent, "server_ip": address}) + "\n")
        bindings[_fingerprint(root / agent / "identity.crt")] = hashlib.sha256(tokens[agent].encode("ascii")).hexdigest()
    _write(root / "server/bindings.json", json.dumps(bindings, sort_keys=True) + "\n")
    _write(root / "server/pilot.json", json.dumps({"schema": schema, "server_ip": address}) + "\n")
    return schema


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", required=True, help="existing operator-protected directory")
    parser.add_argument("--name", required=True, help="new directory name; must not already exist")
    parser.add_argument("--server-ip", required=True, help="specific RFC1918 IPv4 or 127.0.0.1")
    parser.add_argument("--acknowledge-private-parent", action="store_true", required=True,
                        help="confirm parent is restricted; Windows ACLs are not checked")
    args = parser.parse_args(argv)
    try:
        schema = provision(args.parent, args.name, args.server_ip,
                           os.environ.get("HARDLINE_TEST_POSTGRES_DSN", ""),
                           acknowledge_private_parent=args.acknowledge_private_parent)
    except Exception:
        print("Provision failed; inspect the new directory and test DB configuration. Existing data was not removed. Windows ACLs are not checked.", file=sys.stderr)
        return 1
    print("Synthetic pilot provisioned in a new directory; Windows ACLs are not checked.")
    print("Test schema: " + schema)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
