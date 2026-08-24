from __future__ import annotations

import logging

from vinted_deal_finder.logging_config import configure_logging


def test_http_client_request_logging_is_suppressed() -> None:
    configure_logging()

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
