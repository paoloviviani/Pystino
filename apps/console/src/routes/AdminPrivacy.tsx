import { Badge, Button, Card, Dialog, EmptyState, Notice, Spinner } from "@llmp/ui";
import { useState } from "react";
import { useNavigate } from "react-router";

import { PageHeader } from "../components/PageHeader";
import { MUTED, PAGE } from "../lib/layout";
import {
  KINDS,
  issueBadges,
  kindLabel,
  usePrivacyProfiles,
  usePrivacyRules,
  useReorderPrivacyRules,
  useUpdatePrivacyRule,
  ruleSentence,
} from "../lib/privacy";
import type { PrivacyKind, PrivacyRule } from "../lib/types";
import { m } from "../paraglide/messages.js";

/**
 * Privacy routing: the administrator's rules, grouped by what is being judged.
 *
 * The page is the list. Each row is one rule in a sentence, with a badge for
 * anything worth a second look; tapping a badge says why. The model, the
 * first-match order and the worked example live behind "How rules work", so the
 * list itself stays short enough to read aloud.
 */

const SOFT_CAP = 20;

type BadgeKey = "off" | "observe" | "never" | "partly" | "check";

function RuleRow({
  rule,
  index,
  count,
  names,
  onMove,
  onToggle,
  onEdit,
}: {
  rule: PrivacyRule;
  index: number;
  count: number;
  names: ReadonlyMap<string, string>;
  onMove: (delta: -1 | 1) => void;
  onToggle: (enabled: boolean) => void;
  onEdit: () => void;
}) {
  const [shown, setShown] = useState<BadgeKey | null>(null);
  const flags = issueBadges(rule);
  const badges: { key: BadgeKey; tone: "neutral" | "accent" | "warn"; label: string }[] = [];
  if (!rule.enabled) badges.push({ key: "off", tone: "neutral", label: m["privacy.badge.off"]() });
  if (rule.observe_only)
    badges.push({ key: "observe", tone: "accent", label: m["privacy.badge.observe"]() });
  if (flags.never) badges.push({ key: "never", tone: "warn", label: m["privacy.badge.never"]() });
  if (flags.partly)
    badges.push({ key: "partly", tone: "warn", label: m["privacy.badge.partly"]() });
  if (flags.check) badges.push({ key: "check", tone: "warn", label: m["privacy.badge.check"]() });

  const detail = (key: BadgeKey): string[] => {
    switch (key) {
      case "off":
        return [m["privacy.badge.off_detail"]()];
      case "observe":
        return [m["privacy.badge.observe_detail"]()];
      case "never":
        return rule.issues.filter((i) => i.code === "never_fires").map((i) => i.message);
      case "partly":
        return rule.issues.filter((i) => i.code === "partly_shadowed").map((i) => i.message);
      default:
        return rule.issues
          .filter((i) => i.code !== "never_fires" && i.code !== "partly_shadowed")
          .map((i) => i.message);
    }
  };

  return (
    <li className="flex flex-col gap-2 border-b border-line-quiet py-3 last:border-b-0">
      <div className="flex items-start gap-3">
        <span className={`w-6 shrink-0 pt-0.5 text-right text-small ${MUTED}`} aria-hidden="true">
          {index + 1}
        </span>
        <div className="min-w-0 flex-1">
          <p className="m-0 font-medium">
            {/* i18n: server text, the name an administrator gave the rule. */}
            {rule.name}
          </p>
          <p className={`m-0 text-dense ${MUTED}`}>{ruleSentence(rule, names)}</p>
          {badges.length > 0 && (
            <div className="mt-1 flex flex-wrap gap-1">
              {badges.map((badge) => (
                <button
                  key={badge.key}
                  type="button"
                  aria-expanded={shown === badge.key}
                  className="cursor-pointer rounded-full border-0 bg-transparent p-0"
                  onClick={() => setShown(shown === badge.key ? null : badge.key)}
                >
                  <Badge tone={badge.tone}>{badge.label}</Badge>
                </button>
              ))}
            </div>
          )}
        </div>
        <div className="flex shrink-0 flex-wrap items-center justify-end gap-1">
          <Button
            type="button"
            aria-label={m["privacy.row.up"]({ name: rule.name })}
            disabled={index === 0}
            onClick={() => onMove(-1)}
          >
            ↑
          </Button>
          <Button
            type="button"
            aria-label={m["privacy.row.down"]({ name: rule.name })}
            disabled={index === count - 1}
            onClick={() => onMove(1)}
          >
            ↓
          </Button>
          <label className="flex items-center gap-1 px-1 text-small">
            <input
              type="checkbox"
              checked={rule.enabled}
              aria-label={m["privacy.row.enabled"]({ name: rule.name })}
              onChange={(event) => onToggle(event.target.checked)}
            />
            {m["privacy.row.on"]()}
          </label>
          <Button type="button" onClick={onEdit}>
            {m["privacy.row.edit"]()}
          </Button>
        </div>
      </div>
      {shown && (
        <ul className="m-0 ml-9 list-disc rounded-lg bg-sunken py-2 pr-3 pl-6 text-small">
          {detail(shown).map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}
    </li>
  );
}

function KindCard({
  kind,
  rules,
  names,
}: {
  kind: PrivacyKind;
  rules: PrivacyRule[];
  names: ReadonlyMap<string, string>;
}) {
  const navigate = useNavigate();
  const reorder = useReorderPrivacyRules();
  const update = useUpdatePrivacyRule();

  const move = (index: number, delta: -1 | 1) => {
    const ids = rules.map((r) => r.id);
    const [moved] = ids.splice(index, 1);
    ids.splice(index + delta, 0, moved!);
    reorder.mutate({ kind, order: ids });
  };

  return (
    <Card
      title={kindLabel(kind)}
      description={kind === "read" ? m["privacy.kind.read_hint"]() : m["privacy.kind.embed_hint"]()}
      actions={
        <Button type="button" onClick={() => navigate(`/admin/privacy/rules/new?kind=${kind}`)}>
          {m["privacy.add_rule"]()}
        </Button>
      }
    >
      {rules.length > SOFT_CAP && (
        <Notice tone="warn">{m["privacy.soft_cap"]({ count: SOFT_CAP })}</Notice>
      )}
      {rules.length === 0 ? (
        <p className={`m-0 text-body ${MUTED}`}>{m["privacy.kind.empty"]()}</p>
      ) : (
        <ol className="m-0 list-none p-0">
          {rules.map((rule, index) => (
            <RuleRow
              key={rule.id}
              rule={rule}
              index={index}
              count={rules.length}
              names={names}
              onMove={(delta) => move(index, delta)}
              onToggle={(enabled) => update.mutate({ id: rule.id, body: { enabled } })}
              onEdit={() => navigate(`/admin/privacy/rules/${rule.id}`)}
            />
          ))}
        </ol>
      )}
      <p className={`mt-3 mb-0 text-small ${MUTED}`}>{m["privacy.everything_else"]()}</p>
    </Card>
  );
}

function HowItWorks({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Dialog
      open={open}
      title={m["privacy.how.title"]()}
      onClose={onClose}
      footer={
        <Button type="button" variant="primary" onClick={onClose}>
          {m["privacy.how.close"]()}
        </Button>
      }
    >
      <div className="flex flex-col gap-4 text-body">
        <p className="m-0">{m["privacy.how.model"]()}</p>
        <ul className="m-0 flex list-disc flex-col gap-1 pl-5">
          <li>{m["privacy.how.first_match"]()}</li>
          <li>{m["privacy.how.observe"]()}</li>
          <li>{m["privacy.how.stateless"]()}</li>
          <li>{m["privacy.how.fail_closed"]()}</li>
          <li>{m["privacy.how.names_are_convenience"]()}</li>
        </ul>
        <div className="grid gap-3 [grid-template-columns:repeat(auto-fit,minmax(14rem,1fr))]">
          {KINDS.map((kind) => (
            <div key={kind} className="rounded-lg border border-line p-3">
              <p className="m-0 font-medium">{kindLabel(kind)}</p>
              <p className={`m-0 text-small ${MUTED}`}>
                {kind === "read" ? m["privacy.how.read.endpoint"]() : m["privacy.how.embed.endpoint"]()}
              </p>
              <p className="mt-2 mb-0 text-small">
                {kind === "read" ? m["privacy.how.read.sees"]() : m["privacy.how.embed.sees"]()}
              </p>
              <p className="mt-1 mb-0 text-small">
                {kind === "read" ? m["privacy.how.read.can"]() : m["privacy.how.embed.can"]()}
              </p>
              <p className={`mt-1 mb-0 text-small ${MUTED}`}>
                {kind === "read" ? m["privacy.how.read.example"]() : m["privacy.how.embed.example"]()}
              </p>
            </div>
          ))}
        </div>
      </div>
    </Dialog>
  );
}

export function AdminPrivacy() {
  const navigate = useNavigate();
  const rules = usePrivacyRules();
  const profiles = usePrivacyProfiles();
  const [how, setHow] = useState(false);
  const names = new Map((profiles.data?.items ?? []).map((p) => [p.id, p.name]));

  return (
    <div className={PAGE}>
      <PageHeader
        title={m["privacy.title"]()}
        subtitle={m["privacy.subtitle"]()}
        actions={
          <>
            <Button type="button" onClick={() => setHow(true)}>
              {m["privacy.how_link"]()}
            </Button>
            <Button type="button" onClick={() => navigate("/admin/privacy/test")}>
              {m["privacy.test_link"]()}
            </Button>
            <Button type="button" onClick={() => navigate("/admin/privacy/profiles")}>
              {m["privacy.profiles_link"]()}
            </Button>
          </>
        }
      />
      <HowItWorks open={how} onClose={() => setHow(false)} />

      {rules.isPending && <Spinner label={m["privacy.loading"]()} />}
      {rules.error ? (
        <Notice tone="danger" title={m["privacy.error.load"]()}>
          {rules.error instanceof Error ? rules.error.message : m["privacy.error.unknown"]()}
        </Notice>
      ) : null}
      {rules.data &&
        (rules.data.items.length === 0 ? (
          <EmptyState
            title={m["privacy.empty.title"]()}
            detail={m["privacy.empty.detail"]()}
            action={
              <Button
                type="button"
                variant="primary"
                onClick={() => navigate("/admin/privacy/rules/new")}
              >
                {m["privacy.add_rule"]()}
              </Button>
            }
          />
        ) : null)}
      {rules.data &&
        KINDS.map((kind) => (
          <KindCard
            key={kind}
            kind={kind}
            rules={rules.data.items.filter((r) => r.kind === kind)}
            names={names}
          />
        ))}
      {rules.data && (
        <p className={`m-0 text-small ${MUTED}`}>{m["privacy.chat_later"]()}</p>
      )}
    </div>
  );
}
