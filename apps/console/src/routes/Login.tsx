import { Button, Notice, Spinner } from "@llmp/ui";
import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { useSearchParams } from "react-router";
import { Message } from "../components/Message";
import { fetchAuthMethods, loginWith } from "../lib/api";
import { FORM_STACK, LOGIN_CARD, LOGIN_CENTRE } from "../lib/layout";
import { m } from "../paraglide/messages.js";

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
        <Spinner label={m["login.loading_page"]()} />
      </div>
    );
  }

  if (methods.error) {
    return (
      <div className={LOGIN_CENTRE}>
        <Notice tone="danger" title={m["login.gateway_unreachable.title"]()}>
          {/* i18n: server text — the API's own error message, not the console's. */}
          {methods.error instanceof Error
            ? methods.error.message
            : m["login.gateway_unreachable.unknown_error"]()}
        </Notice>
      </div>
    );
  }

  if (providers.length === 0) {
    return (
      <div className={LOGIN_CENTRE}>
        <Notice tone="danger" title={m["login.no_provider.title"]()}>
          <Message message={m["login.no_provider.body"]} markup={{ code: (children) => <code>{children}</code> }} />
        </Notice>
      </div>
    );
  }

  if (providers.length === 1) {
    return (
      <div className={LOGIN_CENTRE}>
        <Spinner label={m["login.redirecting"]()} />
      </div>
    );
  }

  return (
    <div className={LOGIN_CENTRE}>
      <div className={LOGIN_CARD}>
        <h1 className="text-heading font-semibold">{m["login.heading"]()}</h1>
        <div className={FORM_STACK}>
          {providers.map((provider) => (
            <Button key={provider.name} variant="primary" onClick={() => loginWith(provider.name, next)}>
              {m["login.provider_button"]({ provider: provider.name })}
            </Button>
          ))}
        </div>
      </div>
    </div>
  );
}
