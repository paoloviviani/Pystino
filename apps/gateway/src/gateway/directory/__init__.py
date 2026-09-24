"""Batch user sync from identity providers (ADR 0088 draft).

OIDC tells us about a person only when they sign in. A directory knows about
everyone, including who has left. This package brings that knowledge in:
adapters list a directory (Authelia's users file, Keycloak's admin REST) or
receive its pushes (SCIM, `routers/scim.py`), everything lands in one mirror
(`directory_entries`), and one engine applies the mirror to accounts under the
same provenance rules a login follows — a directory grants and revokes only
what it granted, never deletes anyone, and never deactivates half the
deployment because it returned an empty list.
"""
