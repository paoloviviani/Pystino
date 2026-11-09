import { Badge, Button, Card, Notice, Select } from "@llmp/ui";
import { useState } from "react";

import { CODE, FIELD_LABEL, FORM, MUTED, TEXTAREA } from "../lib/layout";
import {
  KINDS,
  baseLevel,
  kindLabel,
  levelLabel,
  usePrivacyOptions,
  usePrivacyTest,
  type Area,
} from "../lib/privacy";
import { entityLabel, modeLabel } from "../lib/entities";
import type { PrivacyKind, PrivacyTestInput, PrivacyTestResult } from "../lib/types";
import { m } from "../paraglide/messages.js";
import { Tip } from "./Tip";

/**
 * The test box (ADR 0113): the real path over facts you give it, no side effects.
 *
 * Shows every pipeline in scope with the rule that fired, the reroute hops, the
 * ladder with its winner, who chose the reader and the engine, and the policies
 * added up with the strictest mode and where it came from. An administrator picks
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
  fired: "✓",
  observed: "○",
  not_matched: "×",
  skipped_off: "–",
  not_reached: "–",
  pending: "…",
};

export function TestResult({ result }: { result: PrivacyTestResult }) {
  const tone =
    result.outcome === "refuse" ? "danger" : result.outcome === "pass" ? "ok" : "warn";
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
                    <span className={s.result === "fired" ? "font-semibold" : MUTED}>
                      {s.is_otherwise ? m["privacy.edit.else"]() : s.name}
                      {s.result === "fired" && ` · ${m["privacy.test.fired_mark"]()}`}
                      {s.result === "observed" && ` · ${m["privacy.test.observed_mark"]()}`}
                      {s.result === "not_reached" && ` · ${m["privacy.test.not_reached"]()}`}
                      {s.result === "pending" && ` · ${m["privacy.test.pending"]()}`}
                      {s.reasons.length > 0 && s.result === "fired" && (
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
              <p className={`m-0 text-small ${MUTED}`}>
                {m["privacy.test.result"]()} <strong>{p.action_text}</strong>
              </p>
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

      <section className="flex flex-col gap-2" aria-label={m["privacy.test.ladder"]()}>
        <h2 className="m-0 flex items-center gap-2 text-heading font-semibold">
          {m["privacy.test.ladder"]()}
          <Tip text={m["privacy.test.ladder_tip"]()} />
        </h2>
        <div className="flex flex-wrap items-stretch gap-2">
          {result.ladder.map((l, index) => (
            <div key={l.level} className="flex items-center gap-2">
              {index > 0 && (
                <span aria-hidden="true" className={MUTED}>
                  &lt;
                </span>
              )}
              <div
                className={`rounded-lg border px-3 py-2 ${
                  l.winning
                    ? l.level === "refuse"
                      ? "border-danger bg-sunken"
                      : "border-accent bg-sunken"
                    : "border-line"
                }`}
                aria-current={l.winning ? "true" : undefined}
              >
                <strong>{l.label}</strong>
                <span className={`block text-small ${MUTED}`}>
                  {l.winning
                    ? result.winner_rule
                      ? m["privacy.test.wins_by"]({
                          rule: `${result.winner_pipeline ?? ""} › ${result.winner_rule}`,
                        })
                      : m["privacy.test.wins_base"]()
                    : l.asked_by.length > 0
                      ? m["privacy.test.asked_by"]({ names: l.asked_by.join(", ") })
                      : ""}
                </span>
              </div>
            </div>
          ))}
        </div>
      </section>

      {kind === "read" && (
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
                  {result.level === "refuse"
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
              {result.engine && (
                <span>
                  {m["privacy.test.engine_is"]({ engine: result.engine })}{" "}
                  <span className={MUTED}>{result.engine_by && m["privacy.test.chosen_by"]({ by: result.engine_by })}</span>
                </span>
              )}
            </div>
          </Card>
        </section>
      )}

      <section className="flex flex-col gap-2" aria-label={m["privacy.test.policies"]()}>
        <h2 className="m-0 flex items-center gap-2 text-heading font-semibold">
          {m["privacy.test.policies"]()}
          <Tip text={m["privacy.test.policies_tip"]()} />
        </h2>
        {result.level === "refuse" ? (
          <p className={`m-0 ${MUTED}`}>{m["privacy.test.not_applied_refused"]()}</p>
        ) : result.policies.length === 0 ? (
          <p className={`m-0 ${MUTED}`}>{m["privacy.test.no_policy"]()}</p>
        ) : (
          <>
            <p className={`m-0 text-small ${MUTED}`}>
              {m["privacy.test.policies_from"]({ names: result.policies.join(" + ") })}
            </p>
            <table className="w-full text-left text-body">
              <thead>
                <tr className={`text-small ${MUTED}`}>
                  <th scope="col">{m["privacy.test.col_type"]()}</th>
                  <th scope="col">{m["privacy.test.col_strictest"]()}</th>
                  <th scope="col">{m["privacy.test.col_from"]()}</th>
                  {result.policy_types.some((t) => t.found !== null) && (
                    <th scope="col">{m["privacy.test.col_found"]()}</th>
                  )}
                </tr>
              </thead>
              <tbody>
                {result.policy_types.map((t) => (
                  <tr key={t.entity_type}>
                    <td>{entityLabel(t.entity_type)}</td>
                    <td>{modeLabel(t.mode as never)}</td>
                    <td>
                      {t.source}
                      {t.per_policy.length > 1 && (
                        <span className={`text-small ${MUTED}`}> ({t.per_policy.join("; ")})</span>
                      )}
                    </td>
                    {result.policy_types.some((x) => x.found !== null) && (
                      <td>{t.found === null ? "" : t.found ? <strong>{m["privacy.test.found"]()}</strong> : "–"}</td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </>
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
      <p className={`m-0 text-small ${MUTED}`}>
        {m["privacy.test.base_level"]({ level: levelLabel(baseLevel(kind)) })}
      </p>
    </div>
  );
}

export function PrivacyTestBox({ area }: { area: Area }) {
  const own = area === "me";
  const options = usePrivacyOptions(area);
  const test = usePrivacyTest(area);
  const [kind, setKind] = useState<PrivacyKind>("embed");
  const [userId, setUserId] = useState("");
  const [modelId, setModelId] = useState("");
  const [filename, setFilename] = useState("");
  const [scan, setScan] = useState<"unknown" | "yes" | "no">("unknown");
  const [text, setText] = useState("");
  const opts = options.data;
  const models = (opts?.models ?? []).filter((x) =>
    kind === "read"
      ? x.kind === "ocr"
      : kind === "embed"
        ? x.kind === "embedding" || x.kind === "rerank"
        : x.kind === "chat",
  );
  const run = () => {
    const body: PrivacyTestInput = {
      kind,
      ...(userId && !own ? { user_id: userId } : {}),
      ...(modelId ? { model_id: modelId } : {}),
      ...(kind === "read"
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
          {kind === "read" ? (
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
