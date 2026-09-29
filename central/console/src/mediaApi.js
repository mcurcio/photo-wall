/** The canonical operator endpoint for requesting a Source refresh. */
export function sourceRefreshPath(sourceRef) {
  return `/v1/operator/sources/${encodeURIComponent(sourceRef)}/refresh`;
}
