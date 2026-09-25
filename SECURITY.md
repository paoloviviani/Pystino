# Security

Please report vulnerabilities **privately**, not in a public issue:

- email **security@REPLACE-ME.example** (placeholder: the maintainer will fill
  in the address before release), or
- use GitHub's "Report a vulnerability" on this repository.

Include what is affected, how to reproduce it, and what an attacker gains. We
will acknowledge a report within a few days and keep you informed until it is
fixed. Please give us reasonable time to release a fix before disclosing.

Particularly in scope: authentication and API keys, quota and accounting
bypasses, redaction failures (PII reaching an upstream), and anything in
`deploy/` that exposes a service it should not.
