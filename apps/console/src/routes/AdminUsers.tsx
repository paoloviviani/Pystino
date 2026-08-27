import { Badge, Button, Card, Input, Notice, Pagination, Spinner, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useGroups, useUpdateUser, useUsers } from "../lib/admin";
import { usePaginated } from "../lib/paging";
import type { AdminGroup, AdminUser } from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import styles from "./Admin.module.css";

export function AdminUsers() {
  // Searched and paged on the server. It used to filter in the browser over
  // whatever the endpoint had returned, which reads the same until the
  // organisation outgrows one response — at which point the box quietly
  // searches the first page and reports nothing found.
  const paged = usePaginated();
  const users = useUsers(paged.page);
  const groupPaging = usePaginated();
  const groups = useGroups(groupPaging.page);
  const update = useUpdateUser();

  const page = users.data;
  const rows = page?.items ?? [];

  const columns: Column<AdminUser>[] = [
    {
      key: "who",
      header: "User",
      render: (user) => (
        <>
          <div>{user.display_name || user.email || user.subject}</div>
          <div className={styles.muted}>{user.email ?? user.subject}</div>
          {/* Identity is (issuer, subject), not email. Two rows can therefore
              share a name and an address and still be different accounts —
              which is exactly what happens if the OIDC issuer URL ever changes.
              Showing the issuer makes that legible instead of looking like a
              duplicate nobody can explain. */}
          <div className={`${styles.muted} ${styles.code}`}>{user.issuer}</div>
        </>
      ),
    },
    {
      key: "groups",
      header: "Groups",
      render: (user) =>
        user.groups.length === 0 ? (
          <span className={styles.muted}>none</span>
        ) : (
          <div className={styles.chips}>
            {user.groups.map((group) => (
              <Badge key={group}>{group}</Badge>
            ))}
          </div>
        ),
    },
    {
      key: "billing",
      header: "Bills to",
      render: (user) => user.default_billing_group ?? <span className={styles.muted}>unset</span>,
    },
    {
      key: "keys",
      header: "Keys",
      numeric: true,
      render: (user) => user.active_key_count.toLocaleString(),
    },
    {
      key: "status",
      header: "Status",
      render: (user) => (
        <div className={styles.chips}>
          {user.is_admin && <Badge tone="accent">Admin</Badge>}
          {user.is_active ? <Badge tone="ok">Active</Badge> : <Badge tone="danger">Disabled</Badge>}
        </div>
      ),
    },
    {
      key: "seen",
      header: "Last login",
      render: (user) =>
        user.last_login_at ? formatDate(user.last_login_at) : <span className={styles.muted}>never</span>,
    },
    {
      key: "actions",
      header: "",
      render: (user) => (
        <div className={styles.rowActions}>
          <Button
            busy={update.isPending && update.variables?.id === user.id}
            onClick={() => update.mutate({ id: user.id, is_active: !user.is_active })}
          >
            {user.is_active ? "Disable" : "Enable"}
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className={styles.page}>
      <PageHeader
        title="Users"
        subtitle="Provisioned by the identity provider on first login. Group membership is
          not editable here."
      />

      {update.error ? (
        <Notice tone="danger" title="Could not update the user">
          {update.error instanceof Error ? update.error.message : "Unknown error."}
        </Notice>
      ) : null}

      <Card>
        <div className={styles.searchRow}>
          <div className={styles.grow}>
            <Input
              label="Search"
              value={paged.search}
              onChange={(event) => paged.setSearch(event.target.value)}
              placeholder="email, name or identity provider subject"
              hint={
                page
                  ? `${page.total.toLocaleString()} matching`
                  : "Searches every account, not just this page."
              }
            />
          </div>
        </div>
      </Card>

      <Card flush>
        {users.isPending ? (
          <Spinner label="Loading users" />
        ) : users.error ? (
          <Notice tone="danger" title="Could not load users">
            {users.error instanceof Error ? users.error.message : "Unknown error."}
          </Notice>
        ) : (
          <>
            <Table
              columns={columns}
              rows={rows}
              rowKey={(user) => user.id}
              empty={paged.query ? "No user matches that." : "No users yet."}
              caption="Users, their groups and their keys."
            />
            <Pagination
              total={page?.total ?? 0}
              limit={paged.limit}
              offset={paged.offset}
              onOffsetChange={paged.setOffset}
              noun="users"
              busy={users.isFetching}
            />
          </>
        )}
      </Card>

      <GroupsCard
        groups={groups.data?.items ?? []}
        total={groups.data?.total ?? 0}
        loading={groups.isPending}
        paging={groupPaging}
        busy={groups.isFetching}
      />
    </div>
  );
}

function GroupsCard({
  groups,
  total,
  loading,
  paging,
  busy,
}: {
  groups: AdminGroup[];
  total: number;
  loading: boolean;
  paging: ReturnType<typeof usePaginated>;
  busy: boolean;
}) {
  const columns: Column<AdminGroup>[] = [
    { key: "name", header: "Group", render: (group) => group.name },
    {
      key: "source",
      header: "Source",
      // A group the IdP owns cannot be meaningfully edited here, so where it
      // came from is worth showing beside it.
      render: (group) => <Badge>{group.source}</Badge>,
    },
    {
      key: "members",
      header: "Members",
      numeric: true,
      render: (group) => group.member_count.toLocaleString(),
    },
    {
      key: "models",
      header: "Models",
      render: (group) =>
        group.models.length === 0 ? (
          <span className={styles.muted}>none granted</span>
        ) : (
          <div className={styles.chips}>
            {group.models.map((model) => (
              <Badge key={model}>{model}</Badge>
            ))}
          </div>
        ),
    },
  ];

  return (
    <Card title="Groups" flush description="Model access is granted per group, on the Models page.">
      {loading ? (
        <Spinner />
      ) : (
        <>
          <Table columns={columns} rows={groups} rowKey={(group) => group.id} empty="No groups." />
          <Pagination
            total={total}
            limit={paging.limit}
            offset={paging.offset}
            onOffsetChange={paging.setOffset}
            noun="groups"
            busy={busy}
          />
        </>
      )}
    </Card>
  );
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}
