/**
 * Join class fragments, dropping falsy ones.
 *
 * Deliberately not `clsx`/`classnames`: this is five lines, and the one thing
 * it must never do — let a `false`/`undefined` fragment become the string
 * `"false"`/`"undefined"` in a class list — is the whole of what it does.
 */
export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}
