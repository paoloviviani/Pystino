import type { UiMessages } from "@llmp/ui";
import { m } from "../paraglide/messages.js";

/**
 * The shared components' own words, from the console's catalogue.
 *
 * `@llmp/ui` carries English defaults and takes these through a provider so it
 * never imports the generated Paraglide module. Each entry reads the message at
 * the moment this is called, so call it where the locale is already settled (the
 * root render, after `initLocale`), not at module load.
 */
export function uiMessages(): UiMessages {
  return {
    close: m["ui.dialog.close"](),
    dismiss: m["ui.toast.dismiss"](),
    loading: m["ui.spinner.loading"](),
    tableEmpty: m["ui.table.empty"](),
    paginationLabel: m["ui.pagination.label"](),
    paginationPrevious: m["ui.pagination.previous"](),
    paginationNext: m["ui.pagination.next"](),
    paginationRows: m["ui.pagination.rows"](),
    paginationNone: (noun) => m["ui.pagination.none"]({ noun }),
    paginationOf: (total, noun) => m["ui.pagination.of"]({ total, noun }),
  };
}
