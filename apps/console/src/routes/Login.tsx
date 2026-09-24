import { Button, Notice, Spinner } from "@llmp/ui";
import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { useSearchParams } from "react-router";
import { fetchAuthMethods, loginWith } from "../lib/api";
import { FORM_STACK, LOGIN_CARD, LOGIN_CENTRE } from "../lib/layout";

/**
 * The sign-in page: OpenID Connect only (ADR 0088, decision D3).
 *
 * Reaches the reader in exactly one way: `App` renders it when the API answered
 * 401. `GET /auth/methods` names the enabled providers:
 *
 * - **one:** the browser is sent straight there — a page whose only content is
 *   a button that does the inevitable is a wasted step;
 * - **several:** one button each; guessing would be a login to the wrong
 *   directory (before this page showed "Redirecting…" forever in that case);
 * - **none:** a deployment with no way in, which this page can only report.
 *
 * `?next` is carried into the redirect, so a deep link survives signing in.
 * There is no password form: every person signs in through a provider, and a
 * deployment with no administrator recovers with `pystino admin grant`.
 */
export function Login() {
  const [searchParams] = useSearchParams();
  const next = searchParams.get("next") ?? "/";
  const methods = useQuery({
    queryKey: ["auth-methods"],
    queryFn: fetchAuthMethods,
    // The answer is a property of the deployment, not of the session.
    retry: false,
    staleTime: Infinity,
  });
  const providers = methods.data?.providers ?? [];

  useEffect(() => {
    if (providers.length === 1) loginWith(providers[0]!.name, next);
  }, [providers, next]);

  if (methods.isPending) {
    return (
      <div className={LOGIN_CENTRE}>
        <Spinner label="Loading the sign-in page" />
      </div>
    );
  }

  if (methods.error) {
    return (
      <div className={LOGIN_CENTRE}>
        <Notice tone="danger" title="The console could not reach the gateway">
          {methods.error instanceof Error ? methods.error.message : "Unknown error."}
        </Notice>
      </div>
    );
  }

  if (providers.length === 0) {
    return (
      <div className={LOGIN_CENTRE}>
        <Notice tone="danger" title="No identity provider is enabled">
          Everyone signs in through an OpenID Connect provider, and this deployment has none
          enabled. An operator can add one in the console after{" "}
          <code>pystino admin grant &lt;email&gt;</code>, or set GATEWAY_OIDC__* and restart.
        </Notice>
      </div>
    );
  }

  if (providers.length === 1) {
    return (
      <div className={LOGIN_CENTRE}>
        <Spinner label="Redirecting to sign in" />
      </div>
    );
  }

  return (
    <div className={LOGIN_CENTRE}>
      <div className={LOGIN_CARD}>
        <h1 className="text-lg font-semibold">Sign in</h1>
        <div className={FORM_STACK}>
          {providers.map((provider) => (
            <Button key={provider.name} variant="primary" onClick={() => loginWith(provider.name, next)}>
              Sign in with {provider.name}
            </Button>
          ))}
        </div>
      </div>
    </div>
  );
}
