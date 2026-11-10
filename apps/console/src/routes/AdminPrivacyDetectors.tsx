import { Badge, Button, Input, Card, Notice } from "@llmp/ui";
import { useState } from "react";

import { PrivacyHeader } from "../components/PrivacyHeader";
import { useRedactionStatus } from "../lib/admin";
import { EngineDetails, Warnings } from "./AdminRedaction";
import { MUTED, PAGE } from "../lib/layout";
import { usePrivacyEngines, usePrivacySettings, useSavePrivacySettings } from "../lib/privacy";
import { useSetRedactionEngine } from "../lib/admin";
import { entityLabel } from "../lib/entities";
import { useOptionalToast } from "../lib/toast";
import type { PrivacyEngine, PrivacyEngines, RedactionStatus } from "../lib/types";
import { m } from "../paraglide/messages.js";

/**
 * Detectors: exactly what the redaction service reports, one card per detector
 * (the patterns, spaCy, GLiNER), each with an On/Off switch, then what users may
 * add to their own policies and, collapsed, the engine's activity and
 * configuration. Whether privacy as a whole is on is the switch on the Pipelines
 * tab. Which NER engine reads a policy is the policy's own choice, among those
 * switched on here.
 */

/**
 * What users may add to their own policies: the entity types they may name
 * (patterns are always theirs). Blank means every type the detector offers.
 */
function UserTypes() {
  const settings = usePrivacySettings();
  const save = useSavePrivacySettings();
  const [text, setText] = useState<string | null>(null);
  const current = settings.data?.user_entity_types ?? null;
  const shown = text ?? (current ? current.join(", ") : "");
  return (
    <Card title={m["privacy.user_types.title"]()} description={m["privacy.user_types.hint"]()}>
      <div className="flex flex-col gap-3">
        <Input
          label={m["privacy.user_types.label"]()}
          hint={m["privacy.user_types.field_hint"]()}
          value={shown}
          onChange={(event) => setText(event.target.value)}
        />
        {save.error && (
          <Notice tone="danger">
            {/* i18n: server text, the API's own message. */}
            {save.error instanceof Error ? save.error.message : m["privacy.error.unknown"]()}
          </Notice>
        )}
        <div>
          <Button
            variant="primary"
            busy={save.isPending}
            onClick={() => {
              const types = shown
                .split(",")
                .map((t) => t.trim())
                .filter(Boolean);
              save.mutate(
                { user_entity_types: types.length ? types : null },
                { onSuccess: () => setText(null) },
              );
            }}
          >
            {m["privacy.user_types.save"]()}
          </Button>
        </div>
      </div>
    </Card>
  );
}


export const DOCS_URL = "https://paoloviviani.github.io/Pystino/redaction/#deploying-less-of-it";

type Which = "patterns" | "spacy" | "gliner";

/** The three switches as the status reports them, and what one change makes of them. */
function switchesOf(status: RedactionStatus) {
  return {
    patterns: status.patterns_enabled,
    spacy: status.spacy_enabled,
    gliner: status.gliner_enabled,
  };
}

function DetectorCard({
  which,
  title,
  hint,
  enabled,
  installed,
  reachable,
  usedBy,
  pending,
  onToggle,
  children,
}: {
  which: Which;
  title: string;
  hint: string;
  enabled: boolean;
  installed: boolean;
  /** False when the redaction service did not answer: nothing can be said to run. */
  reachable: boolean;
  usedBy: string[];
  pending: boolean;
  onToggle: (next: boolean) => void;
  children?: React.ReactNode;
}) {
  // A detector that cannot run never shows On, whatever is stored: the stored switch is left
  // alone and shows again once the engine is installed and the service answers.
  const runnable = installed && reachable;
  const shown = enabled && runnable;
  // The reason a disabled switch is disabled: the badge and install hint, or the unreachable notice.
  const reason = !reachable
    ? "detectors-unreachable"
    : !installed
      ? `detector-${which}-badge detector-${which}-how`
      : undefined;
  return (
    <Card>
      <div className="flex flex-col gap-3" data-testid={`detector-${which}`}>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h3 className="m-0 flex flex-wrap items-center gap-2 text-body font-semibold">
              {title}
              {reachable && !installed && (
                <span id={`detector-${which}-badge`}>
                  <Badge tone="warn">{m["detectors.not_installed"]()}</Badge>
                </span>
              )}
            </h3>
            <p className={`m-0 text-small ${MUTED}`}>{hint}</p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <span aria-hidden className={`text-body ${MUTED}`}>
              {shown ? m["detectors.on"]() : m["detectors.off"]()}
            </span>
            <button
              type="button"
              role="switch"
              aria-checked={shown}
              aria-label={m["detectors.switch"]({ name: title })}
              aria-describedby={reason}
              disabled={pending || !runnable}
              onClick={() => onToggle(!enabled)}
              data-checked={shown}
              className="relative h-9 w-16 shrink-0 cursor-pointer rounded-full border border-line-strong bg-sunken transition-colors data-[checked=true]:border-accent data-[checked=true]:bg-accent disabled:cursor-not-allowed disabled:opacity-60 focus-visible:outline-none focus-visible:shadow-focus"
            >
              <span
                aria-hidden
                className={`absolute top-1 left-1 size-7 rounded-full border border-line-strong bg-surface transition-transform ${shown ? "translate-x-7" : "translate-x-0"}`}
              />
            </button>
          </div>
        </div>
        {runnable && (
          <p className={`m-0 text-small ${MUTED}`}>
            {usedBy.length > 0
              ? `${m["detectors.used_by"]({ count: usedBy.length })}: ${usedBy.join(", ")}`
              : m["detectors.unused"]()}
          </p>
        )}
        {children}
      </div>
    </Card>
  );
}

function NerDetails({ engine }: { engine: PrivacyEngine }) {
  return (
    <div className={`flex flex-col gap-1 text-small ${MUTED}`}>
      {engine.ms_per_token !== null && (
        <span>{m["detectors.speed"]({ ms: String(engine.ms_per_token) })}</span>
      )}
      {engine.languages.length > 0 && (
        <span>{m["detectors.languages"]({ languages: engine.languages.join(", ") })}</span>
      )}
      <span>
        {m["detectors.entities"]({
          types: engine.entities.map((type) => entityLabel(type)).join(", "),
        })}
      </span>
      {engine.custom_labels && <span>{m["detectors.custom"]()}</span>}
    </div>
  );
}

function NotInstalled({ how, id }: { how: string; id: string }) {
  return (
    <p id={id} className={`m-0 text-small ${MUTED}`}>
      {how}{" "}
      <a href={DOCS_URL} target="_blank" rel="noreferrer" className="underline">
        {m["detectors.docs"]()}
      </a>
    </p>
  );
}

/** The cards, built from the capabilities the service reports and the switches in force. */
export function DetectorCards({
  engines,
  status,
}: {
  engines: PrivacyEngines;
  status: RedactionStatus;
}) {
  const setEngine = useSetRedactionEngine();
  const toast = useOptionalToast();
  const spacy = engines.engines.find((engine) => engine.name === "spacy");
  const gliner = engines.engines.find((engine) => engine.name === "gliner");
  const now = switchesOf(status);

  const toggle = (which: Which, next: boolean) => {
    const after = { ...now, [which]: next };
    setEngine.mutate(
      {
        engine: status.engine,
        // This change's own reason: never the previous change's, which would put words
        // in the audit log that nobody wrote for it.
        reason: m["detectors.reason"]({
          name: which === "patterns" ? m["detectors.patterns.title"]() : which === "spacy" ? "spaCy" : "GLiNER",
          state: next ? m["detectors.on"]() : m["detectors.off"](),
        }),
        presidio_pattern_matching: after.patterns,
        spacy_enabled: after.spacy,
        gliner_enabled: after.gliner,
      },
      {
        onSuccess: () => toast?.add({ title: m["detectors.saved"](), type: "success" }),
        onError: (error) =>
          toast?.add({
            title: m["detectors.failed"](),
            // i18n: server text, the API's own error message.
            description: error instanceof Error ? error.message : m["admin_redaction.unknown_error"](),
            type: "error",
          }),
      },
    );
  };

  return (
    <div className="flex flex-col gap-4">
      {!engines.reachable && (
        <div id="detectors-unreachable">
          <Notice tone="warn" title={m["detectors.unreachable"]()}>
            {/* i18n: server text, the redaction service's own reason. */}
            {engines.detail}
          </Notice>
        </div>
      )}
      {setEngine.error ? (
        <Notice tone="danger" title={m["detectors.failed"]()}>
          {/* i18n: server text, the API's own message: it names the policies that use it. */}
          {setEngine.error instanceof Error
            ? setEngine.error.message
            : m["admin_redaction.unknown_error"]()}
        </Notice>
      ) : null}
      <DetectorCard
        which="patterns"
        title={m["detectors.patterns.title"]()}
        hint={m["detectors.patterns.hint"]()}
        enabled={now.patterns}
        installed={(engines.pattern_entities ?? []).length > 0 || !engines.reachable}
        reachable={engines.reachable}
        usedBy={engines.patterns_used_by ?? []}
        pending={setEngine.isPending}
        onToggle={(next) => toggle("patterns", next)}
      >
        <details>
          <summary className="cursor-pointer text-small font-medium">
            {m["detectors.patterns.count"]({ count: (engines.pattern_entities ?? []).length })}
            {" · "}
            {m["detectors.formats_list"]()}
          </summary>
          <p className={`mt-2 text-small ${MUTED}`}>
            {(engines.pattern_entities ?? []).map((type) => entityLabel(type)).join(", ")}
          </p>
        </details>
      </DetectorCard>
      {(
        [
          ["spacy", spacy, m["detectors.spacy.title"](), m["detectors.spacy.hint"](), m["detectors.how_spacy"]()],
          ["gliner", gliner, m["detectors.gliner.title"](), m["detectors.gliner.hint"](), m["detectors.how_gliner"]()],
        ] as const
      ).map(([which, engine, title, hint, how]) => {
        const installed = engine?.available === true;
        return (
          <DetectorCard
            key={which}
            which={which}
            title={title}
            hint={hint}
            enabled={now[which]}
            installed={installed}
            reachable={engines.reachable}
            usedBy={engine?.used_by ?? []}
            pending={setEngine.isPending}
            onToggle={(next) => toggle(which, next)}
          >
            {installed && engine ? <NerDetails engine={engine} /> : <NotInstalled how={how} id={`detector-${which}-how`} />}
          </DetectorCard>
        );
      })}
    </div>
  );
}

export function AdminPrivacyDetectors() {
  const redaction = useRedactionStatus();
  const engines = usePrivacyEngines();
  // Shaped like a status, or not shown: nothing here guesses at a half-loaded answer.
  const loaded = typeof redaction.data?.enabled === "boolean" ? redaction.data : undefined;
  return (
    <div className={PAGE}>
      <PrivacyHeader tab="service" />
      <p className={`m-0 text-small ${MUTED}`}>{m["privacy.service.subtitle"]()}</p>
      {redaction.error ? (
        <Notice tone="danger" title={m["admin_redaction.load_error.title"]()}>
          {/* i18n: server text, the API's own error message. */}
          {redaction.error instanceof Error ? redaction.error.message : m["admin_redaction.unknown_error"]()}
        </Notice>
      ) : null}
      {loaded && <Warnings status={loaded} />}
      {loaded && Array.isArray(engines.data?.engines) && engines.data && (
        <DetectorCards engines={engines.data} status={loaded} />
      )}
      <UserTypes />
      {loaded && <EngineDetails status={loaded} />}
    </div>
  );
}
