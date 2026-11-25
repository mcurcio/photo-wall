import { Button } from "../ui/button";

/** One action on a whole wall: its word, its accessible name (which says it acts on every Frame of
 * the wall) and what a press does. */
export interface WallAction {
  key: string;
  /** The button's word ("Skip"). */
  label: string;
  /** Its accessible name ("Skip every Frame on Living room wall"). */
  name: string;
  onAction: () => void;
}

export interface WallActionsProps {
  /** The wall's name: the group's name ("Actions for Living room wall"). */
  wall: string;
  /** The actions Central offers on this wall now; one not offered is absent, never disabled. */
  actions: readonly WallAction[];
  /** A press is on its way to Central: the buttons wait. */
  busy?: boolean;
}

/**
 * WallActions (design language §4, T1 Home; decision 0036, the owner's S1 and S2): the actions on
 * a whole wall, at the head of its `WallMap`: **Skip** and **Previous**, each moving every Frame of
 * that wall that can move at one instant. Each is named for what it acts on, so a screen reader
 * hears "Skip every Frame on Living room wall". Only what is offered is drawn; with nothing offered
 * the group is absent.
 */
export function WallActions({ wall, actions, busy = false }: WallActionsProps) {
  if (actions.length === 0) return null;
  return (
    <div role="group" aria-label={`Actions for ${wall}`} className="flex min-w-0 flex-wrap items-center gap-2">
      {actions.map((action) => (
        <Button key={action.key} aria-label={action.name} disabled={busy} onClick={action.onAction}>{action.label}</Button>
      ))}
    </div>
  );
}
