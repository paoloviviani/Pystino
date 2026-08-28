/**
 * The last line, so a render error is a message rather than a blank page.
 *
 * Added after one: two crashes inside the thread left an empty document with
 * the answer only in the browser console, and the report that reached me was
 * "it flashes then disappears" — which is exactly what an unguarded React tree
 * looks like from outside. The tests that would have caught those crashes now
 * exist; this is for the next one, which will be different.
 *
 * A class, because `componentDidCatch` has no hook equivalent.
 */

import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  message: string | null;
}

export class ErrorBoundary extends Component<Props, State> {
  override state: State = { message: null };

  static getDerivedStateFromError(error: unknown): State {
    return { message: error instanceof Error ? error.message : "Something went wrong." };
  }

  override componentDidCatch(error: Error, info: ErrorInfo) {
    // Kept in the console for whoever is looking, with the component stack —
    // which is the part that says *where*, and is what the message alone
    // cannot tell you.
    console.error("chat failed to render", error, info.componentStack);
  }

  override render() {
    if (this.state.message === null) return this.props.children;
    return (
      <div role="alert" style={{ padding: "2rem", maxWidth: "40rem" }}>
        <h1>This screen could not be displayed</h1>
        {/* The real message. A generic apology here would make the next
            report as unactionable as the one that prompted this file. */}
        <p>
          <code>{this.state.message}</code>
        </p>
        <p>Reloading may help. If it does not, the details are in the browser console.</p>
      </div>
    );
  }
}
