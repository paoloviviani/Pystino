import { Badge, Button, Card, Dialog, Input, Notice, Pagination, Spinner, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";

import { PageHeader } from "../components/PageHeader";
import { useByokHold, useByokModels, useByokProviders } from "../lib/admin";
import { formatDateTime, formatNumber } from "../lib/format";
import { CLAMP, CODE, MUTED, NOWRAP, PAGE, ROW_ACTIONS } from "../lib/layout";
import { usePaginated } from "../lib/paging";
import { useOptionalToast } from "../lib/toast";
import type { ByokModel, ByokOwner, ByokProvider } from "../lib/types";
import { m } from "../paraglide/messages.js";

/**
 * What users have added with their own keys, for administrators.
 *
 * The page exists to answer "what is leaving this gateway for hosts nobody here
 * chose, and whose is it", and to hold a row that should not run. It never
 * shows a credential, and has no place to: the API does not return one (not the
 * key, its hint or its headers), and a column that said "no key" would suggest
 * one could be shown. A host, never the full URL, whose path can carry a token.
 */

type Row = ByokProvider | ByokModel;

/** The hold being confirmed: which row, and whether it is being held or released. */
interface Pending {
  target: "providers" | "models";
  row: Row;
  hold: boolean;
}

function Owner({ owner }: { owner: ByokOwner }) {
  return (
    <>
      {/* i18n: server text — the owner's own name and address. */}
      <div>{owner.name || owner.email || owner.id}</div>
      {owner.name && owner.email && <div className={`${MUTED} ${CLAMP}`}>{owner.email}</div>}
    </>
  );
}

function Status({ row }: { row: Row }) {
  if (row.admin_disabled) return <Badge tone="danger">{m["admin_byok.badge.held"]()}</Badge>;
  if (!row.is_active) return <Badge>{m["admin_byok.badge.off"]()}</Badge>;
  return <Badge tone="ok">{m["admin_byok.badge.enabled"]()}</Badge>;
}

function Usage({ row }: { row: Row }) {
  return (
    <>
      <div className={NOWRAP}>{m["admin_byok.usage.requests"]({ count: formatNumber(row.usage.requests) })}</div>
      <div className={`${MUTED} ${NOWRAP}`}>
        {m["admin_byok.usage.tokens"]({ count: formatNumber(row.usage.total_tokens) })}
      </div>
      {/* The raw decimal, in the currency of the owner's own price rows:
          the API sends no currency here, and a symbol of our choosing would
          be a guess about someone else's money. */}
      <div className={`${MUTED} ${NOWRAP}`}>{m["admin_byok.usage.cost"]({ cost: row.usage.cost })}</div>
    </>
  );
}

function LastUsed({ row }: { row: Row }) {
  return row.usage.last_used_at ? (
    <span className={NOWRAP}>{formatDateTime(row.usage.last_used_at)}</span>
  ) : (
    <span className={MUTED}>{m["admin_byok.never_used"]()}</span>
  );
}

export function AdminByok() {
  const providerPaging = usePaginated();
  const modelPaging = usePaginated();
  const providers = useByokProviders(providerPaging.page);
  const models = useByokModels(modelPaging.page);
  const hold = useByokHold();
  const toast = useOptionalToast();
  const [pending, setPending] = useState<Pending | null>(null);

  const actions = (target: "providers" | "models") => (row: Row) => (
    <div className={ROW_ACTIONS}>
      <Button
        variant="secondary"
        onClick={() => setPending({ target, row, hold: !row.admin_disabled })}
      >
        {row.admin_disabled ? m["admin_byok.action.enable"]() : m["admin_byok.action.disable"]()}
      </Button>
    </div>
  );

  const providerColumns: Column<ByokProvider>[] = [
    { key: "owner", header: m["admin_byok.column.owner"](), render: (row) => <Owner owner={row.owner} /> },
    {
      key: "name",
      header: m["admin_byok.column.provider"](),
      // i18n: server text — the owner's own name for the provider.
      render: (row) => row.name,
    },
    {
      key: "type",
      header: m["admin_byok.column.type"](),
      hideBelow: "lg",
      render: (row) => row.plugin ?? m["admin_byok.type_generic"](),
    },
    {
      key: "host",
      header: m["admin_byok.column.host"](),
      hideBelow: "md",
      render: (row) => <span className={CODE}>{row.base_url_host}</span>,
    },
    {
      key: "models",
      header: m["admin_byok.column.models"](),
      numeric: true,
      hideBelow: "md",
      render: (row) => formatNumber(row.model_count),
    },
    { key: "status", header: m["admin_byok.column.enabled"](), render: (row) => <Status row={row} /> },
    { key: "last_used", header: m["admin_byok.column.last_used"](), hideBelow: "lg", render: (row) => <LastUsed row={row} /> },
    { key: "usage", header: m["admin_byok.column.usage"](), hideBelow: "sm", render: (row) => <Usage row={row} /> },
    { key: "actions", header: "", render: actions("providers") },
  ];

  const modelColumns: Column<ByokModel>[] = [
    { key: "owner", header: m["admin_byok.column.owner"](), render: (row) => <Owner owner={row.owner} /> },
    {
      key: "name",
      header: m["admin_byok.column.model"](),
      render: (row) => (
        <>
          {/* The name the owner sends on /v1, which is what a request log shows. */}
          <div className={CODE}>{row.api_name}</div>
          <div className={`${MUTED} ${CODE} ${CLAMP}`} title={row.upstream_model}>
            {row.upstream_model}
          </div>
        </>
      ),
    },
    {
      key: "provider",
      header: m["admin_byok.column.provider"](),
      hideBelow: "md",
      render: (row) => row.provider_name,
    },
    { key: "status", header: m["admin_byok.column.enabled"](), render: (row) => <Status row={row} /> },
    { key: "last_used", header: m["admin_byok.column.last_used"](), hideBelow: "lg", render: (row) => <LastUsed row={row} /> },
    { key: "usage", header: m["admin_byok.column.usage"](), hideBelow: "sm", render: (row) => <Usage row={row} /> },
    { key: "actions", header: "", render: actions("models") },
  ];

  const close = () => setPending(null);
  const confirm = () => {
    if (!pending) return;
    hold.mutate(
      { target: pending.target, id: pending.row.id, hold: pending.hold },
      {
        onSuccess: () => {
          toast?.add({
            title: pending.hold ? m["admin_byok.toast.held"]() : m["admin_byok.toast.released"](),
            type: "success",
          });
          close();
        },
        onError: () => toast?.add({ title: m["admin_byok.toast.failed"](), type: "error" }),
      },
    );
  };
  const pendingName =
    pending === null
      ? ""
      : pending.target === "models"
        ? (pending.row as ByokModel).api_name
        : (pending.row as ByokProvider).name;

  const section = <T extends Row>(
    title: string,
    query: typeof providers | typeof models,
    paging: ReturnType<typeof usePaginated>,
    columns: Column<T>[],
    noun: string,
  ) => (
    <Card title={title} flush>
      <div className="p-5">
        <Input
          label={m["admin_byok.search"]()}
          value={paging.search}
          onChange={(event) => paging.setSearch(event.target.value)}
          placeholder={m["admin_byok.search_placeholder"]()}
        />
      </div>
      {query.isPending ? (
        <Spinner label={m["admin_byok.loading"]()} />
      ) : query.error ? (
        <Notice tone="danger" title={m["admin_byok.load_error"]()}>
          {/* i18n: server text — the API's own error message. */}
          {query.error instanceof Error ? query.error.message : m["admin_byok.unknown_error"]()}
        </Notice>
      ) : (
        <>
          <Table
            columns={columns}
            rows={(query.data?.items ?? []) as T[]}
            rowKey={(row) => row.id}
            empty={m["admin_byok.empty"]()}
            caption={title}
          />
          <Pagination
            total={query.data?.total ?? 0}
            limit={paging.limit}
            offset={paging.offset}
            onOffsetChange={paging.setOffset}
            noun={noun}
            busy={query.isFetching}
          />
        </>
      )}
    </Card>
  );

  return (
    <div className={PAGE}>
      <PageHeader title={m["admin_byok.title"]()} subtitle={m["admin_byok.subtitle"]()} />

      {section(m["admin_byok.providers_card"](), providers, providerPaging, providerColumns, m["admin_byok.noun_providers"]())}
      {section(m["admin_byok.models_card"](), models, modelPaging, modelColumns, m["admin_byok.noun_models"]())}

      <Dialog
        open={pending !== null}
        title={
          pending?.hold
            ? m["admin_byok.confirm.title_disable"]({ name: pendingName })
            : m["admin_byok.confirm.title_enable"]({ name: pendingName })
        }
        onClose={close}
        footer={
          <>
            <Button onClick={close}>{m["admin_byok.confirm.cancel"]()}</Button>
            <Button variant={pending?.hold ? "danger" : "primary"} busy={hold.isPending} onClick={confirm}>
              {pending?.hold ? m["admin_byok.action.disable"]() : m["admin_byok.action.enable"]()}
            </Button>
          </>
        }
      >
        {hold.error ? (
          <Notice tone="danger">
            {/* i18n: server text — the API's own error message. */}
            {hold.error instanceof Error ? hold.error.message : m["admin_byok.unknown_error"]()}
          </Notice>
        ) : null}
        <p>{pending?.hold ? m["admin_byok.confirm.body_disable"]() : m["admin_byok.confirm.body_enable"]()}</p>
      </Dialog>
    </div>
  );
}
