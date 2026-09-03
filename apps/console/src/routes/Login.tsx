import { Button, Input, Notice, Spinner } from "@llmp/ui";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { fetchAuthMethods, localLogin, loginWith, NotAuthenticatedError } from "../lib/api";
import { FORM_STACK, LOGIN_CARD, LOGIN_CENTRE } from "../lib/layout";

/**
 * The adaptive sign-in page (ADR 0043).
 *
 * Reaches the reader in exactly one way: `App` renders it when the API answered
 * 401. What it shows is decided by `GET /auth/methods`:
 *
 * - **OIDC only** (every deployment before local auth existed): the same
 *   auto-redirect there has always been — a page whose only content is a
 *   button that does the inevitable is a wasted step.
 * - **Local only, or both:** an email + password form. With both enabled, a
 *   "Sign in with SSO" link sits beside it, and the redirect stops being
 *   automatic — auto-redirecting past a working local form would make the
 *   password path unreachable without typing a URL by hand.
 *
 * `?next` survives the journey in both directions: carried into the OIDC
 * redirect, and honoured by the router after a local sign-in.
 */
export function Login() {
  const [searchParams] = useSearchParams();
  const next = searchParams.get("next") ?? "/";
  const methods = useQuery({
    queryKey: ["auth-methods"],
    queryFn: fetchAuthMethods,
    // The answer is a property of the deployment, not of the session; retrying
    // a 4xx/5xx here only delays the form.
    retry: false,
    staleTime: Infinity,
  });

  useEffect(() => {
    // OIDC-only, and exactly one provider: sent straight there, as this screen
    // always worked before local auth existed. With several providers and no
    // local form, picking is the reader's — a guess would be a login to the
    // wrong directory.
    if (
      methods.data &&
      !methods.data.local &&
      methods.data.providers.length === 1
    ) {
      loginWith(methods.data.providers[0]!.name, next);
    }
  }, [methods.data, next]);

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

  // Both off is a deployment wired without any way in — not a state this page
  // can fix, only report.
  if (!methods.data.local && !methods.data.oidc) {
    return (
      <div className={LOGIN_CENTRE}>
        <Notice tone="danger" title="No sign-in method is enabled">
          This deployment has neither local authentication nor OIDC configured.
        </Notice>
      </div>
    );
  }

  if (!methods.data.local && methods.data.oidc) {
    return (
      <div className={LOGIN_CENTRE}>
        <Spinner label="Redirecting to sign in" />
      </div>
    );
  }

  return (
    <LocalLoginForm
      next={next}
      ssoAvailable={methods.data.oidc}
      ssoProviders={methods.data.providers ?? []}
    />
  );
}

function LocalLoginForm({
  next,
  ssoAvailable,
  ssoProviders,
}: {
  next: string;
  ssoAvailable: boolean;
  ssoProviders: { name: string; issuer: string }[];
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await localLogin(email, password);
      // The cookie is set; the cached failure from before it was must not be
      // what the next screen reads.
      await queryClient.invalidateQueries();
      navigate(next, { replace: true });
    } catch (caught) {
      // The gateway's message is specific by design ("Too many failed
      // sign-in attempts" versus "Incorrect email or password") and safe to
      // show; swallowing it into a generic string would hide the one thing
      // the reader can act on.
      setError(caught instanceof Error ? caught.message : "Sign-in failed.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className={LOGIN_CENTRE}>
      <section className={LOGIN_CARD}>
        <h1 className="m-0 text-lg font-medium">Sign in</h1>
        {error ? (
          <Notice tone="danger" title="Sign-in failed">
            {error}
          </Notice>
        ) : null}
        <form className={FORM_STACK} onSubmit={handleSubmit}>
          {/* autoComplete values are the browser's cue to offer or save the
              credential; without them a password manager offers the wrong
              thing on the wrong field. */}
          <Input
            label="Email"
            type="email"
            name="email"
            autoComplete="username"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            required
            autoFocus
          />
          <Input
            label="Password"
            type="password"
            name="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
          />
          <Button type="submit" variant="primary" busy={submitting} disabled={submitting}>
            Sign in
          </Button>
          <Link to={`/password-reset`} className="text-sm text-accent no-underline">
            Forgot your password?
          </Link>
        </form>
        {ssoAvailable &&
          ssoProviders.map((provider) => (
            <Button
              key={provider.name}
              onClick={() => loginWith(provider.name, next)}
              disabled={submitting}
            >
              Sign in with {provider.name}
            </Button>
          ))}
      </section>
    </div>
  );
}

/**
 * Exported for App: the 401 path renders this page rather than navigating.
 * `NotAuthenticatedError` is re-exported so App keeps one import site.
 */
export { NotAuthenticatedError };
