"""Provision a local user with explicit roles; never grant rights from model output."""

import argparse
import os
import re
import secrets
import uuid
from pathlib import Path

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.auth import Role, hasher
from packages.persistence import Membership, User, connect
from packages.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True)
    parser.add_argument("--factory", required=True)
    parser.add_argument("--role", action="append", required=True)
    parser.add_argument("--generate-local-password", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", args.username):
        raise SystemExit(
            "Username must contain 1–60 ASCII letters, numbers, underscores or hyphens"
        )
    roles: list[Role] = [TypeAdapter(Role).validate_python(role) for role in set(args.role)]
    password = (
        secrets.token_urlsafe(18)
        if args.generate_local_password
        else os.environ.get("BYOF_BOOTSTRAP_PASSWORD", "")
    )
    if len(password) < 12:
        raise SystemExit(
            "Set BYOF_BOOTSTRAP_PASSWORD (12+ characters), or generate a local password"
        )
    settings = Settings()
    engine = connect(settings.migration_database_url.get_secret_value())
    output: Path | None = None
    try:
        with Session(engine) as db, db.begin():
            if db.scalar(select(User).where(User.username == args.username)):
                raise SystemExit("User exists; refusing to overwrite credentials or roles")
            user_id = str(uuid.uuid4())
            db.add(
                User(
                    user_id=user_id,
                    username=args.username,
                    password_hash=hasher.hash(password),
                    active=True,
                )
            )
            db.flush()
            for role in roles:
                db.add(Membership(user_id=user_id, factory_id=args.factory, role=role))
            if args.generate_local_password:
                output = (
                    Path(__file__).resolve().parents[1] / ".runtime" / f"{args.username}-login.txt"
                )
                with output.open("x", encoding="utf-8") as handle:
                    handle.write(f"Username: {args.username}\nPassword: {password}\n")
                if os.name != "nt":
                    output.chmod(0o600)
    finally:
        engine.dispose()
    print(f"Created {args.username} with explicit roles: {', '.join(roles)}")
    if output:
        print(f"Local sign-in details: {output}; keep this ignored file private")


if __name__ == "__main__":
    main()
