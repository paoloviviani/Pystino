import { Notice } from "@llmp/ui";
import { Link } from "react-router";

export function NotFound() {
  return (
    <Notice tone="info" title="No such page">
      That address does not match anything in the console. <Link to="/">Back to your usage</Link>.
    </Notice>
  );
}
