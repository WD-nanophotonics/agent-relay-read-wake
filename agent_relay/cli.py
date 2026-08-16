from __future__ import annotations

import argparse
from pathlib import Path

from .config import app_home, config_path, load_config, write_example
from .gmail import GoogleGmailGateway
from .supervisor import Supervisor
from .ui import RelayApp
from .wake import CodexCliWakeAdapter, CodexTarget, MockWakeAdapter


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent-relay")
    parser.add_argument("command", choices=("init", "ui", "complete"), nargs="?", default="ui")
    parser.add_argument("lease_id", nargs="?")
    args = parser.parse_args(argv)
    home = app_home()
    if args.command == "init":
        path = config_path(home)
        if path.exists(): print(f"EXISTS {path}")
        else:
            home.mkdir(parents=True, exist_ok=True); write_example(path, Path.cwd()); print(f"CREATED {path}")
        return 0
    config = load_config(home)
    if args.command == "complete":
        if not args.lease_id:
            parser.error("complete requires a lease_id")
        relay = Supervisor(config, None, MockWakeAdapter())
        relay.write_completion_record(args.lease_id)
        print("COMPLETION_RECORDED")
        return 0
    target = CodexTarget(config.target_type, config.target_id, config.target_label, config.repo_path)
    adapter = MockWakeAdapter() if config.target_type == "mock" else CodexCliWakeAdapter(target, config.local_project_storage / "logs", config.codex_command)
    app = RelayApp(Supervisor(config, GoogleGmailGateway(config.gmail_auth_home), adapter))
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
