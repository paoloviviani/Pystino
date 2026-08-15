import { Notice, Spinner } from "@llmp/ui";
import { Route, Routes } from "react-router";
import { Shell } from "./components/Shell";
import { NotAuthenticatedError, login } from "./lib/api";
import { useMe } from "./lib/queries";
import { Overview } from "./routes/Overview";
import styles from "./App.module.css";

export function App() {
  const me = useMe();

  if (me.isPending) {
    return (
      <div className={styles.centre}>
        <Spinner label="Signing you in" />
      </div>
    );
  }

  if (me.error instanceof NotAuthenticatedError) {
    // No session, or it expired. Sent straight to the identity provider rather
    // than shown a login button: there is only one way in, so a page whose only
    // content is a button that does the inevitable is a wasted step.
    login();
    return (
      <div className={styles.centre}>
        <Spinner label="Redirecting to sign in" />
      </div>
    );
  }

  if (me.error || !me.data) {
    return (
      <div className={styles.centre}>
        <Notice tone="danger" title="The console could not start">
          {me.error instanceof Error ? me.error.message : "The gateway did not respond."}
        </Notice>
      </div>
    );
  }

  return (
    <Shell me={me.data}>
      <Routes>
        <Route path="/" element={<Overview me={me.data} />} />
        {/* Admin routes arrive next; until then an unknown path lands on the
            overview rather than a blank page. */}
        <Route path="*" element={<Overview me={me.data} />} />
      </Routes>
    </Shell>
  );
}
