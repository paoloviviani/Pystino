import { Badge, Button, Card, Input, Notice, Select } from "@llmp/ui";
import { useState } from "react";
import { useNavigate } from "react-router";

import { PageHeader } from "../components/PageHeader";
import { MUTED, PAGE } from "../lib/layout";
import { KINDS, kindLabel, usePrivacyTest } from "../lib/privacy";
import type { PrivacyKind, PrivacyTestResult } from "../lib/types";
import { m } from "../paraglide/messages.js";

/**
 * Test it: give the facts of a request, see which rule fires and why the ones
 * above it did not.
 *
 * Text and files are judged in memory by the gateway and never stored; the
 * answer carries labels and rule names, not the content. The result is a
 * compact card; the trail, what was seen and the verdicts are behind
 * disclosures.
 */

function toBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] ?? "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

function StepResult({ result }: { result: string }) {
  switch (result) {
    case "fired":
      return <Badge tone="ok">{m["privacy.step.fired"]()}</Badge>;
    case "observed":
      return <Badge tone="accent">{m["privacy.step.observed"]()}</Badge>;
    case "skipped_off":
      return <Badge>{m["privacy.step.off"]()}</Badge>;
    case "not_reached":
      return <Badge>{m["privacy.step.not_reached"]()}</Badge>;
    case "pending":
      return <Badge tone="warn">{m["privacy.step.pending"]()}</Badge>;
    default:
      return <Badge>{m["privacy.step.no_match"]()}</Badge>;
  }
}

function Result({ result }: { result: PrivacyTestResult }) {
  return (
    <Card title={m["privacy.test.result"]()}>
      <div className="flex flex-col gap-3">
        <p className="m-0 text-body font-medium">{result.outcome_text}</p>
        {result.fired && (
          <p className="m-0 text-body">
            {m["privacy.test.fired"]({ name: result.fired.name })}
          </p>
        )}
        {result.observed.length > 0 && (
          <p className={`m-0 text-small ${MUTED}`}>
            {m["privacy.test.observed"]({ names: result.observed.map((r) => r.name).join(", ") })}
          </p>
        )}
        {result.degraded.map((line) => (
          <Notice key={line} tone="warn">
            {line}
          </Notice>
        ))}
        {result.fallbacks.length > 0 && (
          <Notice tone="warn">
            {m["privacy.test.fallback"]({ names: result.fallbacks.join(", ") })}
          </Notice>
        )}
        {result.extractor && (
          <p className={`m-0 text-small ${MUTED}`}>
            {m["privacy.test.extractor"]({ name: result.extractor })}
            {result.fallback_reason
              ? ` ${m["privacy.test.extractor_fallback"]({ reason: result.fallback_reason })}`
              : ""}
          </p>
        )}
        <details>
          <summary className="cursor-pointer text-small font-medium">
            {m["privacy.test.why"]()}
          </summary>
          <ol className="mt-2 mb-0 flex list-none flex-col gap-2 p-0">
            {result.steps.map((step) => (
              <li key={step.rule_id} className="flex flex-col gap-1">
                <span className="flex items-center gap-2 text-body">
                  <StepResult result={step.result} /> {step.name}
                </span>
                {step.reasons.map((reason) => (
                  <span key={reason} className={`pl-2 text-small ${MUTED}`}>
                    {reason}
                  </span>
                ))}
              </li>
            ))}
            {result.steps.length === 0 && (
              <li className={`text-small ${MUTED}`}>{m["privacy.test.no_rules"]()}</li>
            )}
          </ol>
        </details>
        <details>
          <summary className="cursor-pointer text-small font-medium">
            {m["privacy.test.saw"]()}
          </summary>
          <dl className="mt-2 mb-0 grid gap-x-4 gap-y-1 text-small [grid-template-columns:auto_1fr]">
            {Object.entries({ ...result.facts, ...result.verdicts })
              .filter(([, value]) => value !== null && value !== undefined && value !== "")
              .map(([key, value]) => (
                <div key={key} className="contents">
                  <dt className={MUTED}>{key}</dt>
                  <dd className="m-0">{Array.isArray(value) ? value.join(", ") : String(value)}</dd>
                </div>
              ))}
          </dl>
        </details>
      </div>
    </Card>
  );
}

export function AdminPrivacyTest() {
  const navigate = useNavigate();
  const test = usePrivacyTest();
  const [kind, setKind] = useState<PrivacyKind>("read");
  const [filename, setFilename] = useState("");
  const [mime, setMime] = useState("");
  const [scan, setScan] = useState<"unknown" | "yes" | "no">("unknown");
  const [file, setFile] = useState<File | null>(null);
  const [text, setText] = useState("");
  const [user, setUser] = useState("");
  const [groups, setGroups] = useState("");
  const [target, setTarget] = useState<"unknown" | "cloud" | "local">("cloud");
  const [provider, setProvider] = useState("");

  const run = async () => {
    const who = {
      ...(user.trim() ? { user: user.trim() } : {}),
      groups: groups
        .split(",")
        .map((g) => g.trim())
        .filter(Boolean),
    };
    if (kind === "read") {
      test.mutate({
        kind,
        ...who,
        ...(filename.trim() ? { filename: filename.trim() } : {}),
        ...(mime.trim() ? { mime: mime.trim() } : {}),
        ...(scan !== "unknown" ? { is_scan: scan === "yes" } : {}),
        ...(file ? { file_base64: await toBase64(file) } : {}),
      });
    } else {
      test.mutate({
        kind,
        ...who,
        ...(text ? { text } : {}),
        ...(target !== "unknown" ? { target_local: target === "local" } : {}),
        ...(provider.trim() ? { target_provider: provider.trim() } : {}),
      });
    }
  };

  return (
    <div className={PAGE}>
      <PageHeader
        title={m["privacy.test.title"]()}
        subtitle={m["privacy.test.subtitle"]()}
        actions={
          <Button type="button" onClick={() => navigate("/admin/privacy")}>
            {m["privacy.rule.back"]()}
          </Button>
        }
      />
      <Card>
        <div className="flex flex-col gap-4">
          <Select
            label={m["privacy.test.kind"]()}
            value={kind}
            onChange={(event) => {
              setKind(event.target.value as PrivacyKind);
              test.reset();
            }}
          >
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {kindLabel(k)}
              </option>
            ))}
          </Select>
          {kind === "read" ? (
            <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(12rem,1fr))]">
              <Input
                label={m["privacy.test.filename"]()}
                value={filename}
                onChange={(event) => setFilename(event.target.value)}
              />
              <Input
                label={m["privacy.test.mime"]()}
                value={mime}
                placeholder="application/pdf" // i18n: literal
                onChange={(event) => setMime(event.target.value)}
              />
              <Select
                label={m["privacy.test.scan"]()}
                value={scan}
                onChange={(event) => setScan(event.target.value as typeof scan)}
              >
                <option value="unknown">{m["privacy.test.scan_unknown"]()}</option>
                <option value="yes">{m["privacy.test.scan_yes"]()}</option>
                <option value="no">{m["privacy.test.scan_no"]()}</option>
              </Select>
              <div className="flex flex-col gap-1">
                <label className="text-small font-medium" htmlFor="privacy-test-file">
                  {m["privacy.test.file"]()}
                </label>
                <input
                  id="privacy-test-file"
                  type="file"
                  onChange={(event) => setFile(event.target.files?.[0] ?? null)}
                />
                <p className={`m-0 text-small ${MUTED}`}>{m["privacy.test.file_hint"]()}</p>
              </div>
            </div>
          ) : (
            <div className="flex flex-col gap-4">
              <div className="flex flex-col gap-1">
                <label className="text-small font-medium" htmlFor="privacy-test-text">
                  {m["privacy.test.text"]()}
                </label>
                <textarea
                  id="privacy-test-text"
                  className="min-h-28 w-full rounded-lg border border-line bg-surface p-2 text-body"
                  value={text}
                  onChange={(event) => setText(event.target.value)}
                />
                <p className={`m-0 text-small ${MUTED}`}>{m["privacy.test.text_hint"]()}</p>
              </div>
              <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(12rem,1fr))]">
                <Select
                  label={m["privacy.test.target"]()}
                  value={target}
                  onChange={(event) => setTarget(event.target.value as typeof target)}
                >
                  <option value="cloud">{m["privacy.target.cloud"]()}</option>
                  <option value="local">{m["privacy.target.local"]()}</option>
                  <option value="unknown">{m["privacy.test.scan_unknown"]()}</option>
                </Select>
                <Input
                  label={m["privacy.test.provider"]()}
                  value={provider}
                  onChange={(event) => setProvider(event.target.value)}
                />
              </div>
            </div>
          )}
          <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(12rem,1fr))]">
            <Input
              label={m["privacy.test.user"]()}
              value={user}
              onChange={(event) => setUser(event.target.value)}
            />
            <Input
              label={m["privacy.test.groups"]()}
              value={groups}
              hint={m["privacy.test.groups_hint"]()}
              onChange={(event) => setGroups(event.target.value)}
            />
          </div>
          <div>
            <Button type="button" variant="primary" disabled={test.isPending} onClick={run}>
              {m["privacy.test.run"]()}
            </Button>
          </div>
        </div>
      </Card>
      {test.error && (
        <Notice tone="danger" title={m["privacy.test.error"]()}>
          {test.error instanceof Error ? test.error.message : m["privacy.error.unknown"]()}
        </Notice>
      )}
      {test.data && <Result result={test.data} />}
    </div>
  );
}
