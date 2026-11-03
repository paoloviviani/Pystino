import { createContext, useContext } from "react";

/**
 * Whose catalogue a screen manages.
 *
 * The provider and model screens are one implementation with two audiences:
 * administrators manage the deployment's (`/api/admin/...`), and a user holding
 * the BYOK capability manages their own private ones (`/api/me/...`), which
 * accept the same payloads and answer in the same shapes. A context rather than
 * a prop threaded through every dialog, so the hooks beneath (admin.ts) pick
 * their endpoints and cache keys from where they are rendered and a copy of a
 * screen cannot drift from the original.
 */
export type Scope = "admin" | "me";

export const ScopeContext = createContext<Scope>("admin");

export function useScopeName(): Scope {
  return useContext(ScopeContext);
}
