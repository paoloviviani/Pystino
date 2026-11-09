import { Button, Dialog, EmptyState, Notice, Spinner } from "@llmp/ui";
import { useState } from "react";
import { useNavigate } from "react-router";

import { PipelineCard } from "../components/PipelineCard";
import { PrivacyHeader } from "../components/PrivacyHeader";
import { PrivacyMasterSwitch } from "../components/PrivacyMasterSwitch";
import { MUTED, PAGE } from "../lib/layout";
import {
  KINDS,
  kindHint,
  kindLabel,
  useDeletePipeline,
  usePipelines,
  useReorderPipelines,
  useSavePipeline,
} from "../lib/privacy";
import type { PrivacyKind, PrivacyPipeline } from "../lib/types";
import { m } from "../paraglide/messages.js";

/**
 * Pipelines (ADR 0113), by kind: when and how a request is handled.
 *
 * Each card is a pipeline: its scope (who, which target), its rules in order, and
 * the fixed last rule. Every pipeline in scope runs and the results combine, so
 * the order here matters only for the one thing a first match decides (inside a
 * pipeline) and for which policy's allow-list counts.
 */
export function AdminPrivacy() {
  const pipelines = usePipelines("admin");
  const save = useSavePipeline("admin");
  const remove = useDeletePipeline("admin");
  const reorder = useReorderPipelines();
  const navigate = useNavigate();
  const [deleting, setDeleting] = useState<PrivacyPipeline | null>(null);
  const items = pipelines.data?.items ?? [];

  const move = (kind: PrivacyKind, id: string, delta: -1 | 1) => {
    const ids = items.filter((p) => p.kind === kind).map((p) => p.id);
    const at = ids.indexOf(id);
    const to = at + delta;
    if (at < 0 || to < 0 || to >= ids.length) return;
    const next = [...ids];
    [next[at], next[to]] = [next[to]!, next[at]!];
    reorder.mutate({ kind, order: next });
  };

  return (
    <div className={PAGE}>
      <PrivacyHeader
        tab="pipelines"
        actions={
          <Button variant="primary" onClick={() => navigate("/admin/privacy/pipelines/new")}>
            {m["privacy.pipelines.new"]()}
          </Button>
        }
      />
      <PrivacyMasterSwitch />
      {pipelines.isPending && <Spinner label={m["privacy.loading"]()} />}
      {Boolean(pipelines.error) && (
        <Notice tone="danger" title={m["privacy.load_error"]()}>
          {/* i18n: server text, the API's own message. */}
          {pipelines.error instanceof Error ? pipelines.error.message : m["privacy.unknown_error"]()}
        </Notice>
      )}
      {Boolean(save.error || remove.error || reorder.error) && (
        <Notice tone="danger" title={m["privacy.save_error"]()}>
          {/* i18n: server text, the API's own message. */}
          {((save.error ?? remove.error ?? reorder.error) as Error).message}
        </Notice>
      )}
      {pipelines.data &&
        KINDS.map((kind) => {
          const mine = items.filter((p) => p.kind === kind);
          return (
            <section key={kind} className="flex flex-col gap-3" aria-label={kindLabel(kind)}>
              <div className="flex flex-wrap items-baseline gap-2">
                <h2 className="m-0 text-heading font-semibold">{kindLabel(kind)}</h2>
                <span className={`text-small ${MUTED}`}>{kindHint(kind)}</span>
              </div>
              {mine.length === 0 && (
                <EmptyState title={m["privacy.pipelines.empty"]()} detail={m["privacy.pipelines.empty_hint"]()} />
              )}
              {mine.map((p, index) => (
                <PipelineCard
                  key={p.id}
                  pipeline={p}
                  editTo={`/admin/privacy/pipelines/${p.id}`}
                  first={index === 0}
                  last={index === mine.length - 1}
                  onMove={(delta) => move(kind, p.id, delta)}
                  onToggle={(enabled) => save.mutate({ id: p.id, body: { enabled } })}
                  onDelete={() => setDeleting(p)}
                />
              ))}
            </section>
          );
        })}
      <Dialog
        open={deleting !== null}
        title={m["privacy.delete.title"]({ name: deleting?.name ?? "" })}
        onClose={() => setDeleting(null)}
        footer={
          <>
            <Button onClick={() => setDeleting(null)}>{m["privacy.delete.cancel"]()}</Button>
            <Button
              variant="danger"
              busy={remove.isPending}
              onClick={() => {
                if (deleting) remove.mutate(deleting.id, { onSuccess: () => setDeleting(null) });
              }}
            >
              {m["privacy.delete.confirm"]()}
            </Button>
          </>
        }
      >
        <p className="m-0">{m["privacy.delete.body"]()}</p>
      </Dialog>
    </div>
  );
}
