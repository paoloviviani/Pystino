# 0021 — Code execution sandbox: gVisor first, WASM rejected as the boundary

- Status: accepted (decision recorded; **not implemented** — Phase 4+)
- Date: 2026-08-14

## Context

The chat UI needs a code execution sandbox. The brief asked for genuinely current
information comparing gVisor-hardened containers, Firecracker-based options and WASM
(Pyodide/Deno) for a **self-hosted** deployment, and explicitly asked not to just pick
the most popular.

The threat model matters more than the benchmark. This runs code an LLM generated from
input that may itself be attacker-controlled — a retrieved document, a pasted error
message, a web search result. Treat it as hostile.

## Findings (verified 2026-08-14)

### The most important finding: WASM is not a host boundary

- **CVE-2026-24002** (Grist, CVSS **9.0**): setting `GRIST_SANDBOX_FLAVOR=pyodide`
  allowed **unauthenticated remote code execution**. The maintainers' own words:
  *"pyodide on node does not have a useful sandbox barrier."* Fixed in 1.7.9 by running
  Pyodide **under Deno**; the documented workaround was **switch to the gVisor sandbox**.
- n8n had a comparable advisory for its Pyodide-based Python node.
- A further Pyodide escape (CVE-2026-5752) was circulating during 2026.

The distinction to hold on to: WebAssembly gives **memory safety within the module**. It
says nothing about what the *host runtime* exposes. Node exposes plenty. Deno's permission
model mediates it, which is why the fix worked — but that means the security comes from
Deno, not from WASM.

### The isolation options

| Option | Licence | Boundary | Cost |
|---|---|---|---|
| Docker/runc alone | Apache-2.0 | Shared kernel | **Insufficient** for untrusted code — the 2026 consensus |
| **gVisor** | **Apache-2.0** (Google) | ~200 syscalls reimplemented in a user-space kernel | 10–30% overhead; incomplete syscall surface; **no nested virtualisation needed** |
| Firecracker / Kata | Apache-2.0 | Hardware-virtualised microVM, separate kernel per VM | <150ms boot, ~5MB overhead; **requires KVM** |
| **microsandbox** | Apache-2.0 | microVM via libkrun | 7.5k stars; README says **"still beta software. Expect breaking changes, missing features, and rough edges."** |
| Pyodide/WASM | MPL-2.0 | Memory safety only | See above |

The genuine security ranking is clear: compromising a microVM requires a **hypervisor**
escape; compromising gVisor requires a bug in a **syscall implementation**. Firecracker is
the stronger boundary, and the published guidance agrees — gVisor for "code an LLM
generated from your inputs", Firecracker or Kata for "code derived from untrusted external
content, or multi-tenant".

## Decision

**gVisor (`runsc`) as the default, with the sandbox behind an interface.**

The deciding factor is not security ranking, it is **deployability**: gVisor needs no
nested virtualisation. Firecracker and Kata need KVM, and a foundation self-hosting on a
VM — cloud or on-premise vSphere — frequently cannot get nested virt enabled. A default
that only works on bare metal is a default that does not work.

So:

- **gVisor by default.** Apache-2.0, RuntimeClass-stable on Kubernetes 1.28+, integrated
  out of the box on GKE Sandbox, and — telling — the escape hatch Grist recommended when
  its WASM sandbox failed.
- **The sandbox sits behind an interface**, so a deployment with bare metal can select a
  microVM backend without touching application code.
- **Pyodide/Deno is not the boundary.** It may be used as a *second* layer inside a
  container, never as the only one.
- **microsandbox is not built on yet.** It is the most interesting self-hostable microVM
  option and its own README says beta. Revisit when it stabilises.

## Consequences

- gVisor's incomplete syscall surface will break some workloads — native extensions and
  unusual I/O are the usual casualties. Expect to maintain a list of what does not run.
- 10–30% overhead is acceptable for interactive code execution and would not be for a
  hot request path.
- **Defence in depth is still required regardless of runtime**, and is where most real
  incidents are actually stopped: no network egress by default, strict CPU/memory/wall-clock
  limits, read-only root filesystem, no credentials in the environment, per-execution
  ephemeral filesystem, and a hard output size cap.
- E2B and Daytona were noted as managed options and are out of scope: this is a
  self-hosted platform, and sending user code to a third party is a data-protection
  decision, not an infrastructure one.
- **Revisit if the deployment gets bare metal or nested virt.** At that point Firecracker
  becomes strictly better and the interface exists to make the switch cheap.
