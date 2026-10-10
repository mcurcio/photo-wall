/**
 * FocusFilter (design rule H2: focus lives only in the address): the chip saying a list is
 * narrowed to one entity, with the link that ends the focus. The page reads the focus from its
 * route and hands the hrefs in, already formatted; nothing here holds focus state.
 */
export interface FocusFilterProps {
  /** The focused entity's name, or null when the list is not focused. */
  focused: string | null;
  /** What the list shows ("Pi"): the clear link reads "Show every <noun>". */
  noun: string;
  /** The list's address without the focus. */
  clearHref: string;
}

export function FocusFilter({ focused, noun, clearHref }: FocusFilterProps) {
  if (focused === null) return null;
  return (
    <p
      role="group"
      aria-label="Focus"
      className="m-0 mb-2 inline-flex flex-wrap items-center gap-2 rounded-pill border border-accent bg-accent-tint px-3 py-1 text-sm text-text"
    >
      <span>{`Focused on ${focused}`}</span>
      <a href={clearHref} className="text-accent">{`Show every ${noun}`}</a>
    </p>
  );
}

/** FocusLink: the link that narrows a list to one entity (its href carries the focus). */
export function FocusLink({ href, name }: { href: string; name: string }) {
  return (
    <a href={href} aria-label={`Focus on ${name}`} className="ml-2 text-xs text-accent">
      Focus
    </a>
  );
}
