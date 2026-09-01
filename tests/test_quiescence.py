import json
from types import SimpleNamespace

from chat_courier import cli


def event(capsys):
    return json.loads(capsys.readouterr().out.strip())


def test_quiescence_reports_empty_runtime(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "runtime_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "read_owner", lambda: None)

    assert cli.quiescence_command(SimpleNamespace()) == 0
    value = event(capsys)
    assert value["event"] == "courier_quiescence"
    assert value["quiescent"] is True
    assert value["queue_entries"] == []
    assert value["owner"] is None


def test_quiescence_is_read_only_and_fails_on_stale_queue(monkeypatch, tmp_path, capsys):
    queue = {
        "version": 1,
        "next_sequence": 2,
        "entries": [{"project_id": "P", "request_id": "P-1", "state": "queued", "pid": 99}],
    }
    path = tmp_path / "queue.json"
    path.write_text(json.dumps(queue), encoding="utf-8")
    monkeypatch.setattr(cli, "runtime_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "read_owner", lambda: None)
    monkeypatch.setattr(cli, "process_alive", lambda _pid: False)

    assert cli.quiescence_command(SimpleNamespace()) == 1
    value = event(capsys)
    assert value["quiescent"] is False
    assert value["queue_entries"][0]["process_live"] is False
    assert json.loads(path.read_text(encoding="utf-8")) == queue


def test_quiescence_fails_on_owner_or_browser(monkeypatch, tmp_path, capsys):
    owner = SimpleNamespace(
        project_id="P", request_id="P-1", phase="waiting",
        owner_pid=101, browser_pid=202,
    )
    monkeypatch.setattr(cli, "runtime_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "read_owner", lambda: owner)
    monkeypatch.setattr(cli, "process_alive", lambda pid: pid == 202)

    assert cli.quiescence_command(SimpleNamespace()) == 1
    value = event(capsys)
    assert value["owner"]["owner_live"] is False
    assert value["owner"]["browser_live"] is True
    assert value["quiescent"] is False
