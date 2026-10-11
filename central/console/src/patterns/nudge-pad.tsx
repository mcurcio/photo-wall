import { Button } from "../ui/button";

export interface NudgePadProps {
  /** The pad's accessible name. */
  label: string;
  /** What each button names after its direction ("Move up the top-left corner"). */
  subject: string;
  /** One step on each axis, -1, 0 or 1. */
  onNudge: (dx: number, dy: number) => void;
  disabled?: boolean;
}

const DIRECTIONS = [
  { name: "up", mark: "↑", dx: 0, dy: -1 },
  { name: "left", mark: "←", dx: -1, dy: 0 },
  { name: "right", mark: "→", dx: 1, dy: 0 },
  { name: "down", mark: "↓", dx: 0, dy: 1 },
] as const;

/**
 * NudgePad: four arrow buttons in a cross. Each press reports one step in its direction; the
 * caller decides how far a step goes.
 */
export function NudgePad({ label, subject, onNudge, disabled = false }: NudgePadProps) {
  const button = (direction: (typeof DIRECTIONS)[number]) => (
    <Button
      key={direction.name}
      className="w-12 px-0"
      aria-label={`Move ${direction.name}: ${subject}`}
      disabled={disabled}
      onClick={() => onNudge(direction.dx, direction.dy)}
    >
      <span aria-hidden="true">{direction.mark}</span>
    </Button>
  );
  return (
    <div role="group" aria-label={label} className="grid w-fit grid-cols-3 gap-1">
      <span />
      {button(DIRECTIONS[0])}
      <span />
      {button(DIRECTIONS[1])}
      <span />
      {button(DIRECTIONS[2])}
      <span />
      {button(DIRECTIONS[3])}
      <span />
    </div>
  );
}
