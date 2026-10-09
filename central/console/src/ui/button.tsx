import { Button as BaseButton } from "@base-ui/react/button";
import { type VariantProps, cva } from "class-variance-authority";
import type * as React from "react";

import { cn } from "./cn";

/**
 * Button (console design system). `outline` (border and tint) is the default, for a
 * secondary action; `primary` (filled) is a form's one forward action; `ghost` is a
 * record's other actions; `danger` is only a destructive confirmation's confirm.
 */
export const buttonVariants = cva(
  [
    "inline-flex cursor-pointer items-center justify-center gap-2 rounded-button border px-5 py-2",
    "font-sans text-sm font-medium",
    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
    "disabled:cursor-not-allowed disabled:opacity-60",
  ],
  {
    variants: {
      variant: {
        outline: "border-accent bg-accent-tint text-accent",
        primary: "border-accent bg-accent text-on-accent",
        ghost: "border-transparent bg-transparent text-accent hover:bg-accent-tint",
        danger: "border-alarm bg-alarm text-on-alarm",
      },
    },
    defaultVariants: { variant: "outline" },
  },
);

export type ButtonProps = React.ComponentProps<typeof BaseButton> &
  VariantProps<typeof buttonVariants>;

export function Button({ className, variant, type = "button", ...props }: ButtonProps) {
  return (
    <BaseButton
      type={type}
      className={cn(buttonVariants({ variant }), className as string | undefined)}
      {...props}
    />
  );
}
