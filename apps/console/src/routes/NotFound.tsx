import { Notice } from "@llmp/ui";
import { Link } from "react-router";

export function NotFound() {
  return (
    <Notice tone="info" title="No such page">
      No such address in the console. <Link to="/">Back to your usage</Link>.
    </Notice>
  );
}
