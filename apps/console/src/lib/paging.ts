import { useEffect, useRef, useState } from "react";

/**
 * Client-side half of the API's offset pagination.
 *
 * Two things here exist because of bugs that are easy to write and hard to see:
 * changing a filter must reset the window, and a search box must not issue a
 * request per keystroke.
 */

/** The envelope every listing endpoint returns. */
export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface PageQuery {
  limit?: number;
  offset?: number;
  q?: string;
}

/** Matches the gateway's ceiling; asking for more is a 400, not a clamp. */
export const MAX_LIMIT = 200;

/** Query-string form of a page request, stable enough to use as a cache key. */
export function pageParams(query: PageQuery, extra: Record<string, string> = {}): string {
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 50));
  params.set("offset", String(query.offset ?? 0));
  if (query.q?.trim()) params.set("q", query.q.trim());
  for (const [key, value] of Object.entries(extra)) {
    if (value) params.set(key, value);
  }
  return params.toString();
}

/**
 * An empty page, for rendering before the first response arrives.
 *
 * `total: 0` rather than `undefined` so a screen can read `.total` without a
 * guard; the pagination bar hides itself at zero anyway.
 */
export function emptyPage<T>(limit = 50): Page<T> {
  return { items: [], total: 0, limit, offset: 0 };
}

export interface Paginated {
  limit: number;
  offset: number;
  setOffset: (offset: number) => void;
  /** What the user has typed, for the input's value. */
  search: string;
  setSearch: (search: string) => void;
  /** What is actually sent, trailing the input by `delay`. */
  query: string;
  /** The page request to hand to a query hook. */
  page: PageQuery;
}

/**
 * Offset, a debounced search term, and the rule that ties them together.
 *
 * Typing into the search box resets the offset to zero. Without that, an
 * operator on page three who then searches gets an empty table and reasonably
 * concludes there are no matches — when in fact the matches are all on page
 * one. It is the single most common bug in a paginated table with a filter.
 */
export function usePaginated(limit = 50, delay = 250): Paginated {
  const [offset, setOffset] = useState(0);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const first = useRef(true);

  useEffect(() => {
    // No delay on the initial mount: waiting 250ms to fetch an empty search is
    // a page that renders slower than it needs to.
    if (first.current) {
      first.current = false;
      return;
    }
    const timer = setTimeout(() => {
      setQuery(search);
      setOffset(0);
    }, delay);
    return () => clearTimeout(timer);
  }, [search, delay]);

  return {
    limit,
    offset,
    setOffset,
    search,
    setSearch,
    query,
    page: { limit, offset, q: query },
  };
}
