import * as React from "react";

export interface SectionProps {
  /** The section's heading and its region's accessible name. */
  title: string;
  /** A new value (the section's next read) tries a failed section again. */
  resetKey?: unknown;
  children: React.ReactNode;
}

interface BoundaryProps {
  resetKey?: unknown;
  children: React.ReactNode;
}

class Boundary extends React.Component<BoundaryProps, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidUpdate(previous: BoundaryProps) {
    if (this.state.failed && previous.resetKey !== this.props.resetKey) {
      this.setState({ failed: false });
    }
  }

  render() {
    if (this.state.failed) {
      return (
        <p role="alert" className="m-0 font-semibold text-alarm">
          This section could not be shown. The rest of the page is current.
        </p>
      );
    }
    return this.props.children;
  }
}

/**
 * Section: a titled card and region with its own error boundary, so a section whose
 * render throws (a served payload that drifted) says so while the rest of the page stays.
 */
export function Section({ title, resetKey, children }: SectionProps) {
  return (
    <section
      aria-label={title}
      className="min-w-0 rounded-card border border-line bg-surface-raised px-4 py-3"
    >
      <h3 className="m-0 mb-2 text-sm font-normal tracking-wide text-muted uppercase">{title}</h3>
      <Boundary resetKey={resetKey}>{children}</Boundary>
    </section>
  );
}
