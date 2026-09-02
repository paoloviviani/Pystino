/**
 * Self-service password reset, both halves of the journey (ADR 0049).
 *
 * One file, two modes: without a token this page asks for the email and says
 * "check your inbox" — the same answer whether or not the address exists,
 * because the endpoint's refusal to enumerate is only worth having if the page
 * behind it does not re-introduce the leak. With `?token=` it is the confirm
 * form: type the new password, sign in.
 */

import { Button, Input, Notice } from "@llmp/ui";
import { useMutation } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { request } from "../lib/api";
import { FORM_STACK, LOGIN_CENTRE, LOGIN_CARD } from "../lib/layout";

export function PasswordReset() {
  const [params] = useSearchParams();
  const token = params.get("token");
  return token ? <Confirm token={token} /> : <Request />;
}

function Request() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);

  const send = useMutation({
    mutationFn: (address: string) =>
      request<{ status: string }>("/auth/password-reset", {
        method: "POST",
        body: { email: address },
      }),
    onSuccess: () => setSent(true),
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    send.mutate(email.trim());
  };

  return (
    <div className={LOGIN_CENTRE}>
      <section className={LOGIN_CARD}>
        <h1 className="m-0 text-lg font-medium">Reset your password</h1>
        {sent ? (
          <>
            <Notice tone="info" title="Check your inbox">
              If an account exists for that address, a reset link is on its way.
              It is valid for one hour.
            </Notice>
            <p className="m-0 text-sm text-ink-muted">
              No mail? Ask an administrator — reset by email may not be turned on
              for this deployment.{" "}
              <Link to="/login" className="text-accent no-underline">
                Back to sign in
              </Link>
              .
            </p>
          </>
        ) : (
          <>
            {/* The gateway's 503 says the feature is off — a deployment fact,
                not an account fact, so showing it leaks nothing (ADR 0049). */}
            {send.error ? (
              <Notice tone="danger" title="Could not send the reset link">
                {send.error instanceof Error ? send.error.message : "Unknown error."}
              </Notice>
            ) : null}
            <form className={FORM_STACK} onSubmit={submit}>
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
              <Button type="submit" variant="primary" busy={send.isPending}>
                Send reset link
              </Button>
            </form>
            <p className="m-0 text-sm text-ink-muted">
              Local accounts only — an account signed in through the identity
              provider gets its password there.{" "}
              <Link to="/login" className="text-accent no-underline">
                Back to sign in
              </Link>
              .
            </p>
          </>
        )}
      </section>
    </div>
  );
}

function Confirm({ token }: { token: string }) {
  const navigate = useNavigate();
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const confirm = useMutation({
    mutationFn: ( newPassword: string) =>
      request<{ status: string }>("/auth/password-reset/confirm", {
        method: "POST",
        body: { token, password: newPassword },
      }),
    onSuccess: () => setDone(true),
    onError: (caught) =>
      setError(caught instanceof Error ? caught.message : "Unknown error."),
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    confirm.mutate(password);
  };

  return (
    <div className={LOGIN_CENTRE}>
      <section className={LOGIN_CARD}>
        <h1 className="m-0 text-lg font-medium">Choose a new password</h1>
        {done ? (
          <>
            <Notice tone="info" title="Password updated">
              The old password no longer works.
            </Notice>
            <Button variant="primary" onClick={() => navigate("/login", { replace: true })}>
              Sign in
            </Button>
          </>
        ) : (
          <>
            {error ? (
              <Notice tone="danger" title="Could not reset the password">
                {error}
              </Notice>
            ) : null}
            <form className={FORM_STACK} onSubmit={submit}>
              <Input
                label="New password"
                type="password"
                name="password"
                autoComplete="new-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                required
                autoFocus
              />
              <Button type="submit" variant="primary" busy={confirm.isPending}>
                Set password
              </Button>
            </form>
          </>
        )}
      </section>
    </div>
  );
}
