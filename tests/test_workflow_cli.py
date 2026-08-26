from __future__ import annotations

from chat_courier import cli


def test_workflow_source_participates_in_build_identity():
    assert "workflow.py" in cli._BUILD_COMPONENTS


def test_typed_workflow_commands_are_parser_reachable(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "capabilities", lambda: {"operations": []})
    monkeypatch.setattr(cli, "emit", lambda name, **values: seen.append(name))
    assert cli.main(["courier_capabilities"]) == 0
    assert seen == ["courier_capabilities"]
