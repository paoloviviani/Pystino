/**
 * Wire types, derived from the gateway's OpenAPI document.
 *
 * `api-schema.ts` is generated (`pnpm --filter @llmp/console api:generate`,
 * from `apps/console/openapi.json`, which `scripts/export_openapi.py` writes
 * from the FastAPI app). The names below are the console's own and stay the
 * names call sites import; each is an alias of the gateway model, or of that
 * model with some fields narrowed (`Narrow`). Types with no gateway model
 * (closed sets, a few shapes the console builds itself) stay hand-written.
 * Change a model in the gateway and regenerate; a field the console relied on
 * that moved or changed type stops compiling here.
 *
 * **Money is a string everywhere.** The gateway stores `Numeric(24,12)` and
 * serialises decimals as strings precisely so no float ever touches an amount.
 * Typing these as `number` would undo that at the last hop.
 */

import type { components } from "./api-schema";

type Schemas = components["schemas"];

/**
 * A generated type with some fields narrowed by the console. The gateway
 * declares many closed sets as `str` (a model's `kind`, a limit's `scope`);
 * the console knows the values, so it narrows them here. `N` must be
 * assignable to the generated field it replaces, so a field the gateway drops
 * or retypes stops compiling instead of silently surviving as a local lie.
 */
type Narrow<T, N extends { [K in keyof N]: K extends keyof T ? T[K] : never }> = Omit<
  T,
  keyof N
> &
  N;

export type GroupSummary = Schemas["GroupSummary"];

export type Me = Schemas["MeResponse"];

/** The two ADR 0096 capabilities, as one person effectively holds them. */
export type Capabilities = Schemas["CapabilityFlags"];

/** A self-service default billing group choice, from the caller's own groups. */
export type SetDefaultBillingGroupInput = Schemas["SetDefaultBillingGroupRequest"];

export type PeriodInfo = Schemas["PeriodResponse"];

export type UsageReportRow = Schemas["UsageReportRow"];

export type UsageReport = Schemas["UsageReport"];

export type ApiKey = Schemas["ApiKeyResponse"];

/**
 * What minting returns, exactly once.
 *
 * `secret` exists on this response and nowhere else — the gateway stores only a
 * hash, so it cannot be re-read, re-sent or recovered. A separate type rather
 * than an optional field on `ApiKey`, so that anywhere holding a plain `ApiKey`
 * provably has no secret in it.
 */
export type MintedApiKey = Schemas["ApiKeyCreatedResponse"];

// -- administration ----------------------------------------------------------
//
// Decimals are strings here for the same reason they are above: the gateway
// serialises `Numeric` as a string so no float ever touches money, and typing
// them as `number` would undo that at the last hop.

/** What a model produces, and therefore which route may use it. */
export type ModelKind = "chat" | "embedding" | "image" | "ocr" | "search" | "decision";

export type Price = Schemas["PriceResponse"];

/** One row of a search backend's per-request price history. */
export type SearchPrice = Schemas["SearchPriceResponse"];

export type AdminModel = Narrow<
  Schemas["ModelAdminResponse"],
  {
    kind: ModelKind;
  }
>;

/** Which alias, and under what name — enough to link to it and to say whether
 *  it is switched on. Not the whole alias response: a model page showing twenty
 *  pointing aliases needs their names, not each one's visibility summary. */
export type AliasPointer = Schemas["AliasPointer"];

/** The model an alias serves. No provider and no upstream name: choosing a
 *  target is choosing between models, and nothing on this screen is about the
 *  counterparty behind one. */
export type AliasTarget = Narrow<
  Schemas["AliasTargetResponse"],
  {
    kind: ModelKind;
    unavailable_reason: AliasUnavailableReason | null;
  }
>;

/** How a request picks among an alias's targets (issue P1). A string, not a
 *  closed union: a rule added on the gateway later arrives here before the
 *  console knows its name, and must render rather than break. */
export type AliasRouteRule = "first_available" | "weighted" | "cheapest";

/** Why an alias cannot serve a request. `null` on an available one.
 *
 *  `target_is_alias` is not reachable through the console — the save path
 *  refuses to write it — and exists so a chain forced into the table directly
 *  is shown as a fault rather than as an available alias. */
export type AliasUnavailableReason =
  | "target_deleted"
  | "target_inactive"
  | "target_provider_inactive"
  | "target_is_alias";

/** One alias, as the Routing tab shows it (ADR 0101). */
export type AdminAlias = Narrow<
  Schemas["AliasResponse"],
  {
    kind: ModelKind;
    target: AliasTarget | null;
    targets: AliasTarget[];
    status: "available" | "unavailable";
    unavailable_reason: AliasUnavailableReason | null;
  }
>;

/** A group name an identity provider reported that no group here carries
    yet (GATEWAY_OIDC__GROUP_IMPORT=manual records these instead of creating
    them). */
export type SeenGroup = Schemas["SeenGroupResponse"];

export type SeenGroupImport = Schemas["SeenGroupImportResponse"];

export type AdminGroup = Schemas["GroupAdminResponse"];

export type AdminUser = Narrow<
  Schemas["UserAdminResponse"],
  {
    membership_source: "manual" | "oidc" | null;
  }
>;

/** `POST /admin/users`, either account backend (ADR 0095). */
export type UserCreateInput = Schemas["UserCreateRequest"];

/** `POST /admin/users`, bundled Authelia only (ADR 0093 §8.1/§8.2). */
export interface BundledUserCreateInput {
  login: string;
  display_name?: string;
  email: string;
  groups?: string[];
}

/** Includes the one-time password, shown once (§8.1). */
export type BundledUserCreated = Narrow<
  Schemas["BundledUserCreatedResponse"],
  {
    membership_source: "manual" | "oidc" | null;
  }
>;

/** An invitation for a provider that creates no logins (ADR 0095). */
export type InvitedUser = Narrow<
  Schemas["InvitedUserResponse"],
  {
    user: AdminUser;
  }
>;

/** A regenerated invitation text (ADR 0095). */
export type InviteText = Schemas["InviteTextResponse"];

export type UserStatus =
  "awaiting_approval" | "invited" | "active" | "disabled";

export type UserStatusCounts = Schemas["UserStatusCounts"];

/** One `(issuer, subject)` (ADR 0093). */
export type IdentityRef = Schemas["IdentityRef"];

/** `GET /admin/users/{source}/merge-preview?into=` (ADR 0093 §7.1). */
export type MergePreview = Schemas["MergePreviewResponse"];

/** `POST /admin/users/{source}/merge` (ADR 0093 §7.1). */
export type MergeInput = Schemas["MergeRequest"];

export type MergeResult = Schemas["MergeResponse"];

/** One resource of this person that someone else can see, named, with who
 * can see it (ADR 0093 §9.2) — passed through from the chat's own erasure
 * preview verbatim. */
export type ChatSharedResource = Narrow<
  Schemas["ChatSharedResource"],
  {
    kind: "shared_conversation" | "project" | "knowledge_base" | "assistant";
  }
>;

/** `GET /admin/users/{id}/delete-preview` (ADR 0093 §9.2). */
export type DeletePreview = Narrow<
  Schemas["DeletePreviewResponse"],
  {
    shared: ChatSharedResource[];
  }
>;

/** `DELETE /admin/users/{id}` (ADR 0093 §9.2, §9.3). */
export type DeleteUserResult = Schemas["DeleteUserResponse"];

/** The audit trail `record_event` writes to (ADR 0093 §3.1). */
export type IdentityEvent = Schemas["IdentityEventResponse"];

export type LimitRule = Narrow<
  Schemas["LimitRuleResponse"],
  {
    scope: "global" | "group" | "user" | "api_key";
    metric: "requests" | "tokens" | "cost";
    applies_to: "aggregate" | "each_member" | "each_user";
  }
>;

/** One rule's current window, counter against ledger (`GET /api/admin/quota/health`). */
export type QuotaWindowHealth = Narrow<
  Schemas["QuotaWindowHealth"],
  {
    scope: "global" | "group" | "user" | "api_key";
    metric: "requests" | "tokens" | "cost" | "own_search_requests";
  }
>;

export type QuotaHealth = Narrow<
  Schemas["QuotaHealthResponse"],
  {
    windows: QuotaWindowHealth[];
  }
>;

export type QuotaReconcileResult = Schemas["QuotaReconcileResponse"];

export type QuotaReset = Schemas["QuotaResetResponse"];

/** One rule as it applies to one user (`GET /api/admin/quota-usage/{id}`). */
export type UserQuotaEntry = Narrow<
  Schemas["UserQuotaEntry"],
  {
    kind: "own" | "each_user" | "each_member" | "shared";
    scope: "global" | "group" | "user" | "api_key";
    metric: "requests" | "tokens" | "cost" | "own_search_requests";
  }
>;

/** A row of the Quota usage page: a user and their most constrained quota. */
export type UserQuotaSummary = Narrow<
  Schemas["UserQuotaSummary"],
  { most_constrained: UserQuotaEntry | null }
>;

export type UserQuotaUsage = Narrow<Schemas["UserQuotaUsage"], { entries: UserQuotaEntry[] }>;

export type DiscoveredModel = Narrow<
  Schemas["DiscoveredModel"],
  {
    kind: ModelKind;
    price_source: "provider" | "community" | "hint" | null;
  }
>;

export type CatalogueDriftRow = Schemas["CatalogueDriftRow"];

export type CatalogueDiscovery = Narrow<
  Schemas["CatalogueDiscoveryResponse"],
  {
    available: DiscoveredModel[];
  }
>;

export type CatalogueTags = Schemas["CatalogueTagsResponse"];

/**
 * A price typed by an administrator on an unpriced discovery row, sent with
 * the import. The same shape the model page's price form posts — money is the
 * string as typed, never a number — with the rates a kind is metered on
 * required by the gateway and the rest omitted.
 */
export type ManualImportPrice = Narrow<
  Schemas["ManualImportPrice"],
  {
    input_per_mtok?: string;
    output_per_mtok?: string;
    per_page?: string;
    cache_read_per_mtok?: string;
    cache_write_per_mtok?: string;
    currency?: string;
  }
>;

/** One row of an import request. */
export type ModelImportItem = Narrow<
  Schemas["ModelImportItem"],
  {
    manual_price?: ManualImportPrice;
  }
>;

export type ModelImportResult = Narrow<
  Schemas["ModelImportResult"],
  {
    price_source: "provider" | "community" | "hint" | "manual" | null;
  }
>;

export type ModelImportResponse = Narrow<
  Schemas["ModelImportResponse"],
  {
    results: ModelImportResult[];
  }
>;

/** What the detection service says about itself, asked at read time. */
export type RedactionServiceHealth = Schemas["RedactionServiceHealth"];

export type RedactionActivity = Schemas["RedactionActivity"];

/**
 * One installed engine, as something an admin can choose.
 *
 * Read from the API rather than hardcoded, so installing an engine through the
 * `llmp.redactors` entry point makes it selectable without a console release —
 * the same argument the provider-type list makes (ADR 0026, ADR 0033).
 */
export type RedactionEngineOption = Schemas["RedactionEngineOption"];

/** Who last changed the engine, when, and why. Null when the environment decides. */
export type RedactionConfigChange = Schemas["RedactionConfigChange"];

/** The redaction layer as the gateway is actually running it (ADR 0012, ADR 0033). */
/**
 * What happens to one kind of detected entity (ADR 0037).
 *
 * Two questions, not one: what the model sees, and what the reader gets back.
 * Ordered weakest to strongest, which is the order the screen lists them.
 */
export type EntityMode =
  "off" | "anonymise_restore" | "anonymise" | "redact" | "block";

/**
 * How a placeholder is written. `same_length` is `#TOKEN#` with the length of
 * the value it replaces, for forms and fixed-width fields; it tells the
 * provider how long the value is, so the tagged form is the default.
 */
export type PlaceholderStyle = "tagged" | "same_length";

export type EntityPolicy = Narrow<
  Schemas["EntityPolicy"],
  {
    mode: EntityMode;
    threshold: number | null;
    placeholder?: PlaceholderStyle;
  }
>;

/**
 * A regex an operator wrote, treated as one more entity type.
 *
 * Compiled with RE2 in the gateway, so a pattern is validated there and not
 * here: RE2 refuses constructs JavaScript accepts — backreferences, lookaround —
 * and a browser-side check would pass a pattern the API then rejects.
 */
export type CustomPattern = Narrow<
  Schemas["CustomPattern"],
  {
    mode: EntityMode;
    placeholder?: PlaceholderStyle;
  }
>;

export type RedactionPolicy = Narrow<
  Schemas["RedactionPolicy"],
  {
    default_mode: EntityMode;
    entities: Record<string, EntityPolicy>;
    patterns: CustomPattern[];
    allow_list: string[];
  }
>;

export type RedactionStatus = Narrow<
  Schemas["RedactionStatusResponse"],
  {
    source: "console" | "environment";
    policy: RedactionPolicy;
    policy_source: "console" | "environment";
  }
>;

/** The five subjects a redaction rule can name (ADR 0038). */
export type RedactionScope =
  "all" | "provider" | "model" | "group" | "user" | "api_key";

export type RedactionRule = Narrow<
  Schemas["RedactionRuleResponse"],
  {
    policy: RedactionPolicy;
  }
>;

/** One thing the detector found in a sample, and what the policy said about it. */
export type RedactionPreviewSpan = Narrow<
  Schemas["RedactionPreviewSpan"],
  {
    mode: EntityMode;
  }
>;

export type RedactionPreview = Narrow<
  Schemas["RedactionPreviewResponse"],
  {
    policy: RedactionPolicy;
    spans: RedactionPreviewSpan[];
  }
>;

/**
 * One quota rule that constrains the person signed in.
 *
 * Narrower than {@link LimitRule} on purpose. No `scope_id`, because the only
 * scopes here are global, this user, or a group they are in — an id would name
 * either themselves or something they already know. No `is_active`, because an
 * inactive rule is not returned at all.
 */
export type MyLimit = Schemas["MyLimitResponse"];

/**
 * An installed provider type.
 *
 * The plugin *is* the type as far as an operator is concerned: it decides how
 * the counterparty is talked to and what may be believed about what it charged.
 * Read from the API rather than hardcoded, so installing a plugin makes it
 * selectable without a console release (ADR 0032).
 */
export type ProviderPlugin = Narrow<
  Schemas["ProviderPluginResponse"],
  {
    kind: "provider" | "router" | "search" | "internal";
    billing_modes: ("own_prices" | "provider_reported")[];
  }
>;

/** Where one catalogue field's current value came from. */
export type CatalogueOrigin = Schemas["CatalogueOriginResponse"];

/**
 * One entry of the provider catalogue (YAML, read at startup). `null` in a
 * retention field is *unknown*, never "no".
 */
export type CatalogueEntry = Schemas["CatalogueEntryResponse"];

export type AdminProvider = Narrow<
  Schemas["ProviderResponse"],
  {
    kind: "provider" | "router" | "search" | "internal";
    billing_mode: "own_prices" | "provider_reported";
  }
>;

export type ProviderTestResult = Schemas["ProviderTestResponse"];

/**
 * What deleting a search backend took with it (the gateway's cascade for
 * `kind=search` providers, ADR 0071). The cleared groups are names, not ids:
 * they are for a notice, and a notice speaks the group's name.
 */
export type SearchBackendDeleteResult = Schemas["SearchBackendDeleteResponse"];

/**
 * The identity policy in force (ADR 0048, join modes ADR 0095), as
 * `GET /api/admin/oidc/policy` reports it: the effective values, where each
 * came from, and the newest decision behind them.
 */
export type JoinMode = "open" | "approval" | "invite" | "group";

/** ADR 0096 (amended): the default flag new groups are created with. */
export type CapabilityMode = "on" | "off";

export type OidcPolicy = Narrow<
  Schemas["OidcPolicyResponse"],
  {
    join_mode: JoinMode;
    group_fallback: "refuse" | "approval";
    code_agents_mode: CapabilityMode;
    api_keys_mode: CapabilityMode;
    local_connectors_mode: CapabilityMode;
    source: "console" | "environment";
  }
>;

/**
 * A policy change. Unset fields carry the newest stored row's answers
 * forward (ADR 0095) — an explicitly sent null clears back to the
 * environment — so a change to one knob never resets the others.
 */
export type OidcPolicyInput = Schemas["OidcPolicyUpdateRequest"];

/** What saving a group gate would do (ADR 0095), and what a bulk capability
 * flip would do (ADR 0096, amended). */
export type OidcPolicyPreview = Schemas["OidcPolicyPreviewResponse"];

/** Creating a group (ADR 0050). Manual is the point: it is the one kind whose
 * membership the console may edit. */
export type GroupCreateInput = Schemas["GroupCreateRequest"];

/** Flipping a group's capability flags (ADR 0096) — `PATCH /api/admin/groups/{id}`. */
export type GroupUpdateInput = Schemas["GroupUpdateRequest"];

/** The bulk flip (ADR 0096, amended) — `POST /api/admin/groups/capabilities`. */
export type GroupCapabilityBulkInput = Schemas["GroupCapabilityBulkRequest"];

// -- Settings (ADR 0051) ------------------------------------------------------

export type GroupSync = "every_login" | "first_login" | "never";

/** One configured identity provider. The client secret is write-only: a
 * response never carries it, only the fact that one is stored. */
export type IdentityProvider = Narrow<
  Schemas["IdentityProviderResponse"],
  {
    source: "console" | "environment";
    kind: IdentityKind;
    sync_adapter: SyncAdapter;
    // The gateway sends `dict[str, Any]`; the console knows the keys.
    capabilities: IdentityCapabilities;
  }
>;

export type IdentityKind =
  | "generic"
  | "authelia"
  | "entra"
  | "okta"
  | "authentik"
  | "google";
export type GroupSource = "claim" | "directory" | "none";
export type AdminSource = "console" | "claim";
export type SyncAdapter = "none" | "authelia_file" | "scim";

// A type alias, not an interface: only an alias is assignable to the gateway's
// `dict[str, Any]` (an interface has no implicit index signature).
export type IdentityCapabilities = {
  claims_groups: boolean;
  pull_adapters: SyncAdapter[];
  scim_push: boolean;
  subject_before_login: boolean;
  deprovision: boolean;
  /** The provider kind creates logins itself (ADR 0095): the console shows
   * the Add-user dialog. Without it, the Invite dialog. */
  creates_logins: boolean;
  adapters: SyncAdapter[];
};

export interface SyncRun {
  id: string;
  trigger: "schedule" | "manual" | "push";
  dry_run: boolean;
  status: "ok" | "failed" | "needs_confirmation";
  seen: number;
  created: number;
  linked: number;
  updated: number;
  deactivated: number;
  reactivated: number;
  changes: { change: string; who: string; [detail: string]: unknown }[];
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface DirectoryPerson {
  id: string;
  external_id: string;
  username: string | null;
  email: string | null;
  display_name: string | null;
  groups: string[];
  active: boolean;
  present: boolean;
  preassigned_groups: string[];
  user_id: string | null;
}

/** The SMTP configuration in force — the row's, or the environment's. */
export type EmailSettings = Narrow<
  Schemas["EmailSettingsResponse"],
  {
    source: "console" | "environment";
  }
>;

export type EmailTestResult = Schemas["EmailTestResponse"];

/** The sign-in page's menu, from GET /auth/methods (ADR 0051). */
export type AuthMethods = Schemas["AuthMethods"];

// -- Quota notifications (ADR 0052) -------------------------------------------

/** The caller's thresholds for one rule: percentages, arbitrary, user-owned. */
export type MyNotificationThresholdsInput = Schemas["MyNotificationThresholdsRequest"];
