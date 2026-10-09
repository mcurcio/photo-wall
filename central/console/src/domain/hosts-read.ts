/**
 * The shell's fleet host read as the host views use it (fleetHosts.js `FleetHostsRead`), typed
 * here so no host view imports the polling module, which reaches the write primitive (G1,
 * tests/test_console_routes_r4.py).
 */
export interface HostsRead {
  read: object | null;
  failed: boolean;
  error: { code: string; status: number | null } | null;
}
