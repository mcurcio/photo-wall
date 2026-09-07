"""Disposable operator walkthrough using real PostgreSQL and simulated devices.

This is not the end-to-end media/Pi demo. Stop with Ctrl-C to remove its own schema.
"""

import base64
import hashlib
import secrets
import uuid
from pathlib import Path

import psycopg
import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg.conninfo import make_conninfo

from central.app import create_app
from central.db import Database
from central.registry import Enrollment, OutputReport, Registry, enrollment_message
from central.releases import ReleaseAuthority
from contracts.release import BootRequest, Release
from contracts.time import SystemClock

DEMO_TOKEN = "photo-wall-disposable-local-demo-token"


def main():
    root = Path(__file__).resolve().parents[1]
    values = dict(
        line.split("=", 1)
        for line in (root / ".env").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    port = values.get("PHOTO_WALL_DB_PORT", "54329")
    dsn = make_conninfo(
        host="127.0.0.1",
        port=port,
        user="photo_wall",
        dbname="photo_wall",
        password=values["PHOTO_WALL_DB_PASSWORD"],
    )
    schema = "pw_demo_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
    try:
        db = Database(make_conninfo(dsn, options=f"-c search_path={schema}"))
        db.migrate()
        clock = SystemClock()
        signing_key = Ed25519PrivateKey.generate()
        boot_abi, configuration = "a" * 64, "b" * 64
        release = Release("c" * 40, boot_abi, configuration, "d" * 64, 1024)
        authority = ReleaseAuthority(db, clock, signing_key.public_key(), boot_abi, configuration)
        authority.register(release.encode(), signing_key.sign(release.encode()))
        authority.set_default(release.release_id)
        registry = Registry(db, clock, authority)
        for count in (2, 1):
            key = Ed25519PrivateKey.generate()
            public_key = key.public_key().public_bytes_raw().hex()
            device_id = "device-" + hashlib.sha256(bytes.fromhex(public_key)).hexdigest()
            boot_id = str(uuid.uuid4())
            ticket = authority.select_boot(BootRequest(device_id, boot_id, secrets.token_hex(24)))
            nonce = registry.challenge(public_key)["nonce"]
            outputs = tuple(
                OutputReport(output_id=f"HDMI-A-{i + 1}", width_px=1920, height_px=1080)
                for i in range(count)
            )
            registry.enroll(
                Enrollment(
                    public_key=public_key,
                    nonce=nonce,
                    outputs=outputs,
                    device_id=device_id,
                    boot_id=boot_id,
                    ticket_id=ticket.ticket_id,
                    signature=base64.b64encode(
                        key.sign(
                            enrollment_message(nonce, outputs, device_id, boot_id, ticket.ticket_id)
                        )
                    ).decode(),
                )
            )
        print(
            "Simulated equipment + real PostgreSQL operator demo: http://127.0.0.1:8010", flush=True
        )
        print("Disposable demo token: " + DEMO_TOKEN, flush=True)
        uvicorn.run(
            create_app(db, registry.clock, DEMO_TOKEN, release_authority=authority),
            host="127.0.0.1",
            port=8010,
        )
    finally:
        if "db" in locals():
            db.close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema))
            )


if __name__ == "__main__":
    main()
