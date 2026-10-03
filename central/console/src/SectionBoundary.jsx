import React from "react";

/**
 * One Player page section's error boundary (console DDD §9 failure modes): a section whose
 * render throws (a served payload that drifted) reads "This section could not be shown",
 * while the rest of the page and the console stay up. A new `resetKey` (the section's
 * next read) tries the section again.
 *
 * @extends {React.Component<{title: string, resetKey?: unknown, children: React.ReactNode},
 *                           {failed: boolean}>}
 */
export class SectionBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { failed: false };
  }

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidUpdate(previous) {
    if (this.state.failed && previous.resetKey !== this.props.resetKey) {
      this.setState({ failed: false });
    }
  }

  render() {
    const { title, children } = this.props;
    return (
      <section className="player__section" role="region" aria-label={title}>
        <h3 className="player__section-title">{title}</h3>
        {this.state.failed ? (
          <p className="player__section-failed" role="alert">
            This section could not be shown. The rest of the page is current.
          </p>
        ) : (
          children
        )}
      </section>
    );
  }
}
