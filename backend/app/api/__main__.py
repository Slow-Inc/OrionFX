"""Local operator CLI.

    uv run python -m app.api set-password --db data/orionfx.db
    uv run python -m app.api serve --db data/orionfx.db

`serve` binds the host from the stored config (127.0.0.1 unless the owner
changed it). The password is read from the terminal, never from argv.
"""

import argparse
import getpass
from pathlib import Path

import uvicorn

from app.api.app import ApiSettings, create_app
from app.api.auth import set_owner_password
from app.storage.config_store import ConfigStore
from app.storage.db import connect, migrate


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.api")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("set-password", "serve"):
        p = sub.add_parser(name)
        p.add_argument("--db", type=Path, default=Path("data/orionfx.db"))
    sub.choices["serve"].add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(args.db)
    migrate(conn)

    if args.cmd == "set-password":
        first = getpass.getpass("New owner password (min 12 chars): ")
        if first != getpass.getpass("Repeat: "):
            raise SystemExit("passwords differ")
        set_owner_password(conn, first)
        print("owner password set; existing sessions revoked")
        return

    _, cfg = ConfigStore(conn).current()
    conn.close()
    settings = ApiSettings(
        db_path=args.db,
        bind_host=cfg.system.bind_host,
        port=args.port,
        dashboard_origin=cfg.system.dashboard_origin,
    )
    uvicorn.run(create_app(settings), host=settings.bind_host, port=settings.port)


if __name__ == "__main__":
    main()
