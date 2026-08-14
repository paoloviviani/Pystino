# 0001 — EUPL-1.2 for first-party code, and the dependency licence policy

- Status: accepted
- Date: 2026-08-14

## Context

All first-party code is licensed **EUPL-1.2**, a requirement from the foundation,
not a preference. That makes inbound dependency licensing a hard constraint rather
than a matter of taste, and the EUPL's compatibility rules are asymmetric in a way
that is easy to get backwards.

The EUPL Appendix lists "Compatible Licences" (GPLv2/v3, AGPLv3, LGPL, MPLv2, OSL,
EPL, CeCILL, LiLiQ-R, EUPL itself). That list governs the **downstream** direction:
it names the licences under which a derivative work combining EUPL code with
theirs may be distributed. It is routinely misread as an inbound allowlist, which
would suggest MIT and Apache-2.0 are unusable. They are not.

Verified against the European Commission's own compatibility matrix
(interoperable-europe.ec.europa.eu):

| Dependency licence | Incorporate / static link | Dynamic link / separate process |
|---|---|---|
| MIT | OK | OK |
| Apache-2.0 | OK | OK |
| BSD-3-Clause | OK | OK |
| MPL-2.0 | OK | OK |
| **AGPL-3.0** | **No** (needs a licensor exception) | **OK** |
| GPL-3.0 | No (upstream) | OK |

## Decision

1. First-party code is **EUPL-1.2**. The `LICENCE` file at the repository root is
   the canonical text, taken verbatim from the SPDX licence list.
2. **Permissive dependencies (MIT, Apache-2.0, BSD) may be used freely**, including
   by direct incorporation.
3. **Strong-copyleft dependencies (AGPL, GPL) may only be used as separate
   processes** talking over a network or IPC boundary — never vendored, imported or
   statically linked. This is why Valkey-or-Redis is a licensing non-issue: the
   gateway speaks to it over a socket.
4. **Non-OSI licences require explicit approval before adoption.** This includes
   source-available licences (SSPL, RSALv2, BUSL), licences with field-of-use or
   revenue restrictions (RAIL-M), branding clauses, and projects requiring a CLA.
5. **Model weights are licensed separately from the code that runs them** and must
   be assessed separately. This is the single most common way a permissive-looking
   stack acquires a non-OSI obligation.

## Consequences

- Things we deliberately kept at arm's length as a result:
  - **Open WebUI** (v0.6.6+, April 2025) added a branding clause and a CLA and is
    not OSI open source. Usable as a *reference* for RAG configurability; no code
    may be copied. Recorded in [0019](0019-document-conversion.md).
  - **Marker** — GPL-3.0 code plus RAIL-M weights with a revenue threshold. Not a
    default. **MinerU** — income-threshold conditions. Both are reachable only
    because document conversion sits behind an HTTP endpoint.
  - **LiteLLM** — MIT core but SSO/SAML/RBAC/audit are Enterprise-only, i.e. exactly
    the features this project is built around. See [0003](0003-build-vs-adopt-gateway.md).
- Every third-party service in `deploy/` must have its licence recorded in its ADR.
  Today: PostgreSQL (PostgreSQL Licence), Valkey (BSD-3-Clause, Linux Foundation).
- A future need for an AGPL library *as a library* is a blocker requiring either a
  licensor exception or a different library. Notice it early.

## Not verified

pgvector's LICENSE file was not read directly during this session. It is widely
reported as the PostgreSQL Licence and is a Phase 3 dependency; verify before it
enters the tree.
