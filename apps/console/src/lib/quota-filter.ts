import { m } from "../paraglide/messages.js";

/**
 * "on Anthropic" / "on claude-opus (Anthropic)" for a quota rule limited to a
 * provider or a model, or null for a rule on all usage. The names come from the
 * API; this only phrases them.
 */
export function quotaFilter(rule: {
  provider_name?: string | null;
  model_name?: string | null;
}): string | null {
  if (rule.model_name && rule.provider_name) {
    return m["quota_filter.model_of"]({ model: rule.model_name, provider: rule.provider_name });
  }
  if (rule.model_name) return m["quota_filter.model"]({ model: rule.model_name });
  if (rule.provider_name) return m["quota_filter.provider"]({ provider: rule.provider_name });
  return null;
}
