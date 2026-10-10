import { Switch as BaseSwitch } from "@base-ui/react/switch";

import { cn } from "./cn";
import { useFieldControl } from "./field";

export interface SwitchProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
}

/**
 * Switch (design language §4): an on/off setting, inside a Field (Base UI Switch, so it is a
 * `switch` to a screen reader). The accent marks it on, a control you can act on.
 */
export function Switch({ checked, onChange, disabled }: SwitchProps) {
  const { id, describedBy } = useFieldControl();
  return (
    <BaseSwitch.Root
      id={id}
      aria-describedby={describedBy}
      checked={checked}
      disabled={disabled}
      onCheckedChange={(next) => onChange(next)}
      className={cn(
        "relative inline-flex h-6 w-11 shrink-0 cursor-pointer items-center rounded-pill border border-line-input",
        "bg-surface-input p-0.5 data-checked:border-accent data-checked:bg-accent",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
        "data-disabled:cursor-not-allowed data-disabled:opacity-60",
      )}
    >
      <BaseSwitch.Thumb
        className="block size-4 rounded-pill bg-muted transition-transform data-checked:translate-x-5 data-checked:bg-on-accent"
      />
    </BaseSwitch.Root>
  );
}
