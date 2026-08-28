"""Entry point. The container runs ``chat-api``; the app is built by lifespan."""

from __future__ import annotations

import logging
import os
import sys

from chat_api.config import Settings


def build_app() -> object:
    """Factory for uvicorn.

    Only constructs; everything with a connection in it is wired by the
    lifespan handler, because there is no event loop yet at factory time.
    """
    from chat_api.main import create_app

    return create_app(Settings())


def main() -> int:
    import uvicorn

    settings = Settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    uvicorn.run(
        "chat_api.cli:build_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104  (bound inside a container; compose publishes it)
        port=int(os.environ.get("CHAT_PORT", "8100")),
        # uvicorn trusts forwarded headers from 127.0.0.1 only, and behind the
        # compose network the proxy arrives from 172.x — the trap ADR 0035
        # records, which shows up as https logins failing at the last hop.
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
