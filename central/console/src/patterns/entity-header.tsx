import type * as React from "react";

import type { OwnerLink } from "./link-to-owner";

export interface EntityHeaderProps {
  /** The entity's name: the page's level-2 heading, focusable for a moved focus. */
  title: string;
  headingRef?: React.Ref<HTMLHeadingElement>;
  /** The list this page belongs to. */
  back?: { text: string; href: string };
  /** The entity's other pages (a projection elsewhere), as plain links. */
  links?: readonly Pick<OwnerLink, "text" | "href">[];
  /** Its identity facts. */
  children?: React.ReactNode;
}

/** EntityHeader: an entity page's name, its way back, its identity and its other pages. */
export function EntityHeader({ title, headingRef, back, links = [], children }: EntityHeaderProps) {
  return (
    <header className="min-w-0">
      {back && (
        <p className="m-0 mb-1 text-sm">
          <a href={back.href} className="text-accent">{back.text}</a>
        </p>
      )}
      <h2 ref={headingRef} tabIndex={-1} className="m-0 text-xl font-medium text-text wrap-anywhere">{title}</h2>
      {children}
      {links.length > 0 && (
        <nav aria-label={`Other pages of ${title}`} className="mt-2 flex flex-wrap gap-3 text-sm">
          {links.map((link) => (
            <a key={link.href} href={link.href} className="text-accent">{link.text}</a>
          ))}
        </nav>
      )}
    </header>
  );
}
