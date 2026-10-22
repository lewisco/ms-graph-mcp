import json
import logging
import time

from ms_graph_mcp.audit import LOGGER_NAME, AuditLog


def test_enabled_audit_log_writes_one_json_line_to_stdout(capsys, monkeypatch):
    logger = logging.getLogger(LOGGER_NAME)
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "propagate", True)
    AuditLog(True).tool_call("graph_read", None, "authorization_required", time.monotonic())
    logger.setLevel(logging.NOTSET)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert isinstance(record.pop("duration_ms"), int)
    assert record == {
        "event": "tool_call",
        "tool": "graph_read",
        "outcome": "authorization_required",
    }


def test_disabled_audit_log_configures_nothing(monkeypatch):
    logger = logging.getLogger(LOGGER_NAME)
    monkeypatch.setattr(logger, "handlers", [])
    AuditLog(False).tool_call("graph_read", None, "success", time.monotonic())
    assert logger.handlers == []
