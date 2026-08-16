from __future__ import annotations

import argparse
from pathlib import Path

from .config import app_home, config_path, load_config, write_example
from .gmail import GoogleGmailGateway
from .supervisor import Supervisor
from .ui import RelayApp
from .wake import MockWakeAdapter


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent-relay")
    parser.add_argument("command", choices=("init", "ui"), nargs="?", default="ui")
    args = parser.parse_args(argv)
    home = app_home()
    if args.command == "init":
        path = config_path(home)
        if path.exists(): print(f"EXISTS {path}")
        else:
            home.mkdir(parents=True, exist_ok=True); write_example(path, Path.cwd()); print(f"CREATED {path}")
        return 0
    config = load_config(home)
    app = RelayApp(Supervisor(config, GoogleGmailGateway(config.gmail_auth_home), MockWakeAdapter()))
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
