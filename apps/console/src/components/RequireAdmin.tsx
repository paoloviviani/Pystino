import { Notice } from "@llmp/ui";
import type { ReactNode } from "react";
import type { Me } from "../lib/types";

export interface RequireAdminProps {
  me: Me;
  children: ReactNode;
}

/**
 * Hides an administrative screen from a non-administrator.
 *
 * Courtesy, not security. Every endpoint behind these screens checks `is_admin`
 * itself, and must: anything enforced only in the browser is enforced by whoever
 * has the browser. What this prevents is a confusing page full of 403s.
 */
export function RequireAdmin({ me, children }: RequireAdminProps) {
  if (!me.is_admin) {
    // The role follows a group in the identity provider, not a setting here.
    return (
      <Notice tone="warn" title="Administrators only">
        This section needs the administrator role. Ask whoever manages it in the identity
        provider.
      </Notice>
    );
  }
  return <>{children}</>;
}
