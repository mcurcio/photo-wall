import { busLinkFact, type Fact, fact } from "../facts.js";
import { hostRow } from "../hostHealth.js";
import type { HostsRead } from "./hosts-read";

/**
 * A Pi's Node API link as the Hardware pages show it, from the one place Central serves it:
 * the fleet host read (the list and the Pi page both hold it; every fact is served once). The
 * read not having happened says so, and is not "the server does not send it".
 */
export function hostsBusLink(hosts: HostsRead | null, deviceId: string): Fact {
  if (hosts === null) return fact({ kind: "unknown", why: "the fleet host read is not on" });
  const read = hosts.read as { read_at?: number } | null;
  if (read === null) {
    return fact({ kind: "unknown", why: hosts.failed
      ? `the fleet host read failed: ${hosts.error?.code ?? "unanswered"}`
      : "the fleet host read is not read yet" });
  }
  const row = hostRow(read, deviceId) as { bus_link?: Parameters<typeof busLinkFact>[0] } | null;
  return busLinkFact(row?.bus_link, read.read_at);
}
