/**
 * The Knowledge screen (ADR 0062): how documents are extracted and embedded
 * here, and every base built that way.
 *
 * One screen rather than two because the configuration and its consequences
 * are the same question. The setting an administrator changes here does not
 * touch a single existing base — each pins the model it was indexed with — so
 * the only way to understand what a change *means* is to see, in the same
 * view, which bases are now on an older model and would be re-embedded.
 *
 * That is what the `stale` column is, and it is why the reindex button lives
 * beside it rather than on a base's own page: the decision is "bring these up
 * to the current configuration", and it is made while looking at the list.
 */

import { Badge, Button, Card, Input, Notice, Select, Spinner, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { PageHeader } from "../components/PageHeader";
import { useKnowledge, useReindexBase, useSetKnowledgeConfig } from "../lib/admin";
import { FORM, MUTED, NOWRAP, PAGE, ROW_ACTIONS } from "../lib/layout";
import { useOptionalToast } from "../lib/toast";
import type { KnowledgeBaseSummary, KnowledgeConfigInput, KnowledgeStatus } from "../lib/types";

/** The built-in extractor has no model row, so the empty option means it. */
const BUILT_IN = "";

export function AdminKnowledge() {
  const knowledge = useKnowledge();

  if (knowledge.isPending) return <Spinner label="Loading the knowledge configuration…" />;
  if (knowledge.error) {
    return <Notice tone="danger">Could not read the knowledge configuration.</Notice>;
  }

  const status = knowledge.data;
  return (
    <div className={PAGE}>
      <PageHeader
        title="Knowledge"
        subtitle="How documents become searchable: what reads them, what embeds them,
          and where the vectors live. Changing a setting here affects the next base
          built, never one that already exists."
      />
      {status.detail ? (
        <Notice tone="warn" title={status.enabled ? "Not ready yet" : "Switched off"}>
          {status.detail}
        </Notice>
      ) : null}
      <PipelineCard status={status} />
      <BasesCard status={status} />
      <HistoryCard status={status} />
    </div>
  );
}

// -- the pipeline ------------------------------------------------------------

function PipelineCard({ status }: { status: KnowledgeStatus }) {
  const save = useSetKnowledgeConfig();
  const toast = useOptionalToast();

  const [embedding, setEmbedding] = useState("");
  const [extractor, setExtractor] = useState(BUILT_IN);
  const [chunkChars, setChunkChars] = useState("");
  const [chunkOverlap, setChunkOverlap] = useState("");
  const [reason, setReason] = useState("");

  // Re-seeded whenever the server's answer changes, so the form always starts
  // from what is actually in force rather than from what it was when the tab
  // was opened — the other worker may have been reconfigured since.
  useEffect(() => {
    setEmbedding(status.embedding_model ?? "");
    setExtractor(status.extractor_model ?? BUILT_IN);
    setChunkChars(String(status.chunk_chars));
    setChunkOverlap(String(status.chunk_overlap));
    setReason("");
  }, [status]);

  const embeddingChanged = embedding !== (status.embedding_model ?? "");
  const extractorChanged = extractor !== (status.extractor_model ?? BUILT_IN);
  // The only change that alters *where documents go*, and therefore the only
  // one that asks for a sentence. The server enforces this too; the form
  // states it so the refusal is never a surprise.
  const needsReason = extractorChanged && extractor !== BUILT_IN;

  async function submit(event: FormEvent) {
    event.preventDefault();
    const body: KnowledgeConfigInput = {};
    if (embeddingChanged && embedding) body.embedding_model = embedding;
    if (extractorChanged) {
      if (extractor === BUILT_IN) body.clear_extractor = true;
      else body.extractor_model = extractor;
    }
    const chars = Number(chunkChars);
    const overlap = Number(chunkOverlap);
    if (Number.isFinite(chars) && chars !== status.chunk_chars) body.chunk_chars = chars;
    if (Number.isFinite(overlap) && overlap !== status.chunk_overlap) {
      body.chunk_overlap = overlap;
    }
    if (reason.trim()) body.reason = reason.trim();

    if (Object.keys(body).length === 0) {
      toast?.add({ title: "Nothing to change", type: "info" });
      return;
    }
    try {
      const next = await save.mutateAsync(body);
      toast?.add({
        title: "Saved",
        // The consequence, not the acknowledgement. "Saved" alone would leave
        // an administrator unaware that they have just stranded four bases on
        // a model nothing new will use.
        description:
          next.stale_base_count > 0
            ? `${next.stale_base_count} base${next.stale_base_count === 1 ? "" : "s"} ` +
              "now on an older embedding model. They still answer searches; reindex to bring them forward."
            : undefined,
        type: "success",
      });
    } catch {
      /* the notice below carries it */
    }
  }

  const noEmbeddingModels = status.available_embedding_models.length === 0;

  return (
    <Card
      title="Pipeline"
      description={
        `Decided in ${status.source === "console" ? "the console" : "the environment"}. ` +
        `A change reaches every worker within ${Math.round(status.propagation_seconds)}s.`
      }
    >
      <form className={FORM} onSubmit={submit}>
        <Select
          label="Embedding model"
          value={embedding}
          onChange={(event) => setEmbedding(event.target.value)}
          disabled={noEmbeddingModels}
          hint={
            noEmbeddingModels
              ? "This deployment has no embedding model. Import or create one on the Models screen first."
              : "Existing bases keep the model they were indexed with. Vectors from two models are not comparable, so a base only moves when it is reindexed."
          }
        >
          <option value="">— none chosen —</option>
          {status.available_embedding_models.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </Select>

        <Select
          label="Document extraction"
          value={extractor}
          onChange={(event) => setExtractor(event.target.value)}
          hint="The built-in extractor reads Word, Excel, PowerPoint and PDFs with a text
            layer, on this hardware. A scan needs an OCR model, which sends the document to
            that provider."
        >
          <option value={BUILT_IN}>Built in — never leaves this deployment</option>
          {status.available_extractor_models.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </Select>

        <Input
          label="Characters per passage"
          type="number"
          min={80}
          max={20000}
          value={chunkChars}
          onChange={(event) => setChunkChars(event.target.value)}
          hint="Each base snapshots this when it is created, so a change here applies to new
            bases only."
        />
        <Input
          label="Overlap between passages"
          type="number"
          min={0}
          max={5000}
          value={chunkOverlap}
          onChange={(event) => setChunkOverlap(event.target.value)}
          hint="Characters repeated from the previous passage, so a fact that straddles a
            boundary is in both. Capped at a third of the passage size."
        />

        {needsReason ? (
          <Input
            label="Why"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            hint="This extractor sends documents to a provider rather than reading them here.
              The reason is kept with the change."
            required
          />
        ) : null}

        {save.error ? (
          <Notice tone="danger">
            {save.error instanceof Error ? save.error.message : "The change was refused."}
          </Notice>
        ) : null}

        <div className={ROW_ACTIONS}>
          <Button type="submit" disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Save"}
          </Button>
        </div>
      </form>
    </Card>
  );
}

// -- the bases ---------------------------------------------------------------

function BasesCard({ status }: { status: KnowledgeStatus }) {
  const reindex = useReindexBase();
  const toast = useOptionalToast();
  const [pending, setPending] = useState<string | null>(null);

  async function run(base: KnowledgeBaseSummary) {
    setPending(base.id);
    try {
      await reindex.mutateAsync(base.id);
      toast?.add({
        title: `Reindexing ${base.name}`,
        description: "Its documents will show as in progress until every passage is re-embedded.",
        type: "success",
      });
    } catch (error) {
      toast?.add({
        title: "The reindex was refused",
        description: error instanceof Error ? error.message : undefined,
        type: "error",
      });
    } finally {
      setPending(null);
    }
  }

  const columns: Column<KnowledgeBaseSummary>[] = [
    {
      key: "name",
      header: "Base",
      render: (base) => (
        <div>
          <div>{base.name}</div>
          {base.description ? <div className={MUTED}>{base.description}</div> : null}
        </div>
      ),
    },
    {
      key: "owner",
      header: "Owner",
      hideBelow: "md",
      render: (base) => (
        <div>
          <div>{base.owner_email ?? <span className={MUTED}>erased</span>}</div>
          {base.group_name ? <div className={MUTED}>{base.group_name}</div> : null}
        </div>
      ),
    },
    {
      key: "model",
      header: "Embedded with",
      hideBelow: "sm",
      render: (base) => (
        <span className={NOWRAP}>
          {base.embedding_model ?? <span className={MUTED}>not indexed</span>}
          {base.stale ? (
            <>
              {" "}
              <Badge tone="warn">older model</Badge>
            </>
          ) : null}
        </span>
      ),
    },
    {
      key: "documents",
      header: "Documents",
      numeric: true,
      render: (base) => (
        <span className={NOWRAP}>
          {base.document_count}
          {base.failed_count > 0 ? (
            <>
              {" "}
              <Badge tone="danger">{base.failed_count} failed</Badge>
            </>
          ) : null}
        </span>
      ),
    },
    {
      key: "chunks",
      header: "Passages",
      numeric: true,
      hideBelow: "sm",
      render: (base) => base.chunk_count,
    },
    {
      key: "shares",
      header: "Shared",
      numeric: true,
      hideBelow: "lg",
      render: (base) =>
        base.share_count === 0 ? <span className={MUTED}>—</span> : base.share_count,
    },
    {
      key: "actions",
      header: "",
      render: (base) => (
        <div className={ROW_ACTIONS}>
          <Button
            variant="secondary"
            onClick={() => run(base)}
            disabled={pending !== null || !status.ready}
          >
            {pending === base.id ? "Reindexing…" : "Reindex"}
          </Button>
        </div>
      ),
    },
  ];

  return (
    <Card
      title="Knowledge bases"
      description={
        status.stale_base_count > 0
          ? `${status.stale_base_count} of ${status.bases.length} were indexed with a different embedding model. They still answer searches, from their own vectors.`
          : "Every base is on the embedding model configured above."
      }
    >
      <Table
        columns={columns}
        rows={status.bases}
        rowKey={(base) => base.id}
        empty="No knowledge bases yet. They are created by users, not here."
        caption="Knowledge bases, who owns them and what they were indexed with."
      />
    </Card>
  );
}

// -- the trail ---------------------------------------------------------------

function HistoryCard({ status }: { status: KnowledgeStatus }) {
  if (status.history.length === 0) return null;

  const columns: Column<KnowledgeStatus["history"][number]>[] = [
    {
      key: "when",
      header: "When",
      render: (entry) => (
        <span className={NOWRAP}>{new Date(entry.created_at).toLocaleString()}</span>
      ),
    },
    {
      key: "who",
      header: "Who",
      render: (entry) => entry.changed_by ?? <span className={MUTED}>erased</span>,
    },
    {
      key: "what",
      header: "Change",
      render: (entry) => (
        <div>
          <div>
            {entry.embedding_model ?? "no embedding model"}
            {" · "}
            {entry.extractor_model ?? "built-in extractor"}
          </div>
          {entry.reason ? <div className={MUTED}>{entry.reason}</div> : null}
        </div>
      ),
    },
  ];

  return (
    <Card
      title="Changes"
      description="Append-only: every decision is kept, because the question
        'which model was this base built with, and who moved the default' gets asked
        long after the change."
    >
      <Table
        columns={columns}
        rows={status.history}
        rowKey={(entry) => entry.id}
        empty="No changes recorded."
        caption="Past knowledge configurations, newest first."
      />
    </Card>
  );
}
