import { PrivacyHeader } from "../components/PrivacyHeader";
import { PrivacyTestBox } from "../components/PrivacyTest";
import { PAGE } from "../lib/layout";

/** The Test tab: the real path over supplied facts, as any user an administrator picks. */
export function AdminPrivacyTest() {
  return (
    <div className={PAGE}>
      <PrivacyHeader tab="test" />
      <PrivacyTestBox area="admin" />
    </div>
  );
}
