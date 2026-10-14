"""Logging filter that redacts ``ADMIN_PATH_SECRET`` from every log record.

The secret appears in request paths (uvicorn access log) and could leak into
exception messages. This filter replaces it with ``***`` in both the format
string and the format args so it never reaches a handler or a log file.
"""

from __future__ import annotations

import logging
import os


class SecretRedactionFilter(logging.Filter):
    """Replace ``ADMIN_PATH_SECRET`` with ``***`` in a log record's message."""

    def filter(self, record: logging.LogRecord) -> bool:
        secret = os.getenv("ADMIN_PATH_SECRET")
        if not secret:
            return True
        if isinstance(record.msg, str) and secret in record.msg:
            record.msg = record.msg.replace(secret, "***")
        if record.args:
            record.args = tuple(
                a.replace(secret, "***") if isinstance(a, str) else a
                for a in record.args
            )
        # A pre-formatted exception text (logger.exception) may embed the secret.
        if record.exc_text and secret in record.exc_text:
            record.exc_text = record.exc_text.replace(secret, "***")
        return True


def install_secret_filter() -> None:
    """Attach the redaction filter to the app's + uvicorn's loggers.

    Idempotent: each logger gets the filter at most once.
    """
    secret = os.getenv("ADMIN_PATH_SECRET")
    if not secret:
        return
    flt = SecretRedactionFilter()
    names = (
        None,  # root
        "uvicorn",
        "uvicorn.access",
        "uvicorn.error",
        "freeroomfinder",
        "freeroomfinder.admin",
        "freeroomfinder.scheduler_api",
        "freeroomfinder.envelopes",
        "freeroomfinder.jobs",
        "freeroomfinder.rmp",
        "freeroomfinder.section_scraper",
        "freeroomfinder.scraper",
        "freeroomfinder.llm",
        "freeroomfinder.solver",
        "freeroomfinder.database",
        "freeroomfinder.matching",
    )
    for name in names:
        lg = logging.getLogger(name)
        if flt not in lg.filters:
            lg.addFilter(flt)
        for handler in lg.handlers:
            if flt not in handler.filters:
                handler.addFilter(flt)
