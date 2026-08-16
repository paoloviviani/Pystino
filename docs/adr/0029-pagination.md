# 0029 — Pagination on the management listings

- Date: 2026-08-16
- Status: accepted

## Context

Every listing on the management API returned the whole table. That was noted as
a gap when the reporting API was designed and again when the console was built,
and left as it was both times, because a demo deployment has eleven users and a
handful of models.

Two things make it worth closing now rather than later. The first is size: a
provider catalogue import can add several hundred models in one click, and the
foundation has a few hundred people who will each be provisioned on first login.
The second is that the console's users screen had grown a search box that
filtered in the browser, over whatever the endpoint had returned. That reads
identically to a real search right up to the point where the organisation
outgrows one response — and then it searches the first page, finds nothing, and
says so. A filter that quietly stops being a filter is worse than no filter.

## Decision

### 1. Offset, not a cursor

`?limit=&offset=`, and a response envelope:

```json
{ "items": [...], "total": 812, "limit": 50, "offset": 0 }
```

A cursor is the better answer for an append-only feed consumed forwards — it is
stable under concurrent inserts and cheap at any depth. It cannot express "51–100
of 812" and it cannot jump to the last page, and those are what an administrative
table needs. An operator looking at a page of users has to be able to tell
whether that is all of them.

The costs of offset are real and accepted. A deep offset makes the database walk
the rows it skips. A row inserted while somebody pages shifts the window, so an
entry can be seen twice or missed. Both are tolerable for a table an operator
reads; neither would be tolerable for billing, which is why nothing in the ledger
is read through this.

### 2. `total` is what the filter matched

Not what was returned. It is the only thing on the page that distinguishes a
complete listing from a truncated one, and a client can decide there is more
without a second request. It costs one extra `count()` per listing, in the same
transaction as the page.

### 3. Row listings paginate; aggregations do not

`/api/admin/users` is a listing, and truncates.
`/api/admin/reports/usage` is a sum over a billing period, and does not — a
truncated sum is a wrong number that looks exactly like a right one, and the
report exists to be reconciled against an invoice. The CSV export is the same
report and is likewise complete.

`GET /v1/models` also keeps its shape: it is OpenAI's schema, not ours, and it
is already bounded by what a single caller has been granted.

Everything else paginates, including the small ones. A client that has to know
which endpoints truncate will eventually get it wrong; one shape means one
helper. `/api/admin/providers` will return three rows in an envelope and that
costs nothing.

### 4. Out of bounds is refused, not clamped

`limit=0`, `limit=201` and `offset=-1` are 400s. Silently substituting a
different window is how a caller ends up treating a truncated list as complete.
The ceiling is 200; without one, a caller can ask for everything and the
endpoint that was paginated to survive a large organisation goes back to loading
it all.

### 5. Filtering moves to the server with the pagination

`/api/admin/users?q=`, `/api/admin/models?q=`, `/api/admin/groups?q=`, plus
`is_active` on users and `provider_id` on models. Paginating without this would
be a regression: the console had a search box, and narrowing fifty already-loaded
rows is not a search.

The match is a case-insensitive substring, over the columns an operator actually
half-remembers — a model's name and its upstream name, a person's email, display
name and IdP subject. Substring rather than prefix because a catalogue full of
`meta-llama/Llama-3.3-70B-Instruct` is unsearchable by prefix. `%` and `_` in the
term are escaped, or a search for `gpt_4` would silently also match `gpt-4`.

The subject is searchable because for an account whose identity provider
releases no email claim it is the only handle there is.

## Consequences

Every listing response changed shape. This is a breaking change to the
management API, taken now while the console is the only client. The `/v1`
surface is untouched.

Two lookups had to follow the page rather than the table. Rendering a page of
models used to read every group grant and every personal grant; rendering a page
of users read every API key and every group. Both are now restricted to the ids
on the page — invisible at eleven users, quadratic at a thousand.

`PATCH /api/admin/users/{id}` used to answer by re-reading the listing and
picking its row out of it. That stops working the moment the listing is a page,
because the user just edited is usually not on the first one. It builds one
response directly now.

The console gained a `Pagination` primitive and a `usePaginated` hook. The hook
carries one rule worth naming: typing in a search box resets the offset to zero.
Without it an operator on page three who searches gets an empty table and
reasonably concludes there are no matches, when every match is on page one.

Two listings are windowed in Python rather than in SQL — price history, whose
rows arrive with the model, and quota rules, which are loaded whole anyway to
read their live counter values. The database work is not saved; the response
shape is the same, which is the point.

## Alternatives considered

**`X-Total-Count` header, keeping the bare arrays.** Non-breaking, and every
client that ignores the header silently keeps its truncation bug. The whole
reason for the change is that truncation should be impossible to miss.

**Paginate only the endpoints that need it.** Then "does this one truncate?" is
a question with a different answer per endpoint, and the answer changes as the
deployment grows. The line was going to be redrawn every time somebody added a
listing.

**Keyset pagination on the audit-style listings** (quota resets), offset
elsewhere. Correct on the merits — resets are append-only and time-ordered — and
rejected because two pagination styles in one API is a cost paid by every reader
forever, to save a table nobody pages deeply into.
