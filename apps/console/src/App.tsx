import { Notice, Spinner } from "@llmp/ui";
import { Navigate, Route, Routes } from "react-router";
import { RequireAdmin } from "./components/RequireAdmin";
import { Shell } from "./components/Shell";
import { NotAuthenticatedError } from "./lib/api";
import { useMe } from "./lib/queries";
import { AdminModelDetail } from "./routes/AdminModelDetail";
import { AdminModels } from "./routes/AdminModels";
import { AdminProviders } from "./routes/AdminProviders";
import { AdminQuotas } from "./routes/AdminQuotas";
import { AdminRedaction } from "./routes/AdminRedaction";
import { AdminRedactionRule } from "./routes/AdminRedactionRule";
import { Admin } from "./routes/Admin";
import { AdminReports } from "./routes/AdminReports";
import { AdminUsers } from "./routes/AdminUsers";
import { Login } from "./routes/Login";
import { NotFound } from "./routes/NotFound";
import { Overview } from "./routes/Overview";
import { Reports } from "./routes/Reports";
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
    // No session, or it expired. What to show is the login page's decision,
    // not this one: it asks which sign-in methods the deployment offers and
    // adapts — auto-redirect when OIDC is the only way in, a form when a
    // local password works, both when they are both enabled (ADR 0043).
    return <Login />;
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
        {/* A person's own consumption. Not admin-gated: everyone has usage,
            including administrators, whose own spend is not administration. */}
        <Route path="/reports" element={<Reports />} />

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
        {/* The way in. A landing page, not a redirect to the first section:
            the six screens under here are not a sequence. */}
        <Route
          path="/admin"
          element={
            <RequireAdmin me={me_}>
              <Admin />
            </RequireAdmin>
          }
        />
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
        {/* One rule, reached by clicking it in the list on the Redaction
            screen. `new` shares the route: it cannot collide with a uuid, and a
            second route would duplicate every line of the form. */}
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
