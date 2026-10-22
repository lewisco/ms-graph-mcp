"""Opt-in audit records for Graph tool calls: validated identity and outcome only."""

import json
import logging
import sys
import time

from ms_graph_mcp.auth import DelegatedAccessToken

LOGGER_NAME = "ms_graph_mcp.audit"


class AuditLog:
    def __init__(self, enabled: bool):
        self._logger = self._configure() if enabled else None

    @staticmethod
    def _configure() -> logging.Logger:
        # Uvicorn configures only its own loggers; without a handler, INFO records are dropped.
        logger = logging.getLogger(LOGGER_NAME)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
        return logger

    def tool_call(
        self,
        tool: str,
        identity: DelegatedAccessToken | None,
        outcome: str,
        started: float,
        graph_status: int | None = None,
    ) -> None:
        if self._logger is None:
            return
        # Never add paths, queries, bodies, handles, URLs or tokens: they can carry content.
        record: dict[str, object] = {"event": "tool_call", "tool": tool, "outcome": outcome}
        if graph_status is not None:
            record["graph_status"] = graph_status
        if identity is not None:
            record["oid"] = identity.subject
            if identity.username:
                record["username"] = identity.username
            record["azp"] = identity.client_id
        record["duration_ms"] = round((time.monotonic() - started) * 1000)
        self._logger.info(json.dumps(record, separators=(",", ":")))
