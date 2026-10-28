import React from "react";

/** A Player row's text, then [Frame <id>]… [<Player>] (§61: links follow R4). */
function PlayerEntry({ row, entry }) {
  return (
    <>
      <span className="attention__text">{row.text}</span>
      {row.frames.map((frameId) => (
        <React.Fragment key={frameId}>
          {" "}
          {entry({ key: frameId, frameId, text: `Frame ${frameId}` })}
        </React.Fragment>
      ))}{" "}
      <a className="attention__player" href={row.hardwareHref}>{row.name}</a>
    </>
  );
}

/**
 * The list of Frames and Players needing attention: the attention strip's disclosure (capped) and
 * the full-width Needs attention page (#/attention) both render it.
 *
 * A row about a frame renders through `entry(row)` when given (a button on the Wall
 * side, a link on the Needs attention page); otherwise, and for the scheduler row, it
 * is plain text, so a Show page sees health as status only (R4). A Player row, with
 * `entry`, is its text, then `entry` for each of its Frames ("Frame <id>") and a link to
 * its Hardware page; without `entry` it too is plain text.
 *
 * @param {{rows: import("./attention.js").AttentionRow[], cap?: number, id?: string, className?: string,
 *          entry?: ((row: import("./attention.js").AttentionRow) => React.ReactNode)|null}} props
 */
export function AttentionList({ rows, cap = Infinity, id, className, entry = null }) {
  const shown = rows.slice(0, cap);
  const more = rows.length - shown.length;
  return (
    <ul id={id} className={className} aria-label="Frames and Players needing attention">
      {shown.map((row) => (
        <li key={row.key} className={`attention__item health--${row.severity ?? "alarm"}`}>
          {entry === null ? row.text
            : row.deviceId !== undefined ? <PlayerEntry row={row} entry={entry} />
              : row.frameId !== undefined ? entry(row)
                : row.href !== undefined ? <a className="attention__scene" href={row.href}>{row.text}</a>
                  : row.text}
        </li>
      ))}
      {more > 0 && <li className="attention__more">{`and ${more} more`}</li>}
    </ul>
  );
}
