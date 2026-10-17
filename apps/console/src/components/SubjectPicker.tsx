import { Input, Select } from "@llmp/ui";
import { useGroups, useModels, useProviders, useUsers } from "../lib/admin";
import type { RedactionScope } from "../lib/types";
import { m } from "../paraglide/messages.js";

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
      return <p>{m["subject_picker.all_scope"]()}</p>;
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
          label={m["subject_picker.api_key.label"]()}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={m["subject_picker.api_key.placeholder"]()}
          hint={m["subject_picker.api_key.hint"]()}
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
    <Select label={m["subject_picker.provider.label"]()} value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="">{m["subject_picker.choose"]()}</option>
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
    <Select label={m["subject_picker.model.label"]()} value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="">{m["subject_picker.choose"]()}</option>
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
    <Select label={m["subject_picker.group.label"]()} value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="">{m["subject_picker.choose"]()}</option>
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
    <Select label={m["subject_picker.user.label"]()} value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="">{m["subject_picker.choose"]()}</option>
      {(users.data?.items ?? []).map((user) => (
        <option key={user.id} value={user.id}>
          {user.email ?? user.display_name ?? user.subject}
        </option>
      ))}
    </Select>
  );
}

/**
 * The five scopes, in the order the API lists them.
 *
 * Message functions rather than strings: a label resolved at import is frozen
 * against the language the page happened to load in, and these are rendered
 * inside sentences further down the screen.
 */
export const SCOPES: { value: RedactionScope; label: () => string }[] = [
  { value: "provider", label: m["subject_picker.scope.provider"] },
  { value: "model", label: m["subject_picker.scope.model"] },
  { value: "group", label: m["subject_picker.scope.group"] },
  { value: "user", label: m["subject_picker.scope.user"] },
  { value: "api_key", label: m["subject_picker.scope.api_key"] },
];

/** The bare noun for a scope, for the places that name one mid-sentence. */
export function scopeNoun(scope: RedactionScope): string {
  return {
    all: m["subject_picker.noun.all"](),
    provider: m["subject_picker.noun.provider"](),
    model: m["subject_picker.noun.model"](),
    group: m["subject_picker.noun.group"](),
    user: m["subject_picker.noun.user"](),
    api_key: m["subject_picker.noun.api_key"](),
  }[scope];
}
