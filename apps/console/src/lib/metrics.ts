/**
 * What a quota metric is counted in, for the screens that print the unit.
 *
 * Shared rather than duplicated, because two screens print it — the admin
 * quota list and a person's own limits on the overview — and a metric named
 * one way in one place and another way beside it is how an operator ends up
 * believing they are two different ceilings.
 *
 * Only metrics whose column name is not a readable unit need an entry;
 * `unitFor` falls back to the metric itself, so a metric added to the API
 * without being added here reads awkwardly rather than disappearing.
 */
const METRIC_UNITS: Record<string, string> = {
  // A count of calls to our own web-search backends. Worth spelling out on
  // every screen it appears on that this bounds *volume, not spend*: the
  // backends' per-request prices differ by more than ten times between tiers,
  // so a search budget says nothing about what the searches cost.
  own_search_requests: "web searches",
};

export function unitFor(metric: string): string {
  return METRIC_UNITS[metric] ?? metric;
}
