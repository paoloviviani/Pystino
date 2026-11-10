import { Badge, Button, Card, Notice, Select } from "@llmp/ui";
import { useState } from "react";

import { CODE, FIELD_LABEL, FORM, MUTED, TEXTAREA } from "../lib/layout";
import {
  KINDS,
  kindLabel,
  ruleModeLabel,
  usePrivacyOptions,
  usePrivacyTest,
  type Area,
} from "../lib/privacy";
import { entityLabel } from "../lib/entities";
import type { PrivacyKind, PrivacyTestInput, PrivacyTestResult } from "../lib/types";
import { m } from "../paraglide/messages.js";
import { Tip } from "./Tip";

/**
 * The test box (ADR 0113): the real path over facts you give it, no side effects.
 *
 * Shows every pipeline in scope and every rule of it (matched or not, with the items it
 * found), the reroute hops, who chose the extractor, and which rule claimed which piece of
 * text (the first rule to hide a piece decides how). An administrator picks
 * the user; a user's box is locked to themselves. The text and the file are never
 * stored or logged.
 */

function ownerBadge(owner: string) {
  return owner === "user" ? (
    <Badge tone="accent">{m["privacy.test.owner_user"]()}</Badge>
  ) : (
    <Badge tone="neutral">{m["privacy.test.owner_admin"]()}</Badge>
  );
}

const STEP_MARK: Record<string, string> = {
  matched: "✓",
  not_matched: "×",
  skipped_off: "–",
  pending: "…",
};

export function TestResult({ result }: { result: PrivacyTestResult }) {
  const tone =
    result.outcome === "block" ? "danger" : result.outcome === "pass" ? "ok" : "warn";
  const kind = result.kind as PrivacyKind;
  return (
    <div className="flex flex-col gap-4" data-testid="test-result">
      <Notice tone={tone === "ok" ? "info" : tone === "warn" ? "warn" : "danger"} title={result.outcome_headline}>
        {/* i18n: server text, the gateway's own sentence. */}
        {result.outcome_text}
      </Notice>

      <section className="flex flex-col gap-2" aria-label={m["privacy.test.in_scope"]()}>
        <h2 className="m-0 text-heading font-semibold">
          {m["privacy.test.in_scope"]()}{" "}
          <span className={`text-small ${MUTED}`}>{result.pipelines.length}</span>
        </h2>
        {result.pipelines.length === 0 && (
          <p className={`m-0 ${MUTED}`}>{m["privacy.test.none_in_scope"]()}</p>
        )}
        {result.pipelines.map((p) => (
          <Card key={p.id}>
            <div className="flex flex-col gap-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="flex flex-wrap items-center gap-2">
                  {/* i18n: server text, the pipeline's own name. */}
                  <strong>{p.name}</strong>
                  {ownerBadge(p.owner)}
                  {p.hop > 0 && <Badge tone="warn">{m["privacy.test.hop"]({ n: String(p.hop) })}</Badge>}
                </span>
                <span className={`text-small ${MUTED}`}>{p.scope.join(" · ")}</span>
              </div>
              <ul className="m-0 flex list-none flex-col gap-1 p-0 text-body">
                {p.steps.map((s) => (
                  <li key={s.rule_id} className="flex gap-2">
                    <span aria-hidden="true" className="w-4">
                      {STEP_MARK[s.result] ?? "·"}
                    </span>
                    <span className={s.result === "matched" ? "font-semibold" : MUTED}>
                      {s.name}
                      {s.result === "matched" && ` · ${m["privacy.test.fired_mark"]()}`}
                      {s.result === "pending" && ` · ${m["privacy.test.pending"]()}`}
                      {s.policies.length > 0 && (
                        <span className={`block text-small font-normal ${MUTED}`}>
                          {s.policies.join(" + ")} → {ruleModeLabel(s.mode)}
                        </span>
                      )}
                      {s.reasons.length > 0 && s.result === "matched" && (
                        <span className={`block text-small font-normal ${MUTED}`}>
                          {/* i18n: server text, the gateway's own reasons. */}
                          {s.reasons.join(" ")}
                        </span>
                      )}
                      {s.items.length > 0 && (
                        <ul
                          aria-label={m["privacy.test.items"]()}
                          className="m-0 mt-0.5 flex list-none flex-col gap-0.5 p-0 text-small font-normal"
                        >
                          {s.items.map((item, at) => (
                            <li key={at} className="flex flex-wrap items-baseline gap-x-2">
                              {/* i18n: server text, the policy's own name. */}
                              <strong>{item.policy}</strong>
                              <span className={MUTED}>
                                {item.group === "entity"
                                  ? m["privacy.test.group_entity"]()
                                  : m["privacy.test.group_pattern"]()}
                              </span>
                              <span>{entityLabel(item.type)}</span>
                              {item.group === "entity" && (
                                <span className={MUTED}>
                                  {item.engine}
                                  {item.score !== null && ` · ${item.score.toFixed(2)}`}
                                </span>
                              )}
                              {item.text !== null && <code className={CODE}>{item.text}</code>}
                            </li>
                          ))}
                        </ul>
                      )}
                      {s.degraded.length > 0 && (
                        <span className="block text-small font-normal text-ink">
                          {/* i18n: server text, the gateway's own sentence. */}
                          {s.degraded.join(" ")}
                        </span>
                      )}
                      {s.note && (
                        <span className={`block text-small font-normal ${MUTED}`}>
                          {/* i18n: server text, the gateway's own note. */}
                          {s.note}
                        </span>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          </Card>
        ))}
        {result.skipped.length > 0 && (
          <details>
            <summary className={`cursor-pointer text-small ${MUTED}`}>
              {m["privacy.test.skipped"]({ count: String(result.skipped.length) })}
            </summary>
            <ul className="m-0 mt-1 list-none p-0 text-small">
              {result.skipped.map((s) => (
                <li key={s.id}>
                  {/* i18n: server text, the pipeline's own name and the reason. */}
                  {s.name} <span className={MUTED}>({s.why})</span>
                </li>
              ))}
            </ul>
          </details>
        )}
      </section>

      {Object.keys(result.verdicts).length > 0 && (
        <section className="flex flex-col gap-1" aria-label={m["privacy.test.verdicts"]()}>
          <h2 className="m-0 flex items-center gap-2 text-heading font-semibold">
            {m["privacy.test.verdicts"]()}
            <Tip text={m["privacy.test.verdicts_tip"]()} />
          </h2>
          <ul className="m-0 flex list-none flex-col gap-0.5 p-0 text-body">
            {Object.entries(result.verdicts).map(([name, verdict]) => (
              <li key={name}>
                {/* i18n: server text, the classifier's own name. */}
                <strong>{name}</strong>{" "}
                <span className={MUTED}>
                  {verdict === true
                    ? m["privacy.test.verdict_yes"]()
                    : verdict === false
                      ? m["privacy.test.verdict_no"]()
                      : m["privacy.test.verdict_unknown"]()}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {result.hops.map((hop) => (
        <Notice key={hop.reader} tone="info" title={m["privacy.test.rerouted"]({ reader: hop.reader_name })}>
          {hop.added.length > 0
            ? m["privacy.test.hop_added"]({ names: hop.added.join(", ") })
            : m["privacy.test.hop_none"]()}
        </Notice>
      ))}

      {result.winner_rule && (
        <p className={`m-0 text-body ${MUTED}`}>
          {m["privacy.test.wins_by"]({ rule: `${result.winner_pipeline ?? ""} › ${result.winner_rule}` })}
        </p>
      )}

      {kind === "extract" && (
        <section className="flex flex-col gap-1" aria-label={m["privacy.test.reader"]()}>
          <h2 className="m-0 text-heading font-semibold">{m["privacy.test.reader"]()}</h2>
          <Card>
            <div className="flex flex-col gap-1 text-body">
              {result.reader ? (
                <span>
                  {m["privacy.test.reader_is"]({ reader: result.reader })}{" "}
                  <span className={MUTED}>{result.reader_by && m["privacy.test.chosen_by"]({ by: result.reader_by })}</span>
                </span>
              ) : (
                <span className={MUTED}>
                  {result.level === "block"
                    ? m["privacy.test.reader_refused"]()
                    : m["privacy.test.reader_default"]()}
                </span>
              )}
              {result.reader_overridden.map((o) => (
                <Badge key={o} tone="warn">
                  {/* i18n: server text, the gateway's own sentence. */}
                  {o}
                </Badge>
              ))}
            </div>
          </Card>
        </section>
      )}

      <section className="flex flex-col gap-2" aria-label={m["privacy.test.policies"]()}>
        <h2 className="m-0 flex items-center gap-2 text-heading font-semibold">
          {m["privacy.test.policies"]()}
          <Tip text={m["privacy.test.policies_tip"]()} />
        </h2>
        {result.level === "block" && result.blocked_by === null ? (
          <p className={`m-0 ${MUTED}`}>{m["privacy.test.not_applied_refused"]()}</p>
        ) : result.claims.length === 0 ? (
          <p className={`m-0 ${MUTED}`}>{m["privacy.test.no_policy"]()}</p>
        ) : (
          <table className="w-full text-left text-body">
            <thead>
              <tr className={`text-small ${MUTED}`}>
                <th scope="col">{m["privacy.test.col_type"]()}</th>
                <th scope="col">{m["privacy.test.col_strictest"]()}</th>
                <th scope="col">{m["privacy.test.col_from"]()}</th>
                <th scope="col">{m["privacy.test.col_found"]()}</th>
              </tr>
            </thead>
            <tbody>
              {result.claims.map((c, at) => (
                <tr key={at}>
                  <td>{entityLabel(c.type)}</td>
                  <td>{ruleModeLabel(c.mode)}</td>
                  <td>
                    {/* i18n: server text, the pipeline's and rule's own names. */}
                    {c.pipeline} › {c.rule}
                  </td>
                  <td>{c.text !== null ? <code className={CODE}>{c.text}</code> : "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      {result.degraded.length > 0 && (
        <Notice tone="warn" title={m["privacy.test.degraded"]()}>
          <ul className="m-0 list-disc pl-5">
            {result.degraded.map((d) => (
              // i18n: server text, the gateway's own sentence.
              <li key={d}>{d}</li>
            ))}
          </ul>
        </Notice>
      )}
    </div>
  );
}

export function PrivacyTestBox({ area }: { area: Area }) {
  const own = area === "me";
  const options = usePrivacyOptions(area);
  const test = usePrivacyTest(area);
  const [kind, setKind] = useState<PrivacyKind>("content");
  const [userId, setUserId] = useState("");
  const [modelId, setModelId] = useState("");
  const [filename, setFilename] = useState("");
  const [scan, setScan] = useState<"unknown" | "yes" | "no">("unknown");
  const [text, setText] = useState("");
  const opts = options.data;
  const models = (opts?.models ?? []).filter((x) =>
    kind === "extract"
      ? x.kind === "ocr"
      : x.kind === "chat" || x.kind === "embedding" || x.kind === "rerank",
  );
  const run = () => {
    const body: PrivacyTestInput = {
      kind,
      ...(userId && !own ? { user_id: userId } : {}),
      ...(modelId ? { model_id: modelId } : {}),
      ...(kind === "extract"
        ? {
            filename: filename || null,
            is_scan: scan === "unknown" ? null : scan === "yes",
          }
        : { text }),
    };
    test.mutate(body);
  };
  return (
    <div className="flex flex-col gap-4">
      <Card>
        <div className={FORM}>
          <div className="flex flex-wrap items-end gap-4">
            <div>
              <div className={FIELD_LABEL}>{m["privacy.edit.kind"]()}</div>
              <div role="group" aria-label={m["privacy.edit.kind"]()} className="mt-1 inline-flex gap-1">
                {KINDS.map((k) => (
                  <button
                    key={k}
                    type="button"
                    aria-pressed={kind === k}
                    className={`rounded-full border px-3 py-1 text-dense ${
                      kind === k ? "border-accent bg-sunken font-semibold" : "border-line"
                    }`}
                    onClick={() => {
                      setKind(k);
                      setModelId("");
                    }}
                  >
                    {kindLabel(k)}
                  </button>
                ))}
              </div>
            </div>
            <div className="min-w-[12rem] flex-1">
              {own ? (
                <div>
                  <div className={FIELD_LABEL}>{m["privacy.test.user_pick"]()}</div>
                  <Badge tone="neutral">{m["privacy.scope.me"]()}</Badge>
                </div>
              ) : (
                <Select
                  label={m["privacy.test.user_pick"]()}
                  value={userId}
                  onChange={(event) => setUserId(event.target.value)}
                >
                  <option value="">{m["privacy.test.no_user"]()}</option>
                  {(opts?.users ?? []).map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.name}
                    </option>
                  ))}
                </Select>
              )}
            </div>
            <div className="min-w-[12rem] flex-1">
              <Select
                label={m["privacy.test.target"]()}
                value={modelId}
                onChange={(event) => setModelId(event.target.value)}
              >
                <option value="">{m["privacy.test.no_target"]()}</option>
                {models.map((x) => (
                  <option key={x.id} value={x.id}>
                    {x.provider ?? ""} · {x.name}
                  </option>
                ))}
              </Select>
            </div>
          </div>
          {kind === "extract" ? (
            <div className="flex flex-wrap items-end gap-4">
              <div className="min-w-[14rem] flex-1">
                <label className={FIELD_LABEL} htmlFor="test-file-name">
                  {m["privacy.test.file_name"]()}
                </label>
                <input
                  id="test-file-name"
                  className="mt-1 w-full rounded-md border border-line bg-surface px-3 py-2 font-mono text-body"
                  placeholder={m["privacy.test.file_placeholder"]()}
                  value={filename}
                  onChange={(event) => setFilename(event.target.value)}
                />
              </div>
              <Select
                label={m["privacy.test.is_scan"]()}
                value={scan}
                onChange={(event) => setScan(event.target.value as "unknown" | "yes" | "no")}
              >
                <option value="unknown">{m["privacy.test.scan_unknown"]()}</option>
                <option value="yes">{m["privacy.test.scan_yes"]()}</option>
                <option value="no">{m["privacy.test.scan_no"]()}</option>
              </Select>
            </div>
          ) : (
            <div>
              <label className={FIELD_LABEL} htmlFor="test-text">
                {m["privacy.test.text"]()}
              </label>
              <textarea
                id="test-text"
                className={TEXTAREA}
                placeholder={m["privacy.test.text_placeholder"]()}
                value={text}
                onChange={(event) => setText(event.target.value)}
              />
            </div>
          )}
          <div className="flex flex-wrap items-center gap-3">
            <Button variant="primary" busy={test.isPending} onClick={run}>
              {m["privacy.test.run"]()}
            </Button>
            <span className={`text-small ${MUTED}`}>{m["privacy.test.real_path"]()}</span>
          </div>
        </div>
      </Card>
      {test.error && (
        <Notice tone="danger" title={m["privacy.test.error"]()}>
          {/* i18n: server text, the API's own message. */}
          {test.error instanceof Error ? test.error.message : m["privacy.unknown_error"]()}
        </Notice>
      )}
      {test.data && <TestResult result={test.data} />}
    </div>
  );
}
