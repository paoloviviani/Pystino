import { Badge, Button, Card, Dialog, Input, Notice, Spinner, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";
import {
  useCreateProvider,
  useDeleteProvider,
  useProviders,
  useTestProvider,
  useUpdateProvider,
} from "../lib/admin";
import type { AdminProvider, ProviderTestResult } from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import styles from "./Admin.module.css";

export function AdminProviders() {
  const providers = useProviders();
  const update = useUpdateProvider();
  const remove = useDeleteProvider();
  const test = useTestProvider();

  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<AdminProvider | null>(null);
  const [results, setResults] = useState<Record<string, ProviderTestResult>>({});

  const runTest = (provider: AdminProvider) => {
    test.mutate(provider.id, {
      onSuccess: (result) => setResults((current) => ({ ...current, [provider.id]: result })),
    });
  };

  const columns: Column<AdminProvider>[] = [
    {
      key: "name",
      header: "Provider",
      render: (provider) => (
        <>
          <div>{provider.name}</div>
          <div className={`${styles.muted} ${styles.code}`}>{provider.base_url}</div>
          {provider.description && <div className={styles.muted}>{provider.description}</div>}
        </>
      ),
    },
    {
      key: "credential",
      header: "API key",
      render: (provider) =>
        provider.has_api_key ? (
          // Only ever a hint: the key itself is write-only and never leaves the
          // gateway (ADR 0027).
          <span className={styles.code}>{provider.api_key_hint}</span>
        ) : (
          <span className={styles.muted}>none</span>
        ),
    },
    {
      key: "models",
      header: "Models",
      numeric: true,
      render: (provider) => provider.model_count.toLocaleString(),
    },
    {
      key: "status",
      header: "Status",
      render: (provider) => (
        <div className={styles.chips}>
          {provider.is_active ? <Badge tone="ok">Active</Badge> : <Badge>Inactive</Badge>}
          <TestBadge result={results[provider.id]} />
        </div>
      ),
    },
    {
      key: "actions",
      header: "",
      render: (provider) => (
        <div className={styles.rowActions}>
          <Button
            busy={test.isPending && test.variables === provider.id}
            onClick={() => runTest(provider)}
          >
            Test
          </Button>
          <Button onClick={() => setEditing(provider)}>Edit</Button>
          <Button
            busy={update.isPending && update.variables?.id === provider.id}
            onClick={() => update.mutate({ id: provider.id, is_active: !provider.is_active })}
          >
            {provider.is_active ? "Deactivate" : "Activate"}
          </Button>
          <Button
            variant="ghost"
            busy={remove.isPending && remove.variables === provider.id}
            onClick={() => remove.mutate(provider.id)}
          >
            Delete
          </Button>
        </div>
      ),
    },
  ];

  const failures = Object.entries(results).filter(([, result]) => !result.ok);

  return (
    <div className={styles.page}>
      <PageHeader
        title="Providers"
        subtitle="The inference endpoints models are served from. An API key entered here is
          encrypted before it is stored and is never shown again — only a hint, enough to tell two
          keys apart."
        actions={
          <Button variant="primary" onClick={() => setCreating(true)}>
            Add provider
          </Button>
        }
      />

      {remove.error ? (
        <Notice tone="danger" title="Could not delete the provider">
          {remove.error instanceof Error ? remove.error.message : "Unknown error."}
        </Notice>
      ) : null}

      {failures.map(([id, result]) => {
        const provider = providers.data?.find((entry) => entry.id === id);
        return (
          <Notice key={id} tone="warn" title={`${provider?.name ?? "Provider"} is not reachable`}>
            {result.detail}
          </Notice>
        );
      })}

      <Card flush>
        {providers.isPending ? (
          <Spinner label="Loading providers" />
        ) : providers.error ? (
          <Notice tone="danger" title="Could not load providers">
            {providers.error instanceof Error ? providers.error.message : "Unknown error."}
          </Notice>
        ) : (
          <Table
            columns={columns}
            rows={providers.data ?? []}
            rowKey={(provider) => provider.id}
            empty="No providers configured. Nothing can be served until one exists."
            caption="Inference endpoints, their credentials and how many models they serve."
          />
        )}
      </Card>

      <ProviderDialog
        open={creating}
        provider={null}
        onClose={() => setCreating(false)}
      />
      <ProviderDialog
        open={editing !== null}
        provider={editing}
        onClose={() => setEditing(null)}
      />
    </div>
  );
}

function TestBadge({ result }: { result: ProviderTestResult | undefined }) {
  if (!result) return null;
  if (!result.ok) return <Badge tone="danger">Test failed</Badge>;
  return (
    <Badge tone="ok">
      {result.model_count ?? 0} models{result.latency_ms ? ` · ${result.latency_ms}ms` : ""}
    </Badge>
  );
}

/**
 * Create and edit share a dialog, because the fields are the same and the only
 * real difference is what happens to the API key.
 *
 * On edit the key field starts empty and an empty field means "leave it alone" —
 * matching the API's three-way convention. Pre-filling it with the hint would be
 * worse than useless: saving would then store the mask as the credential.
 */
function ProviderDialog({
  open,
  provider,
  onClose,
}: {
  open: boolean;
  provider: AdminProvider | null;
  onClose: () => void;
}) {
  const create = useCreateProvider();
  const update = useUpdateProvider();
  const editing = provider !== null;

  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [description, setDescription] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [clearKey, setClearKey] = useState(false);
  const [streamOptions, setStreamOptions] = useState(true);
  // Keyed remount: without this the fields keep the previous provider's values
  // when a different row is opened.
  const [loadedFor, setLoadedFor] = useState<string | null>(null);

  if (open && loadedFor !== (provider?.id ?? "new")) {
    setLoadedFor(provider?.id ?? "new");
    setName(provider?.name ?? "");
    setBaseUrl(provider?.base_url ?? "");
    setDescription(provider?.description ?? "");
    setApiKey("");
    setClearKey(false);
    setStreamOptions(provider?.forward_stream_options ?? true);
  }

  const pending = create.isPending || update.isPending;
  const error = create.error ?? update.error;

  const submit = () => {
    const done = { onSuccess: onClose };
    if (editing && provider) {
      update.mutate(
        {
          id: provider.id,
          base_url: baseUrl,
          description: description || null,
          forward_stream_options: streamOptions,
          // Three ways, deliberately: a typed key replaces, the explicit clear
          // removes, and neither leaves the stored credential untouched.
          ...(apiKey ? { api_key: apiKey } : clearKey ? { api_key: "" } : {}),
        },
        done,
      );
    } else {
      create.mutate(
        {
          name,
          base_url: baseUrl,
          description: description || null,
          forward_stream_options: streamOptions,
          ...(apiKey ? { api_key: apiKey } : {}),
        },
        done,
      );
    }
  };

  return (
    <Dialog
      open={open}
      title={editing ? `Edit ${provider?.name}` : "Add a provider"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            busy={pending}
            disabled={!baseUrl.trim() || (!editing && !name.trim())}
            onClick={submit}
          >
            {editing ? "Save" : "Add"}
          </Button>
        </>
      }
    >
      {error ? (
        <Notice tone="danger">
          {error instanceof Error ? error.message : "Unknown error."}
        </Notice>
      ) : null}

      {!editing && (
        <Input
          label="Name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="cortecs"
          hint="Letters, digits, dot, dash and underscore. Cannot be changed later."
        />
      )}

      <Input
        label="Base URL"
        value={baseUrl}
        onChange={(event) => setBaseUrl(event.target.value)}
        placeholder="https://api.example.com/v1"
        hint="Include the version path. Most OpenAI-compatible endpoints end in /v1."
      />

      <Input
        label="Description"
        value={description}
        onChange={(event) => setDescription(event.target.value)}
        placeholder="Commercial API, EU region"
      />

      <Input
        label="API key"
        type="password"
        value={apiKey}
        onChange={(event) => setApiKey(event.target.value)}
        autoComplete="new-password"
        placeholder={editing && provider?.has_api_key ? "unchanged" : "leave empty if none needed"}
        hint={
          editing && provider?.has_api_key
            ? `Currently ${provider.api_key_hint}. Type a new key to replace it, or leave empty to keep it.`
            : "Encrypted before storage. A local vLLM or Ollama usually needs none."
        }
      />

      {editing && provider?.has_api_key && (
        <label className={styles.checkItem}>
          <input
            type="checkbox"
            checked={clearKey}
            disabled={apiKey.length > 0}
            onChange={(event) => setClearKey(event.target.checked)}
          />
          <span>Remove the stored key</span>
        </label>
      )}

      <label className={styles.checkItem}>
        <input
          type="checkbox"
          checked={streamOptions}
          onChange={(event) => setStreamOptions(event.target.checked)}
        />
        <span>
          Ask for token usage on streamed responses
          <span className={styles.muted}>
            {" "}— turn off for a provider that sends usage anyway and rejects unknown
            parameters. Cortecs is one.
          </span>
        </span>
      </label>

      <Notice tone="info">
        Use <strong>Test</strong> after saving. It calls the provider&apos;s <code>/models</code>
        {" "}with the credential as stored, so a wrong URL or a stale key shows up now rather than
        in someone&apos;s request.
      </Notice>
    </Dialog>
  );
}
