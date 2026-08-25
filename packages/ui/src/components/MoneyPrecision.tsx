import { createContext, useContext } from "react";

/**
 * Whether money is shown rounded for reading, or exact for reconciling.
 *
 * Two audiences want two different things from the same number. Someone checking
 * what they spent wants milli-units; an administrator comparing our ledger
 * against a provider's invoice wants every digit the ledger holds, because a
 * divergence can be smaller than a milli-unit and still be the thing they are
 * looking for.
 *
 * A context rather than a prop threaded through every screen, because the choice
 * belongs to the *reader* and applies to the whole console at once. Flipping it
 * on one table and not another would produce a page whose figures cannot be
 * added up.
 *
 * Default `false`, deliberately: the safe default is the readable one, and the
 * exact one is a deliberate act.
 */
export const MoneyPrecisionContext = createContext(false);

export function useExactMoney(): boolean {
  return useContext(MoneyPrecisionContext);
}

export interface MoneyPrecisionProviderProps {
  exact: boolean;
  children: React.ReactNode;
}

export function MoneyPrecisionProvider({ exact, children }: MoneyPrecisionProviderProps) {
  return (
    <MoneyPrecisionContext.Provider value={exact}>{children}</MoneyPrecisionContext.Provider>
  );
}
