import { Badge, Card, Input, Notice, Pagination, Spinner, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";
import { Link } from "react-router";

import { PageHeader } from "../components/PageHeader";
import { kindLabel } from "../lib/privacy";
import { entityLabel, modeLabel } from "../lib/entities";
import { formatDateTime } from "../lib/format";
import { MUTED, NOWRAP, PAGE } from "../lib/layout";
import { useRedactionEvents } from "../lib/myRedaction";
import type { RedactionEvent } from "../lib/types";
import { m } from "../paraglide/messages.js";

/**
 * Redactions: what the gateway hides from providers for a user, in one place.
 *
 * Four things, top to bottom: what the administrators' rules already do (read
 * only), the user's own rules (which can only add protection), a box to try
 * text against both, and the log of requests where something was hidden. The
 * log holds no values, only what kind of thing, how many and which rule.
 */

const PAGE_SIZE = 25;

function RoutingCell({ routing }: { routing: RedactionEvent["routing"] }) {
  if (!routing) return <span className={MUTED}>{m["my_redaction.routing.none"]()}</span>;
  const rule = routing.handled.routing_rule;
  const observed = routing.observed;
  const seen = [observed.filetype, observed.is_scan ? m["my_redaction.routing.scan"]() : null].filter(
    Boolean,
  );
  return (
    <span className="flex flex-col gap-0.5">
      <span>
        {kindLabel(routing.kind)}
        {" · "}
        {rule
          ? m["my_redaction.routing.rule"]({ name: `${rule.pipeline ?? ""} › ${rule.name}` })
          : m["my_redaction.routing.no_rule"]()}
      </span>
      {routing.handled.extractor && (
        <span className="text-small">
          {m["my_redaction.routing.reader"]({ name: routing.handled.extractor })}
        </span>
      )}
      {seen.length > 0 && <span className={`text-small ${MUTED}`}>{seen.join(" · ")}</span>}
      {routing.handled.fallback_reason && (
        <span className="text-small">
          <Badge tone="warn">
            {m["my_redaction.routing.fallback"]({ reason: routing.handled.fallback_reason })}
          </Badge>
        </span>
      )}
      {routing.outcome === "refused" && (
        <span className="text-small">
          <Badge tone="danger">{m["my_redaction.routing.refused"]()}</Badge>
        </span>
      )}
    </span>
  );
}

function entitySummary(event: RedactionEvent) {
  if (event.entities.length === 0) {
    return <span className={MUTED}>{m["my_redaction.log.nothing_hidden"]()}</span>;
  }
  return event.entities.map((entity) => (
    <span key={entity.type} className="block">
      {m["my_redaction.log.entity"]({
        type: entityLabel(entity.type),
        count: entity.count,
        mode: modeLabel(entity.mode),
      })}
    </span>
  ));
}

const SOURCE_LABEL: Record<string, () => string> = {
  message: m["my_redaction.source.message"],
  tool_result: m["my_redaction.source.tool_result"],
  document: m["my_redaction.source.document"],
};

function Log() {
  const [requestId, setRequestId] = useState("");
  const [offset, setOffset] = useState(0);
  const log = useRedactionEvents({ requestId, limit: PAGE_SIZE, offset });
  const page = log.data;

  const columns: Column<RedactionEvent>[] = [
    {
      key: "time",
      header: m["my_redaction.log.column.time"](),
      nowrap: true,
      render: (event) => formatDateTime(event.created_at),
    },
    {
      key: "model",
      header: m["my_redaction.log.column.model"](),
      render: (event) => (
        <span>
          {event.model}
          {event.provider && <span className={`block text-small ${MUTED}`}>{event.provider}</span>}
          {event.byok && (
            <>
              {" "}
              <Badge>{m["my_redaction.log.own_key"]()}</Badge>
            </>
          )}
        </span>
      ),
    },
    {
      key: "what",
      header: m["my_redaction.log.column.hidden"](),
      render: (event) => entitySummary(event),
    },
    {
      key: "handled",
      header: m["my_redaction.log.column.handled"](),
      render: (event) => <RoutingCell routing={event.routing} />,
    },
    {
      key: "rule",
      header: m["my_redaction.log.column.rule"](),
      hideBelow: "md",
      render: (event) =>
        event.entities.map((entity) => (
          <span key={entity.type} className="block text-small">
            {entity.rule
              ? entity.rule.owner === "user"
                ? m["my_redaction.log.rule_you"]({ name: entity.rule.name })
                : m["my_redaction.log.rule_admin"]({ name: entity.rule.name })
              : m["my_redaction.test.by_unknown"]()}
          </span>
        )),
    },
    {
      key: "sources",
      header: m["my_redaction.log.column.where"](),
      hideBelow: "lg",
      render: (event) =>
        [...new Set(event.entities.flatMap((entity) => entity.sources))]
          .map((source) => (SOURCE_LABEL[source] ?? (() => source))())
          .join(", "),
    },
    {
      key: "outcome",
      header: m["my_redaction.log.column.outcome"](),
      render: (event) =>
        event.blocked ? (
          <Badge tone="danger">{m["my_redaction.log.blocked"]()}</Badge>
        ) : event.entities.length === 0 ? (
          <Badge>{m["my_redaction.log.seen"]()}</Badge>
        ) : (
          <Badge tone="ok">{m["my_redaction.log.hidden"]()}</Badge>
        ),
    },
    {
      key: "id",
      header: m["my_redaction.log.column.request"](),
      hideBelow: "lg",
      render: (event) => <code className={`${NOWRAP} text-small`}>{event.request_id}</code>,
    },
  ];

  return (
    <Card title={m["my_redaction.log.title"]()} flush>
      <div className="flex flex-col gap-4 p-5">
        <p className={`m-0 ${MUTED}`}>{m["my_redaction.log.intro"]()}</p>
        <Input
          label={m["my_redaction.log.filter"]()}
          value={requestId}
          onChange={(e) => {
            setRequestId(e.target.value);
            setOffset(0);
          }}
          placeholder={m["my_redaction.log.filter_placeholder"]()}
        />
        {log.isError && (
          <Notice tone="danger" title={m["my_redaction.error"]()}>
            {/* i18n: server text, the API's own error message. */}
            {log.error instanceof Error ? log.error.message : m["my_redaction.unknown_error"]()}
          </Notice>
        )}
      </div>
      {log.isPending ? (
        <Spinner label={m["my_redaction.loading"]()} />
      ) : (
        <>
          <Table
            stack
            columns={columns}
            rows={page?.items ?? []}
            rowKey={(event, index) => `${event.request_id}-${index}`}
            empty={m["my_redaction.log.empty"]()}
            caption={m["my_redaction.log.caption"]()}
          />
          <Pagination
            total={page?.total ?? 0}
            limit={PAGE_SIZE}
            offset={offset}
            onOffsetChange={setOffset}
            noun={m["my_redaction.log.noun"]()}
            busy={log.isFetching}
          />
        </>
      )}
    </Card>
  );
}

export function MyRedactions() {
  return (
    <div className={PAGE}>
      <PageHeader
        title={m["my_redaction.title"]()}
        subtitle={m["my_redaction.subtitle"]()}
      />
      <p className={`m-0 text-body ${MUTED}`}>
        {m["my_redaction.pipelines_note"]()} <Link to="/privacy">{m["my_redaction.pipelines_link"]()}</Link>
      </p>
      <Log />
    </div>
  );
}
