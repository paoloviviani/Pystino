import {
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  Input,
  Notice,
  Select,
  Spinner,
  Switch,
} from "@llmp/ui";
import { useState } from "react";
import { Link } from "react-router";

import { PrivacyHeader } from "../components/PrivacyHeader";
import { Tip } from "../components/Tip";
import { FIELD_LABEL, FORM, MUTED, PAGE, TEXTAREA } from "../lib/layout";
import {
  useClassifiers,
  useDeleteClassifier,
  usePrivacySettings,
  useSavePrivacySettings,
  useSaveClassifier,
} from "../lib/privacy";
import type { PrivacyClassifier } from "../lib/types";
import { m } from "../paraglide/messages.js";

/**
 * Classifiers (ADR 0113): a decision model and a topic written in plain words,
 * which a rule can name ("classified as ...").
 *
 * Scaffolding for larger deployments: nothing is bundled and no topic is
 * seeded. The one thing the page must say, where the model is chosen, is that
 * classifying sends the text to that model's provider. Without a chat model
 * there is nothing to choose, and the page says what is missing.
 */
function ClassifierDialog({
  classifier,
  models,
  onClose,
}: {
  /** null: a new classifier. */
  classifier: PrivacyClassifier | null;
  models: { id: string; name: string; provider: string | null; local?: boolean }[];
  onClose: () => void;
}) {
  const save = useSaveClassifier();
  const [name, setName] = useState(classifier?.name ?? "");
  const [topic, setTopic] = useState(classifier?.topic ?? "");
  const [modelId, setModelId] = useState(classifier?.model_id ?? "");
  const chosen = models.find((model) => model.id === modelId);
  return (
    <Dialog
      open
      title={classifier ? m["classifiers.edit"]() : m["classifiers.new"]()}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{m["privacy.delete.cancel"]()}</Button>
          <Button
            variant="primary"
            busy={save.isPending}
            disabled={!name.trim()}
            onClick={() =>
              save.mutate(
                {
                  id: classifier?.id,
                  body: { name: name.trim(), topic, model_id: modelId || null },
                },
                { onSuccess: onClose },
              )
            }
          >
            {m["classifiers.save"]()}
          </Button>
        </>
      }
    >
      <div className={FORM}>
        {save.error && (
          <Notice tone="danger" title={m["privacy.save_error"]()}>
            {/* i18n: server text, the API's own message. */}
            {save.error.message}
          </Notice>
        )}
        <Input
          label={m["classifiers.name"]()}
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-2">
            <label htmlFor="classifier-topic" className={FIELD_LABEL}>
              {m["classifiers.topic"]()}
            </label>
            <Tip text={m["classifiers.topic_tip"]()} />
          </div>
          <textarea
            id="classifier-topic"
            rows={4}
            className={TEXTAREA}
            value={topic}
            placeholder={m["classifiers.topic_placeholder"]()}
            onChange={(event) => setTopic(event.target.value)}
          />
        </div>
        <Select
          label={m["classifiers.model"]()}
          value={modelId}
          onChange={(event) => setModelId(event.target.value)}
        >
          <option value="">{m["classifiers.no_model"]()}</option>
          {models.map((model) => (
            <option key={model.id} value={model.id}>
              {model.provider ? `${model.provider} · ${model.name}` : model.name}
            </option>
          ))}
        </Select>
        {/* Where the model is chosen, because this is where the text's
            destination is decided. */}
        {chosen?.local ? (
          <Notice tone="info">{m["classifiers.stays_inside"]()}</Notice>
        ) : (
          <Notice tone="warn">
            {chosen?.provider
              ? m["classifiers.sends_to"]({ provider: chosen.provider })
              : m["classifiers.sends"]()}
          </Notice>
        )}
      </div>
    </Dialog>
  );
}

export function AdminPrivacyClassifiers() {
  const list = useClassifiers();
  const remove = useDeleteClassifier();
  const [editing, setEditing] = useState<PrivacyClassifier | "new" | null>(null);
  const [deleting, setDeleting] = useState<PrivacyClassifier | null>(null);
  const items = list.data?.items ?? [];
  const models = list.data?.models ?? [];
  const noModels = list.data !== undefined && models.length === 0;
  const allowRemote = list.data?.allow_remote ?? false;
  const settings = usePrivacySettings();
  const saveSettings = useSavePrivacySettings();
  return (
    <div className={PAGE}>
      <PrivacyHeader
        tab="classifiers"
        actions={
          <Button variant="primary" disabled={noModels} onClick={() => setEditing("new")}>
            {m["classifiers.new"]()}
          </Button>
        }
      />
      <p className={`m-0 flex items-center gap-2 text-small ${MUTED}`}>
        {m["classifiers.lead"]()}
        <Tip text={m["classifiers.lead_tip"]()} />
      </p>
      {list.isPending && <Spinner label={m["privacy.loading"]()} />}
      {Boolean(list.error) && (
        <Notice tone="danger" title={m["privacy.load_error"]()}>
          {/* i18n: server text, the API's own message. */}
          {list.error instanceof Error ? list.error.message : m["privacy.unknown_error"]()}
        </Notice>
      )}
      {noModels && !allowRemote && (
        <Notice tone="info" title={m["classifiers.needs_local"]()}>
          {m["classifiers.needs_local_text"]()}
        </Notice>
      )}
      {noModels && allowRemote && (
        <Notice tone="info" title={m["classifiers.needs_provider"]()}>
          {m["classifiers.needs_provider_text"]()}{" "}
          <Link to="/admin/providers">{m["classifiers.providers_link"]()}</Link>
        </Notice>
      )}
      {list.data && (
        <Card title={m["classifiers.remote_title"]()} description={m["classifiers.remote_hint"]()}>
          <div className="flex flex-col gap-3">
            {saveSettings.error && (
              <Notice tone="danger">
                {/* i18n: server text, the API's own message. */}
                {saveSettings.error.message}
              </Notice>
            )}
            <div className="flex items-center gap-3">
              <Switch
                label={m["classifiers.remote_switch"]()}
                checked={allowRemote}
                disabled={saveSettings.isPending || settings.data === undefined}
                onChange={(next) =>
                  saveSettings.mutate({
                    user_entity_types: settings.data?.user_entity_types ?? null,
                    allow_remote_classifiers: next,
                  })
                }
              />
              <span className="text-body">{m["classifiers.remote_switch"]()}</span>
            </div>
          </div>
        </Card>
      )}
      {list.data && items.length === 0 && (
        <EmptyState title={m["classifiers.empty"]()} detail={m["classifiers.empty_hint"]()} />
      )}
      {remove.error && (
        <Notice tone="danger" title={m["privacy.save_error"]()}>
          {/* i18n: server text, the API's own message (it names the rules using it). */}
          {remove.error.message}
        </Notice>
      )}
      {items.map((c) => (
        <Card key={c.id}>
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="flex flex-wrap items-center gap-2">
                {/* i18n: server text, the name an administrator gave the classifier. */}
                <strong>{c.name}</strong>
                {c.available ? (
                  <Badge tone="ok">{m["classifiers.available"]()}</Badge>
                ) : (
                  <Badge tone="warn">{m["classifiers.unavailable"]()}</Badge>
                )}
                <Badge tone="neutral">{m["classifiers.rules_using"]({ count: String(c.rules_using) })}</Badge>
              </div>
              <div className="flex flex-wrap gap-1">
                <Button size="sm" onClick={() => setEditing(c)}>
                  {m["privacy.row.edit"]()}
                </Button>
                <Button size="sm" className="text-danger" onClick={() => setDeleting(c)}>
                  {m["privacy.row.delete"]()}
                </Button>
              </div>
            </div>
            {/* i18n: server text, written by the administrator. */}
            {c.topic && <p className="m-0 text-body">{c.topic}</p>}
            <p className={`m-0 text-small ${MUTED}`}>
              {c.model_name
                ? m["classifiers.model_line"]({
                    model: c.provider_name ? `${c.provider_name} · ${c.model_name}` : c.model_name,
                  })
                : m["classifiers.no_model"]()}
              {!c.available && c.why_not && (
                <>
                  {" · "}
                  {/* i18n: server text, why this classifier cannot answer. */}
                  {c.why_not}
                </>
              )}
            </p>
          </div>
        </Card>
      ))}
      {editing !== null && (
        <ClassifierDialog
          key={editing === "new" ? "new" : editing.id}
          classifier={editing === "new" ? null : editing}
          models={models}
          onClose={() => setEditing(null)}
        />
      )}
      <Dialog
        open={deleting !== null}
        title={m["classifiers.delete_title"]({ name: deleting?.name ?? "" })}
        onClose={() => setDeleting(null)}
        footer={
          <>
            <Button onClick={() => setDeleting(null)}>{m["privacy.delete.cancel"]()}</Button>
            <Button
              variant="danger"
              busy={remove.isPending}
              onClick={() => {
                if (deleting)
                  remove.mutate(deleting.id, {
                    onSuccess: () => setDeleting(null),
                    // The server refuses while a rule names it; the message
                    // shows on the page, behind the dialog's close.
                    onError: () => setDeleting(null),
                  });
              }}
            >
              {m["privacy.delete.confirm"]()}
            </Button>
          </>
        }
      >
        <p className="m-0">{m["classifiers.delete_body"]()}</p>
      </Dialog>
    </div>
  );
}
