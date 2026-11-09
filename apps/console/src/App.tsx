import { Notice, Spinner } from "@llmp/ui";
import { NoConsole } from "./routes/NoConsole";
import { Navigate, Route, Routes, useParams } from "react-router";
import { RequireAdmin } from "./components/RequireAdmin";
import { Shell } from "./components/Shell";
import { NotAuthenticatedError } from "./lib/api";
import { useMe } from "./lib/queries";
import { AdminAudit } from "./routes/AdminAudit";
import { AdminModelDetail } from "./routes/AdminModelDetail";
import { AdminModels } from "./routes/AdminModels";
import { AdminRouting } from "./routes/AdminRouting";
import { AdminProviders } from "./routes/AdminProviders";
import { AdminSearch } from "./routes/AdminSearch";
import { AdminQuotaUsage } from "./routes/AdminQuotaUsage";
import { AdminQuotas } from "./routes/AdminQuotas";
import { AdminPrivacy } from "./routes/AdminPrivacy";
import { AdminPrivacyClassifiers } from "./routes/AdminPrivacyClassifiers";
import { AdminPrivacyDetectors } from "./routes/AdminPrivacyDetectors";
import { AdminPrivacyTest } from "./routes/AdminPrivacyTest";
import { AdminPolicies } from "./routes/AdminPolicies";
import { AdminPolicy } from "./routes/AdminPolicy";
import { AdminPipeline, MyPipeline } from "./components/PipelineEditor";
import { MyPrivacy, MyPrivacyTest } from "./routes/MyPrivacy";
import { Admin } from "./routes/Admin";
import { AdminReports } from "./routes/AdminReports";
import { AdminRequests } from "./routes/AdminRequests";
import { MyProviders } from "./routes/MyProviders";
import { MyRedactions } from "./routes/MyRedactions";
import { MyRequests } from "./routes/MyRequests";
import { AdminByok } from "./routes/AdminByok";
import { AdminGroups } from "./routes/AdminGroups";
import { AdminSettings } from "./routes/AdminSettings";
import { AdminUsers } from "./routes/AdminUsers";
import { Login } from "./routes/Login";
import { NotFound } from "./routes/NotFound";
import { Overview } from "./routes/Overview";
import { Reports } from "./routes/Reports";

/**
 * `/admin/redaction/rules/:id` and `/admin/privacy/hidden/rules/:id`: a redaction rule
 * is now a policy, and it kept its id, so an old link lands on the same thing.
 */
function RedirectRedactionRule() {
  const { ruleId } = useParams();
  return <Navigate to={`/admin/privacy/policies/${ruleId ?? "new"}`} replace />;
}

export function App() {
  const me = useMe();

  if (me.isPending) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center p-6">
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
      <div className="flex min-h-[60vh] items-center justify-center p-6">
        <Notice tone="danger" title="The console could not start">
          {me.error instanceof Error ? me.error.message : "The gateway did not respond."}
        </Notice>
      </div>
    );
  }

  const me_ = me.data;

  // A signed-in account whose groups withhold the console: the sign-in is
  // fine, the console is not for them. Absent means allowed (older gateways);
  // administrators always have it. The gateway refuses their `/api/me/*`
  // calls regardless; this page only says why instead of rendering 403s.
  if (me_.capabilities?.console === false && !me_.is_admin) {
    return <NoConsole chatUrl={me_.chat_url} />;
  }

  return (
    <Shell me={me_}>
      <Routes>
        <Route path="/" element={<Overview me={me_} />} />
        {/* A person's own consumption. Not admin-gated: everyone has usage,
            including administrators, whose own spend is not administration. */}
        <Route path="/reports" element={<Reports />} />
        <Route path="/requests" element={<MyRequests />} />
        {/* What redaction hid from providers, the rules in force and the user's
            own. Not admin-gated: it is their own requests. */}
        <Route path="/redactions" element={<MyRedactions />} />
        {/* The user's own privacy pipelines, their policies, and a test box locked to them. */}
        <Route path="/privacy" element={<MyPrivacy />} />
        <Route path="/privacy/pipelines/:id" element={<MyPipeline />} />
        <Route path="/privacy/test" element={<MyPrivacyTest />} />
        {/* Bring your own key. Not admin-gated and not capability-gated in the
            router: the page itself explains a missing capability, so a
            bookmarked link says why rather than reading as a broken deploy. */}
        <Route path="/me/providers" element={<MyProviders me={me_} part="providers" />} />
        <Route path="/me/models" element={<MyProviders me={me_} part="models" />} />
        <Route path="/me/models/:modelId" element={<MyProviders me={me_} part="model" />} />

        {/* Wrapped rather than conditionally registered: a non-admin who follows
            a bookmarked link should be told, not handed a blank 404 that reads
            as a broken deploy. The API refuses them regardless — this is
            courtesy, not enforcement. */}
        <Route
          path="/admin/search"
          element={
            <RequireAdmin me={me_}>
              <AdminSearch />
            </RequireAdmin>
          }
        />
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
          path="/admin/private-providers"
          element={
            <RequireAdmin me={me_}>
              <AdminByok />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/privacy"
          element={
            <RequireAdmin me={me_}>
              <AdminPrivacy />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/privacy/pipelines/:id"
          element={
            <RequireAdmin me={me_}>
              <AdminPipeline />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/privacy/policies"
          element={
            <RequireAdmin me={me_}>
              <AdminPolicies />
            </RequireAdmin>
          }
        />
        {/* One redaction policy; `new` shares the route, as it cannot collide with a uuid. */}
        <Route
          path="/admin/privacy/policies/:policyId"
          element={
            <RequireAdmin me={me_}>
              <AdminPolicy />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/privacy/test"
          element={
            <RequireAdmin me={me_}>
              <AdminPrivacyTest />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/privacy/classifiers"
          element={
            <RequireAdmin me={me_}>
              <AdminPrivacyClassifiers />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/privacy/detectors"
          element={
            <RequireAdmin me={me_}>
              <AdminPrivacyDetectors />
            </RequireAdmin>
          }
        />
        {/* Old links keep working (ADR 0113): redaction rules became policies under the
            same ids, "What gets hidden" became Redaction policies, and the routing rules
            became pipelines. */}
        <Route path="/admin/privacy/profiles" element={<Navigate to="/admin/privacy/detectors" replace />} />
        <Route path="/admin/privacy/service" element={<Navigate to="/admin/privacy/detectors" replace />} />
        <Route path="/admin/privacy/hidden" element={<Navigate to="/admin/privacy/policies" replace />} />
        <Route path="/admin/privacy/hidden/rules/:ruleId" element={<RedirectRedactionRule />} />
        <Route path="/admin/privacy/rules/:ruleId" element={<Navigate to="/admin/privacy" replace />} />
        <Route path="/admin/redaction" element={<Navigate to="/admin/privacy/policies" replace />} />
        <Route path="/admin/redaction/rules/:ruleId" element={<RedirectRedactionRule />} />
        <Route path="/admin/privacy/*" element={<Navigate to="/admin/privacy" replace />} />
        <Route
          path="/admin/routing"
          element={
            <RequireAdmin me={me_}>
              <AdminRouting />
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
          path="/admin/settings"
          element={
            <RequireAdmin me={me_}>
              <AdminSettings />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/groups"
          element={
            <RequireAdmin me={me_}>
              <AdminGroups />
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
          path="/admin/quota-usage"
          element={
            <RequireAdmin me={me_}>
              <AdminQuotaUsage />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/quota-usage/:userId"
          element={
            <RequireAdmin me={me_}>
              <AdminQuotaUsage />
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
          path="/admin/requests"
          element={
            <RequireAdmin me={me_}>
              <AdminRequests />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/audit"
          element={
            <RequireAdmin me={me_}>
              <AdminAudit />
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

        {/* Password reset is public by design: the person who needs it has
            no session (ADR 0049). Two entries, one file — the token decides
            which half of the journey this is. */}
        <Route path="*" element={<NotFound />} />
      </Routes>
    </Shell>
  );
}
