import { Notice } from "@llmp/ui";
import { Link } from "react-router";
import { Message } from "../components/Message";
import { m } from "../paraglide/messages.js";

export function NotFound() {
  return (
    <Notice tone="info" title={m["not_found.title"]()}>
      <Message message={m["not_found.body"]} markup={{ link: (children) => <Link to="/">{children}</Link> }} />
    </Notice>
  );
}
