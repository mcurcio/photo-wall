"""Create private local deployment settings, never overwrite an existing deployment."""

import os
import secrets
from pathlib import Path


def main():
    path = Path(__file__).resolve().parents[1] / ".env"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print("Existing .env preserved.")
        return
    with os.fdopen(fd, "w") as stream:
        stream.write("PHOTO_WALL_DB_PASSWORD=" + secrets.token_hex(24) + "\n")
        stream.write("PHOTO_WALL_ADMIN_TOKEN=" + secrets.token_hex(32) + "\n")
    print("Created private .env. Use its operator token to sign into the local interface.")


if __name__ == "__main__":
    main()
