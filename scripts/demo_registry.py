"""Disposable operator walkthrough using real PostgreSQL and simulated devices.

This is not the end-to-end media/Pi demo. Stop with Ctrl-C to remove its own schema.
"""

import base64
import uuid
from pathlib import Path

import psycopg
import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg.conninfo import make_conninfo

from central.app import create_app
from central.db import Database
from central.registry import Enrollment, OutputReport, Registry, enrollment_message
from contracts.time import SystemClock

DEMO_TOKEN = "photo-wall-disposable-local-demo-token"


def main():
    root = Path(__file__).resolve().parents[1]
    values = dict(line.split("=", 1) for line in (root / ".env").read_text().splitlines()
                  if line and not line.startswith("#") and "=" in line)
    port = values.get("PHOTO_WALL_DB_PORT", "54329")
    dsn = make_conninfo(host="127.0.0.1", port=port, user="photo_wall", dbname="photo_wall",
                       password=values["PHOTO_WALL_DB_PASSWORD"])
    schema = "pw_demo_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
    try:
        db = Database(make_conninfo(dsn, options=f"-c search_path={schema}"))
        db.migrate()
        registry = Registry(db, SystemClock())
        for count in (2, 1):
            key = Ed25519PrivateKey.generate()
            public_key = key.public_key().public_bytes_raw().hex()
            nonce = registry.challenge(public_key)["nonce"]
            outputs = tuple(OutputReport(output_id=f"HDMI-A-{i+1}", width_px=1920, height_px=1080)
                            for i in range(count))
            registry.enroll(Enrollment(public_key=public_key, nonce=nonce, outputs=outputs,
                                      signature=base64.b64encode(key.sign(
                                          enrollment_message(nonce, outputs))).decode()))
        print("Simulated equipment + real PostgreSQL operator demo: http://127.0.0.1:8010", flush=True)
        print("Disposable demo token: " + DEMO_TOKEN, flush=True)
        uvicorn.run(create_app(db, registry.clock, DEMO_TOKEN), host="127.0.0.1", port=8010)
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema)))


if __name__ == "__main__":
    main()
