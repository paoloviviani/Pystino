"""The chat application's backend.

A client of the gateway, not a second path into providers: every model call
here is an ordinary metered ``/v1`` request made with the access token of the
person whose chat message it is. See docs/phase-3-plan.md.
"""
