import { Badge, Button, Card, Dialog, Input, Money, Notice, Spinner, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";
import { useCreateSearchPrice, useSearchPrices } from "../lib/admin";
import { formatDateTime } from "../lib/format";
import { CODE, FORM, FORM_ROW, MUTED } from "../lib/layout";
import type { AdminProvider, SearchPrice } from "../lib/types";
import { useOptionalToast } from "../lib/toast";
import { m } from "../paraglide/messages.js";

/**
 * What one request to a search backend costs, and what it used to cost.
 *
 * Three states are shown apart on purpose, because a report reads them apart:
 * no price (unpriced: searches are recorded at zero with no price behind them),
 * a price of 0 (billed out of band: the operator settles the vendor's invoice
 * outside Pystino, and says so), and a real price. A 0 and "unset" look alike
 * in a number column and mean opposite things, so neither is left to a digit.
 *
 * Prices are append-only. "Set price" adds a row; nothing here edits or
 * deletes one, so a change never rewrites what an earlier search was billed.
 */

// The ledger records a backend by its plugin name, and that is what prices are
// keyed on; a search provider is always created with one.
const priceKey = (backend: AdminProvider) => backend.plugin ?? backend.name;

const isZero = (price: SearchPrice) => Number(price.per_request) === 0;

export function SearchPricesCard({ backends }: { backends: AdminProvider[] }) {
  const prices = useSearchPrices();
  const [pricing, setPricing] = useState<AdminProvider | null>(null);
  const rows = prices.data?.items ?? [];
  const now = Date.now();

  const columns: Column<AdminProvider>[] = [
    {
      key: "backend",
      header: m["admin_search.price.column.backend"](),
      render: (backend) => backend.name,
    },
    {
      key: "price",
      header: m["admin_search.price.column.price"](),
      numeric: true,
      render: (backend) => {
        const current = rows.find(
          (row) => row.backend === priceKey(backend) && row.tier === null && row.in_force,
        );
        if (!current) return <Badge tone="warn">{m["admin_search.price.unpriced"]()}</Badge>;
        return isZero(current) ? (
          <Badge>{m["admin_search.price.out_of_band"]()}</Badge>
        ) : (
          <Money amount={current.per_request} currency={current.currency} />
        );
      },
    },
    {
      key: "overrides",
      header: m["admin_search.price.column.overrides"](),
      render: (backend) => {
        const overrides = rows.filter(
          (row) => row.backend === priceKey(backend) && row.tier !== null && row.in_force,
        );
        if (overrides.length === 0) return <span className={MUTED}>—</span>;
        return (
          <div className="flex flex-col gap-0.5">
            {overrides.map((row) => (
              <span key={row.id}>
                <code className={CODE}>{row.tier}</code>{" "}
                {isZero(row) ? (
                  m["admin_search.price.out_of_band"]()
                ) : (
                  <Money amount={row.per_request} currency={row.currency} />
                )}
              </span>
            ))}
          </div>
        );
      },
    },
    {
      key: "actions",
      header: "",
      render: (backend) => (
        <Button variant="secondary" onClick={() => setPricing(backend)}>
          {m["admin_search.price.action.set"]()}
        </Button>
      ),
    },
  ];

  const historyColumns: Column<SearchPrice>[] = [
    {
      key: "backend",
      header: m["admin_search.price.column.backend"](),
      render: (row) => (
        <>
          {row.backend}
          {row.tier !== null && (
            <>
              {" "}
              <code className={CODE}>{row.tier}</code>
            </>
          )}
        </>
      ),
    },
    {
      key: "price",
      header: m["admin_search.price.column.price"](),
      numeric: true,
      render: (row) =>
        isZero(row) ? (
          <span>
            <Money amount={row.per_request} currency={row.currency} />{" "}
            <span className={MUTED}>{m["admin_search.price.out_of_band_short"]()}</span>
          </span>
        ) : (
          <Money amount={row.per_request} currency={row.currency} />
        ),
    },
    {
      key: "currency",
      header: m["admin_search.price.column.currency"](),
      render: (row) => row.currency,
    },
    {
      key: "from",
      header: m["admin_search.price.column.from"](),
      render: (row) => formatDateTime(row.effective_from),
    },
    {
      key: "state",
      header: m["admin_search.price.column.state"](),
      render: (row) =>
        row.in_force ? (
          <Badge tone="ok">{m["admin_search.price.state.in_force"]()}</Badge>
        ) : new Date(row.effective_from).getTime() > now ? (
          <Badge tone="accent">{m["admin_search.price.state.scheduled"]()}</Badge>
        ) : (
          <Badge>{m["admin_search.price.state.superseded"]()}</Badge>
        ),
    },
  ];

  return (
    <>
      <Card flush>
        <div className="p-4">
          <h2 className="text-heading font-semibold">{m["admin_search.price.heading"]()}</h2>
          <p className={`${MUTED} mt-1 text-body`}>{m["admin_search.price.body"]()}</p>
        </div>
        {prices.isPending ? (
          <div className="p-6">
            <Spinner label={m["admin_search.price.loading"]()} />
          </div>
        ) : (
          <Table
            columns={columns}
            rows={backends}
            rowKey={(row) => row.id}
            empty={m["admin_search.price.backends_empty"]()}
          />
        )}
      </Card>

      <Card title={m["admin_search.price.history_heading"]()} flush>
        <Table
          columns={historyColumns}
          rows={rows}
          rowKey={(row) => row.id}
          empty={m["admin_search.price.history_empty"]()}
        />
      </Card>

      <SetPriceDialog backend={pricing} onClose={() => setPricing(null)} />
    </>
  );
}

function SetPriceDialog({
  backend,
  onClose,
}: {
  backend: AdminProvider | null;
  onClose: () => void;
}) {
  const create = useCreateSearchPrice();
  const toast = useOptionalToast();
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState("");
  const [tier, setTier] = useState("");
  const [effective, setEffective] = useState("");
  const [openFor, setOpenFor] = useState<string | null>(null);

  // A fresh form for each backend, never the previous one's leftovers.
  if (backend && openFor !== backend.id) {
    setOpenFor(backend.id);
    setAmount("");
    setCurrency("");
    setTier("");
    setEffective("");
    create.reset();
  }
  if (!backend && openFor !== null) setOpenFor(null);

  const submit = () => {
    if (!backend) return;
    create.mutate(
      {
        backend: priceKey(backend),
        per_request: amount,
        // Left out rather than sent empty: the gateway then applies the
        // billing currency, which is the right answer for nearly everyone.
        currency: currency.trim() === "" ? null : currency.trim().toUpperCase(),
        tier: tier.trim() === "" ? null : tier.trim(),
        // A datetime-local value carries no zone; converting through Date
        // makes the browser's zone explicit rather than letting the server guess.
        effective_from: effective ? new Date(effective).toISOString() : null,
      },
      {
        onSuccess: () => {
          toast?.add({ title: m["admin_search.price.toast.saved"](), type: "success" });
          onClose();
        },
        onError: () =>
          toast?.add({ title: m["admin_search.price.toast.failed"](), type: "error" }),
      },
    );
  };

  return (
    <Dialog
      open={backend !== null}
      title={m["admin_search.price.dialog.title"]({ backend: backend?.name ?? "" })}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{m["admin_search.dialog.cancel"]()}</Button>
          <Button variant="primary" busy={create.isPending} disabled={amount === ""} onClick={submit}>
            {m["admin_search.price.dialog.submit"]()}
          </Button>
        </>
      }
    >
      <div className={FORM}>
        {create.error ? (
          <Notice tone="danger">
            {/* i18n: server text — the API's own error message. */}
            {create.error instanceof Error ? create.error.message : m["admin_search.unknown_error"]()}
          </Notice>
        ) : null}
        <Input
          label={m["admin_search.price.field.amount"]()}
          type="number"
          min="0"
          step="0.000001"
          value={amount}
          onChange={(event) => setAmount(event.target.value)}
          hint={m["admin_search.price.field.amount_hint"]()}
        />
        <div className={FORM_ROW}>
          <Input
            label={m["admin_search.price.field.currency"]()}
            value={currency}
            maxLength={3}
            onChange={(event) => setCurrency(event.target.value)}
            hint={m["admin_search.price.field.currency_hint"]()}
          />
          <Input
            label={m["admin_search.price.field.tier"]()}
            value={tier}
            onChange={(event) => setTier(event.target.value)}
            hint={m["admin_search.price.field.tier_hint"]()}
          />
          <Input
            label={m["admin_search.price.field.effective"]()}
            type="datetime-local"
            value={effective}
            onChange={(event) => setEffective(event.target.value)}
            hint={m["admin_search.price.field.effective_hint"]()}
          />
        </div>
      </div>
    </Dialog>
  );
}
