import { Badge, Button, Card, Input, Money, Notice, Select, Spinner, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";
import { useCreatePrice, useModels, usePrices } from "../lib/admin";
import type { Price } from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import styles from "./Admin.module.css";

export function AdminPricing() {
  const models = useModels();
  const [modelId, setModelId] = useState("");
  const prices = usePrices(modelId || null);

  const model = models.data?.items.find((entry) => entry.id === modelId);
  const now = new Date();

  const columns: Column<Price>[] = [
    {
      key: "effective",
      header: "Effective from",
      render: (price) => (
        <>
          <div>{formatDateTime(price.effective_from)}</div>
          {new Date(price.effective_from) > now && (
            <Badge tone="accent">Scheduled</Badge>
          )}
        </>
      ),
    },
    {
      key: "input",
      header: "Input / Mtok",
      numeric: true,
      render: (price) => <Money amount={price.input_per_mtok} currency={price.currency} />,
    },
    {
      key: "output",
      header: "Output / Mtok",
      numeric: true,
      render: (price) => <Money amount={price.output_per_mtok} currency={price.currency} />,
    },
    {
      key: "image",
      header: "Per image",
      numeric: true,
      // Only image models carry one, and showing a dash beats showing zero:
      // "not priced this way" and "free" are different facts.
      render: (price) =>
        price.per_image ? (
          <Money amount={price.per_image} currency={price.currency} />
        ) : (
          <span className={styles.muted}>—</span>
        ),
    },
    {
      key: "source",
      header: "Source",
      render: (price) => <Badge>{price.source}</Badge>,
    },
  ];

  return (
    <div className={styles.page}>
      <PageHeader
        title="Pricing"
        subtitle="Prices are append-only and effective-dated. There is no edit: a future date
          schedules a change, and a past one cannot rewrite what already-recorded requests cost."
      />

      <Card title="Model">
        <Select
          label="Model"
          hideLabel
          value={modelId}
          onChange={(event) => setModelId(event.target.value)}
        >
          <option value="">Choose a model…</option>
          {(models.data?.items ?? []).map((entry) => (
            <option key={entry.id} value={entry.id}>
              {entry.name}
              {entry.current_price ? "" : "  (unpriced)"}
            </option>
          ))}
        </Select>
      </Card>

      {modelId && (
        <>
          <Card title="Price history" flush description="Newest first. Nothing here is ever mutated.">
            {prices.isPending ? (
              <Spinner />
            ) : prices.error ? (
              <Notice tone="danger" title="Could not load the price history">
                {prices.error instanceof Error ? prices.error.message : "Unknown error."}
              </Notice>
            ) : (
              <Table
                columns={columns}
                rows={prices.data?.items ?? []}
                rowKey={(price) => price.id}
                empty="No price has ever been set. This model records a cost of zero."
                caption={`Price history for ${model?.name ?? "the model"}.`}
              />
            )}
          </Card>

          <AppendPrice modelId={modelId} modelName={model?.name ?? ""} />
        </>
      )}
    </div>
  );
}

function AppendPrice({ modelId, modelName }: { modelId: string; modelName: string }) {
  const create = useCreatePrice();
  const [input, setInput] = useState("");
  const [output, setOutput] = useState("");
  const [perImage, setPerImage] = useState("");
  const [effective, setEffective] = useState("");

  const submit = () => {
    create.mutate(
      {
        modelId,
        input_per_mtok: input,
        output_per_mtok: output,
        // Omitted rather than sent as zero: zero is a real price meaning
        // "free", and a token-priced model has no per-image price at all.
        per_image: perImage === "" ? null : perImage,
        // A local datetime-local value carries no zone; converting through Date
        // makes the browser's zone explicit rather than letting the server guess.
        effective_from: effective ? new Date(effective).toISOString() : null,
      },
      {
        onSuccess: () => {
          setInput("");
          setOutput("");
          setPerImage("");
          setEffective("");
        },
      },
    );
  };

  return (
    <Card title={`Append a price for ${modelName}`}>
      <div className={styles.form}>
        {create.error ? (
          <Notice tone="danger">
            {create.error instanceof Error ? create.error.message : "Unknown error."}
          </Notice>
        ) : null}
        {create.isSuccess && !create.isPending && (
          <Notice tone="info">Price appended. It applies from its effective date onwards.</Notice>
        )}

        <div className={styles.formRow}>
          <Input
            label="Input per Mtok"
            type="number"
            min="0"
            step="0.000001"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            hint="In the gateway's billing currency"
          />
          <Input
            label="Output per Mtok"
            type="number"
            min="0"
            step="0.000001"
            value={output}
            onChange={(event) => setOutput(event.target.value)}
          />
          <Input
            label="Per image"
            type="number"
            min="0"
            step="0.000001"
            value={perImage}
            onChange={(event) => setPerImage(event.target.value)}
            hint="Image models only. Charged per picture, on top of any token rates."
          />
          <Input
            label="Effective from"
            type="datetime-local"
            value={effective}
            onChange={(event) => setEffective(event.target.value)}
            hint="Leave empty for now. A future date schedules the change."
          />
        </div>

        <div>
          <Button
            variant="primary"
            busy={create.isPending}
            disabled={input === "" || output === ""}
            onClick={submit}
          >
            Append price
          </Button>
        </div>
      </div>
    </Card>
  );
}

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
