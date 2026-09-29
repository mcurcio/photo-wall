/** The plain operator name of a Source row. Older rows only carry `source_ref`. */
export function sourceName(source) {
  if (typeof source === "string") return source.replace(/:[1-9][0-9]*$/, "");
  if (typeof source?.name === "string" && source.name !== "") return source.name;
  return String(source?.source_ref ?? "").replace(/:[1-9][0-9]*$/, "");
}

/** A stored Source with this logical name, if it is in the current snapshot. */
export function namedSource(sources, name) {
  return sources.find((source) => sourceName(source) === name);
}

/** Browser-local date for a stored UTC instant, as the date input expects. */
export function sourceDay(seconds) {
  if (seconds == null) return "";
  const date = new Date(Number(seconds) * 1000);
  if (Number.isNaN(date.getTime())) return "";
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}
