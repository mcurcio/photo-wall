import { HardwareList } from "../domain/hardware-list";
import type { HostsRead } from "../domain/host-health";

export interface HardwarePageProps {
  snapshot: object | null;
  bootFacts: { unavailable?: boolean; devices?: Map<string, object> } | null;
  hosts: HostsRead | null;
  /** The route's focus (`#/hardware?pi=<device-id>`), or null. */
  focus: string | null;
}

/**
 * Hardware (`#/hardware`; console by domain § Fleet): is every Pi powered, healthy and linked?
 * The list of every Pi, focused on one by the address alone (design rule H2).
 */
export function HardwarePage({ snapshot, bootFacts, hosts, focus }: HardwarePageProps) {
  return <HardwareList snapshot={snapshot} bootFacts={bootFacts} hosts={hosts} focus={focus} />;
}
