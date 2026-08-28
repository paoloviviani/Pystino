import { Input, Select } from "@llmp/ui";
import { useGroups, useModels, useProviders, useUsers } from "../lib/admin";
import type { RedactionScope } from "../lib/types";

export interface SubjectPickerProps {
  scope: RedactionScope;
  value: string;
  onChange: (scopeId: string) => void;
}

/**
 * The thing a rule is about, chosen from whichever listing owns it.
 *
 * One component per scope rather than four listings fetched at once: only the
 * chosen scope's hook is mounted, so opening this costs one request instead of
 * four, three of which nobody looked at.
 *
 * API keys are the exception and take an id typed in. There is no admin listing
 * of other people's keys — a key's prefix is the only part of it that is ever
 * shown, and a directory of everybody's keys is not a thing this console should
 * invent for the sake of a dropdown.
 */
export function SubjectPicker({ scope, value, onChange }: SubjectPickerProps) {
  switch (scope) {
    case "all":
      // The catch-all has one subject and it is every request, so there is
      // nothing to pick. Stated rather than rendered as an empty control.
      return <p>Applies to every request.</p>;
    case "provider":
      return <ProviderSubject value={value} onChange={onChange} />;
    case "model":
      return <ModelSubject value={value} onChange={onChange} />;
    case "group":
      return <GroupSubject value={value} onChange={onChange} />;
    case "user":
      return <UserSubject value={value} onChange={onChange} />;
    case "api_key":
      return (
        <Input
          label="API key"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder="key id"
          hint="The key's id. Keys are not listed here."
        />
      );
  }
}

interface ChoiceProps {
  value: string;
  onChange: (scopeId: string) => void;
}

function ProviderSubject({ value, onChange }: ChoiceProps) {
  const providers = useProviders();
  return (
    <Select label="Provider" value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="">Choose…</option>
      {(providers.data?.items ?? []).map((provider) => (
        <option key={provider.id} value={provider.id}>
          {provider.name}
        </option>
      ))}
    </Select>
  );
}

function ModelSubject({ value, onChange }: ChoiceProps) {
  const models = useModels();
  return (
    <Select label="Model" value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="">Choose…</option>
      {(models.data?.items ?? []).map((model) => (
        <option key={model.id} value={model.id}>
          {model.name}
        </option>
      ))}
    </Select>
  );
}

function GroupSubject({ value, onChange }: ChoiceProps) {
  const groups = useGroups();
  return (
    <Select label="Group" value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="">Choose…</option>
      {(groups.data?.items ?? []).map((group) => (
        <option key={group.id} value={group.id}>
          {group.name}
        </option>
      ))}
    </Select>
  );
}

function UserSubject({ value, onChange }: ChoiceProps) {
  const users = useUsers();
  return (
    <Select label="User" value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="">Choose…</option>
      {(users.data?.items ?? []).map((user) => (
        <option key={user.id} value={user.id}>
          {user.email ?? user.display_name ?? user.subject}
        </option>
      ))}
    </Select>
  );
}

/** The five scopes, in the order the API lists them. */
export const SCOPES: { value: RedactionScope; label: string }[] = [
  { value: "provider", label: "A provider" },
  { value: "model", label: "A model" },
  { value: "group", label: "A group" },
  { value: "user", label: "A user" },
  { value: "api_key", label: "An API key" },
];

export function scopeNoun(scope: RedactionScope): string {
  return {
    all: "Every request",
    provider: "Provider",
    model: "Model",
    group: "Group",
    user: "User",
    api_key: "API key",
  }[scope];
}
