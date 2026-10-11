import type * as React from "react";

import { cn } from "../ui/cn";
import { Link } from "../ui/link";
import type { OwnerLink } from "./link-to-owner";

export interface EntityHeaderProps {
  /** The entity's name: the page's heading, focusable for a moved focus. */
  title: string;
  /** 1 when the entity's name is the page's own heading (an object page); 2 under a section's. */
  level?: 1 | 2;
  headingRef?: React.Ref<HTMLHeadingElement>;
  /** The list this page belongs to. */
  back?: { text: string; href: string };
  /** The entity's other pages (a projection elsewhere), as plain links. */
  links?: readonly Pick<OwnerLink, "text" | "href">[];
  /** Its identity facts. */
  children?: React.ReactNode;
}

/** EntityHeader: an entity page's name, its way back, its identity and its other pages. */
export function EntityHeader({ title, level = 2, headingRef, back, links = [], children }: EntityHeaderProps) {
  const Heading = level === 1 ? "h1" : "h2";
  return (
    <header className="min-w-0">
      {back && (
        <p className="m-0 mb-1 text-sm">
          <Link href={back.href}>{back.text}</Link>
        </p>
      )}
      <Heading ref={headingRef} tabIndex={-1}
        className={cn("m-0 font-medium text-text wrap-anywhere", level === 1 ? "text-2xl" : "text-xl")}>
        {title}
      </Heading>
      {children}
      {links.length > 0 && (
        <nav aria-label={`Other pages of ${title}`} className="mt-2 flex flex-wrap gap-3 text-sm">
          {links.map((link) => (
            <Link key={link.href} href={link.href}>{link.text}</Link>
          ))}
        </nav>
      )}
    </header>
  );
}
