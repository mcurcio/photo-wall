import React, { useId } from "react";

/**
 * A read-only card for one saved thing, with its actions (flow design §4, §5, §6
 * frozen surface; presentational). Header: the title (the card's accessible name)
 * and an optional status chip; body: label/value lines; footer: the actions.
 *
 * A chip never relies on colour alone: its tone ("ok", "todo" or "alarm") adds the
 * status colour and shape (index.css `.health--*`), and its text states the state.
 *
 * @param {{title: string, chip?: {tone: "ok"|"todo"|"alarm", text: string}|null,
 *          lines: ReadonlyArray<{label: string, value: React.ReactNode}>,
 *          actions?: React.ReactNode}} props
 */
export function SummaryCard({ title, chip = null, lines, actions = null }) {
  const titleId = useId();
  return (
    <article className="card" aria-labelledby={titleId}>
      <header className="card__head">
        <h3 id={titleId} className="card__title">
          {title}
        </h3>
        {chip !== null && <span className={`card__chip health--${chip.tone}`}>{chip.text}</span>}
      </header>
      <dl className="record card__lines">
        {lines.map(({ label, value }) => (
          <React.Fragment key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </React.Fragment>
        ))}
      </dl>
      {actions !== null && <footer className="record__actions card__actions">{actions}</footer>}
    </article>
  );
}
