"""HTTP routers.

``/v1`` is the OpenAI-compatible surface, authenticated with API keys.
``/api`` and ``/auth`` are the management surface, authenticated with OIDC.
"""

from gateway.routers import admin, auth, chat, health, models

__all__ = ["admin", "auth", "chat", "health", "models"]
