"""`pystino`: initialise, bootstrap, check and upgrade a deployment (ADR 0086).

Runs from the gateway image, so a host needs Docker and nothing else, and from a
checkout (`uv run pystino …`) in development. It replaces both bash installers:
what they spent most of their length on — two Caddy shapes, a CA trust bundle,
three identity providers, a resumable two-phase bring-up — no longer exists, and
what remains is writing one `.env` and a few idempotent steps inside the stack.
"""
