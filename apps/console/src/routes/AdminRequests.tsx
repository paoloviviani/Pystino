import { RequestLogView } from "../components/RequestLogView";

/** Every request in the deployment, without its content. */
export function AdminRequests() {
  return <RequestLogView scope="admin" />;
}
