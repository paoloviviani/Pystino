/**
 * The sign-in screen, driven by what the deployment actually offers.
 *
 * Ask first, render second: `/api/auth/methods` says which doors exist, and
 * this component shows the password form, the identity-provider redirect, or
 * both. Before ADR 0046 this app had exactly one behaviour — bounce to
 * `/auth/login`, the OIDC redirect — and on a deployment with no identity
 * provider that bounce landed on `{"detail":"Sign-in is unavailable."}`:
 * an error page where a login should be, with nothing anywhere saying a
 * password would work.
 *
 * The gateway's own words come back on failure ("Incorrect email or
 * password.") and are shown as they are — the same discipline the turn
 * errors already follow.
 */

import { Button, Input, Notice } from "@llmp/ui";
import { useEffect, useState, type FormEvent } from "react";

import logo from "../assets/logo.png";
import { BASE, type AuthMethods, getAuthMethods, signInLocal } from "../lib/api";
import styles from "./SignIn.module.css";

export function SignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [methods, setMethods] = useState<AuthMethods | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getAuthMethods()
      .then((available) => {
        if (cancelled) return;
        setMethods(available);
        // OIDC-only: the provider's page *is* the form, and the app used to
        // go there straight away — keep that journey exactly as it was.
        if (available.oidc && !available.local) {
          window.location.assign(loginUrl());
        }
      })
      .catch(() => {
        if (!cancelled) setError("Sign-in is unavailable right now.");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await signInLocal(email, password);
      onSignedIn();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Sign-in failed.");
    } finally {
      setBusy(false);
    }
  };

  if (methods === null) {
    return (
      <div className={styles.wrap}>
        {error ? (
          <Notice tone="danger" title="Sign-in">
            {error}
          </Notice>
        ) : null}
      </div>
    );
  }

  return (
    <div className={styles.wrap}>
      <form className={styles.card} onSubmit={(event) => void submit(event)}>
        <img className={styles.logo} src={logo} alt="" width={64} height={64} />
        <h1 className={styles.heading}>Sign in</h1>
        {error ? (
          <Notice tone="danger" title="Sign-in failed">
            {error}
          </Notice>
        ) : null}
        <Input
          label="Email"
          type="email"
          autoComplete="username"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          required
        />
        <Input
          label="Password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          required
        />
        <Button variant="primary" type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </Button>
        {methods.oidc ? (
          // Both doors: the form stays primary and the provider is a link,
          // because the link's destination is outside this app's control —
          // an error page there must not look like ours went away.
          <a className={styles.sso} href={loginUrl()}>
            Sign in with single sign-on
          </a>
        ) : null}
      </form>
    </div>
  );
}

function loginUrl(): string {
  return `${BASE}/auth/login?next=${encodeURIComponent(window.location.pathname)}`;
}
