import { Button, Input, Card, Notice } from "@llmp/ui";
import { useState } from "react";

import { PrivacyHeader } from "../components/PrivacyHeader";
import { useRedactionStatus } from "../lib/admin";
import { EngineDetails, EngineSettings } from "./AdminRedaction";
import { MUTED, PAGE } from "../lib/layout";
import { usePrivacySettings, useSavePrivacySettings } from "../lib/privacy";
import { m } from "../paraglide/messages.js";

/**
 * The redaction service: which engine is in force, the detection families, and
 * what users may add to their own policies. Which NER engine reads a policy is
 * the policy's own choice (its editor), not set here.
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

export function AdminPrivacyService() {
  const redaction = useRedactionStatus();
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
      {redaction.data && <EngineSettings status={redaction.data} />}
      <UserTypes />
      {redaction.data && <EngineDetails status={redaction.data} />}
    </div>
  );
}
