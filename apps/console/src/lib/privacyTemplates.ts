import type { PrivacyAction, PrivacyCondition, PrivacyKind } from "./types";
import { m } from "../paraglide/messages.js";

/**
 * "Start from a goal": five rules an administrator is likely to want, in the
 * words they would use, that fill the builder and nothing else. A template is
 * only a draft; saving still goes through the same checks as any rule.
 */

export interface PrivacyTemplate {
  id: string;
  kind: PrivacyKind;
  title: () => string;
  explain: () => string;
  name: () => string;
  conditions: PrivacyCondition[];
  action: PrivacyAction;
  observeOnly?: boolean;
}

export const PRIVACY_TEMPLATES: PrivacyTemplate[] = [
  {
    id: "medical-files-here",
    kind: "read",
    title: m["privacy.template.medical_files.title"],
    explain: m["privacy.template.medical_files.explain"],
    name: m["privacy.template.medical_files.name"],
    conditions: [
      { key: "filetype", values: ["pdf"], negate: false },
      { key: "filename", values: ["referto*"], negate: false },
    ],
    action: { type: "reader", reader: "ocr" },
  },
  {
    id: "scans-here",
    kind: "read",
    title: m["privacy.template.scans.title"],
    explain: m["privacy.template.scans.explain"],
    name: m["privacy.template.scans.name"],
    conditions: [{ key: "filetype", values: ["scan"], negate: false }],
    action: { type: "reader", reader: "ocr" },
  },
  {
    id: "health-not-embedded-outside",
    kind: "embed",
    title: m["privacy.template.health_refuse.title"],
    explain: m["privacy.template.health_refuse.explain"],
    name: m["privacy.template.health_refuse.name"],
    conditions: [
      { key: "verdict", values: ["health"], negate: false, decide: "patterns" },
      { key: "target", values: ["cloud"], negate: false },
    ],
    action: { type: "refuse" },
  },
  {
    id: "health-local-embeddings",
    kind: "embed",
    title: m["privacy.template.health_local.title"],
    explain: m["privacy.template.health_local.explain"],
    name: m["privacy.template.health_local.name"],
    conditions: [
      { key: "verdict", values: ["health"], negate: false, decide: "patterns" },
      { key: "target", values: ["cloud"], negate: false },
    ],
    action: { type: "local" },
  },
  {
    id: "try-first",
    kind: "embed",
    title: m["privacy.template.try_first.title"],
    explain: m["privacy.template.try_first.explain"],
    name: m["privacy.template.try_first.name"],
    conditions: [{ key: "verdict", values: ["ids"], negate: false, decide: "patterns" }],
    action: { type: "refuse" },
    observeOnly: true,
  },
];
