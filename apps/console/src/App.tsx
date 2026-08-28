import { Notice, Spinner } from "@llmp/ui";
import { Navigate, Route, Routes } from "react-router";
import { RequireAdmin } from "./components/RequireAdmin";
import { Shell } from "./components/Shell";
import { NotAuthenticatedError, login } from "./lib/api";
import { useMe } from "./lib/queries";
import { AdminModelDetail } from "./routes/AdminModelDetail";
import { AdminModels } from "./routes/AdminModels";
import { AdminProviders } from "./routes/AdminProviders";
import { AdminQuotas } from "./routes/AdminQuotas";
import { AdminRedaction } from "./routes/AdminRedaction";
import { AdminRedactionRule } from "./routes/AdminRedactionRule";
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
          path="/admin/models/:modelId"
          element={
            <RequireAdmin me={me_}>
              <AdminModelDetail />
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
        {/* Pricing was its own screen, with its own model picker, until it
            became a section of the model's page — a price is a fact about a
            model, and asking for one meant navigating away and choosing the
            model again. Kept as a redirect rather than deleted: the tab existed
            long enough to be bookmarked, and a 404 would read as a broken
            deployment rather than as a screen that moved. */}
        <Route path="/admin/pricing" element={<Navigate to="/admin/models" replace />} />
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
        {/* Reached from the Redaction screen, not from the navigation: the nav
            is deliberately short, and this is the second question about
            redaction rather than a place anyone starts. */}
        <Route
          path="/admin/redaction/rules/:ruleId"
          element={
            <RequireAdmin me={me_}>
              <AdminRedactionRule />
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
