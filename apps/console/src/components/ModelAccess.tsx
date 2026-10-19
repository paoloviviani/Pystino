import { Card, Input, Notice, Spinner } from "@llmp/ui";
import { useGroups, useModelAccess, useUserModelAccess, useUsers } from "../lib/admin";
import { formatNumber } from "../lib/format";
import { CHECK_ITEM, CHECK_LIST, MUTED } from "../lib/layout";
import { usePaginated } from "../lib/paging";
import { m } from "../paraglide/messages.js";

/**
 * Who may use this row — the visibility controls the model page has always had,
 * extracted so the Routing tab uses the same ones.
 *
 * Shared because an alias is a `models` row with the same `is_public`,
 * `group_model_access` and `user_model_access` (ADR 0101), and because two
 * copies of a checkbox grid are two implementations of "who may use this name"
 * that will drift. Nothing here knows whether the row is an alias: the grants
 * endpoints take a model id and that is the whole interface.
 *
 * Groups and individuals are unioned at request time, so they are shown
 * together rather than as alternatives — a person may reach a row through their
 * group or personally, and removing one leaves the other.
 */
export function ModelAccess({
  id,
  name,
  grantedToUsers,
  revokedNote,
  cardDescription,
}: {
  /** The `models` row id — an alias row is a `models` row, which is the whole
   *  reason the same grants endpoint serves both. */
  id: string;
  /** What a group's model list calls this name; the grant checkboxes match on
   *  it, so it has to be the row's own name and not a display name. */
  name: string;
  grantedToUsers: string[];
  revokedNote: () => string;
  cardDescription: () => string;
}) {
  const groups = useGroups();
  const access = useModelAccess();
  const userAccess = useUserModelAccess();
  const finder = usePaginated(20);

  // The directory is searched, not downloaded. Nothing is requested until
  // something is typed: granting one person access should not pull a page of
  // accounts nobody asked about.
  const users = useUsers(finder.page, finder.query.trim().length > 0);
  const matching = finder.query.trim() ? (users.data?.items ?? []) : [];
  const granted = grantedToUsers;

  return (
    <Card
      title={m["model_detail.access.card"]()}
      description={cardDescription()}
    >
      {access.error ? (
        <Notice tone="danger">
          {/* i18n: server text — the API's own error message. */}
          {access.error instanceof Error ? access.error.message : m["model_detail.unknown_error"]()}
        </Notice>
      ) : null}

      <p className={MUTED}>{m["model_detail.access.groups"]()}</p>
      {groups.isPending ? (
        <Spinner />
      ) : (
        <div className={CHECK_LIST}>
          {(groups.data?.items ?? []).map((group) => {
            const has = group.models.includes(name);
            return (
              <label key={group.id} className={CHECK_ITEM}>
                <input
                  type="checkbox"
                  checked={has}
                  disabled={access.isPending}
                  onChange={() =>
                    access.mutate({ groupId: group.id, modelId: id, grant: !has })
                  }
                />
                <span>{group.name}</span>
              </label>
            );
          })}
        </div>
      )}

      <p className={MUTED}>{m["model_detail.access.individuals"]()}</p>
      {granted.length > 0 && (
        <div className={CHECK_LIST}>
          {granted.map((email) => (
            <label key={email} className={CHECK_ITEM}>
              <input
                type="checkbox"
                checked
                disabled={userAccess.isPending}
                onChange={() => {
                  // Revoking needs the id, and the grant list carries only the
                  // label — so find the account by searching for it. Exact,
                  // because an email is unique.
                  const match = users.data?.items.find((entry) => entry.email === email);
                  if (match) {
                    userAccess.mutate({ userId: match.id, modelId: id, grant: false });
                  } else {
                    finder.setSearch(email);
                  }
                }}
              />
              <span>{email}</span>
            </label>
          ))}
        </div>
      )}
      <Input
        label={m["model_detail.access.person_search"]()}
        hideLabel
        value={finder.search}
        onChange={(event) => finder.setSearch(event.target.value)}
        placeholder={m["model_detail.access.person_placeholder"]()}
      />
      {userAccess.error ? (
        <Notice tone="danger">
          {/* i18n: server text — the API's own error message. */}
          {userAccess.error instanceof Error
            ? userAccess.error.message
            : m["model_detail.unknown_error"]()}
        </Notice>
      ) : null}
      <div className={CHECK_LIST}>
        {matching.map((user) => {
          const has = granted.includes(user.email ?? "");
          return (
            <label key={user.id} className={CHECK_ITEM}>
              <input
                type="checkbox"
                checked={has}
                disabled={userAccess.isPending}
                onChange={() =>
                  userAccess.mutate({ userId: user.id, modelId: id, grant: !has })
                }
              />
              <span>{user.email ?? user.display_name ?? user.subject}</span>
            </label>
          );
        })}
        {matching.length === 0 && (
          <span className={MUTED}>
            {!finder.query.trim()
              ? granted.length === 0
                ? m["model_detail.access.no_grants"]()
                : m["model_detail.access.type_to_find"]()
              : users.isFetching
                ? m["model_detail.access.searching"]()
                : m["model_detail.access.no_match"]()}
          </span>
        )}
        {users.data && users.data.total > matching.length && (
          <span className={MUTED}>
            {m["model_detail.access.showing"]({
              shown: formatNumber(matching.length),
              total: formatNumber(users.data.total),
            })}
          </span>
        )}
      </div>

      {/* Deactivating the model instead takes it away from everyone at once, and
          keeps historical spend attributable. */}
      <Notice tone="info">{revokedNote()}</Notice>
    </Card>
  );
}
