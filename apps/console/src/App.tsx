import { Notice, Spinner } from "@llmp/ui";
import { Route, Routes } from "react-router";
import { RequireAdmin } from "./components/RequireAdmin";
import { Shell } from "./components/Shell";
import { NotAuthenticatedError, login } from "./lib/api";
import { useMe } from "./lib/queries";
import { AdminModels } from "./routes/AdminModels";
import { AdminPricing } from "./routes/AdminPricing";
import { AdminProviders } from "./routes/AdminProviders";
import { AdminQuotas } from "./routes/AdminQuotas";
import { AdminRedaction } from "./routes/AdminRedaction";
import { AdminReports } from "./routes/AdminReports";
import { AdminUsers } from "./routes/AdminUsers";
import { NotFound } from "./routes/NotFound";
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

  const me_ = me.data;

  return (
    <Shell me={me_}>
      <Routes>
        <Route path="/" element={<Overview me={me_} />} />

        {/* Wrapped rather than conditionally registered: a non-admin who follows
            a bookmarked link should be told, not handed a blank 404 that reads
            as a broken deploy. The API refuses them regardless — this is
            courtesy, not enforcement. */}
        <Route
          path="/admin/models"
          element={
            <RequireAdmin me={me_}>
              <AdminModels />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/providers"
          element={
            <RequireAdmin me={me_}>
              <AdminProviders />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/pricing"
          element={
            <RequireAdmin me={me_}>
              <AdminPricing />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/quotas"
          element={
            <RequireAdmin me={me_}>
              <AdminQuotas />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/redaction"
          element={
            <RequireAdmin me={me_}>
              <AdminRedaction />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/reports"
          element={
            <RequireAdmin me={me_}>
              <AdminReports />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/users"
          element={
            <RequireAdmin me={me_}>
              <AdminUsers />
            </RequireAdmin>
          }
        />

        <Route path="*" element={<NotFound />} />
      </Routes>
    </Shell>
  );
}
