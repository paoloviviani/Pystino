import { RequestLogView } from "../components/RequestLogView";

/** The signed-in person's own requests, without their content. */
export function MyRequests() {
  return <RequestLogView scope="me" />;
}
