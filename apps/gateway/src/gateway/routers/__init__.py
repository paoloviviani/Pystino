"""HTTP routers.

``/v1`` is the OpenAI-compatible surface, authenticated with API keys.
``/auth`` and ``/api/me`` are the human surface, authenticated with OIDC.
``/api/admin`` is the same surface restricted to administrators.
"""

from gateway.routers import admin, auth, chat, health, me, models

__all__ = ["admin", "auth", "chat", "health", "me", "models"]
