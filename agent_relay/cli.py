from __future__ import annotations

import argparse
from pathlib import Path

from .config import app_home, config_path, load_config, save_binding, write_example
from .gmail import GoogleGmailGateway
from .supervisor import Supervisor, write_completion_receipt
from .ui import RelayApp
from .wake import CodexAppServerWakeAdapter, CodexCliWakeAdapter, CodexTarget, MockWakeAdapter


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent-relay")
    parser.add_argument("command", choices=("init", "ui", "complete", "complete-diagnostic", "bind"), nargs="?", default="ui")
    parser.add_argument("--lease-id")
    parser.add_argument("--target-id")
    parser.add_argument("--target-type", default="codex-cli")
    parser.add_argument("--chat-url")
    parser.add_argument("--handoff-succeeded", action="store_true")
    parser.add_argument("--dev-session-id")
    parser.add_argument("--completion-token")
    args = parser.parse_args(argv)
    home = app_home()
    if args.command == "init":
        path = config_path(home)
        if path.exists(): print(f"EXISTS {path}")
        else:
            home.mkdir(parents=True, exist_ok=True); write_example(path, Path.cwd()); print(f"CREATED {path}")
        return 0
    if args.command == "bind":
        if not args.target_id:
            parser.error("bind requires --target-id")
        print(f"BOUND {save_binding(home, target_id=args.target_id, target_type=args.target_type, chat_url=args.chat_url or 'https://chatgpt.com/c/6a818a0c-5208-83ee-95cd-fd558d66ecc9', dev_session_id=args.dev_session_id or '')}")
        return 0
    if args.command == "complete-diagnostic":
        if not args.lease_id or not args.completion_token:
            parser.error("complete-diagnostic requires --lease-id and --completion-token")
        config = load_config(home)
        path = write_completion_receipt(config.local_project_storage, args.lease_id, args.completion_token)
        print(f"DIAGNOSTIC_COMPLETION_RECORDED {path}")
        return 0
    config = load_config(home)
    if args.command == "complete":
        if not args.lease_id:
            parser.error("complete requires --lease-id")
        relay = Supervisor(config, None, MockWakeAdapter())
        relay.write_completion_record(args.lease_id, handoff_succeeded=args.handoff_succeeded)
        print("COMPLETION_RECORDED")
        return 0
    target = CodexTarget(config.target_type, config.target_id, config.target_label, config.repo_path)
    if config.target_type == "mock":
        adapter = MockWakeAdapter()
    elif config.target_type == "codex-app-server":
        adapter = CodexAppServerWakeAdapter(target, config.local_project_storage / "logs", config.codex_command, config.local_project_storage, config.dev_session_id)
    else:
        adapter = CodexCliWakeAdapter(target, config.local_project_storage / "logs", config.codex_command)
    app = RelayApp(Supervisor(config, GoogleGmailGateway(config.gmail_auth_home), adapter))
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
