/**
 * Story data for the Hardware members: a snapshot and a fleet host read in Central's served
 * shapes (as tests/test_console_host_health.py builds them). Fixtures only; no live read.
 */
const THRESHOLDS = {
  host_silent_after_seconds: 60,
  metrics: [{ name: "soc_temperature", unit: "celsius", notice_at: 75, alarm_at: 80 }],
};

const temp = (value: number) => ({ name: "soc_temperature", value, unit: "celsius", source: "host_sampler" });
const cpu = { name: "cpu_busy", value: 23, unit: "percent", source: "host_sampler" };
const run = { name: "runtime_available", value: 1.2e9, unit: "bytes", source: "host_sampler" };

const host = (device: string, receivedAt: number, celsius: number, linked: boolean | null) => ({
  device_id: device,
  previous_boot_received_at: null,
  intake_full: false,
  host: { received_at: receivedAt, fault_code: null, metrics: [temp(celsius), cpu, run] },
  bus_link: linked === null ? undefined : { linked, changed_at: 400, looked_at: 995 },
});

const player = (id: string, device: string, registered: number, retired: number | null = null) => ({
  id, device_id: device, registered_at: registered, last_seen: 900, retired_at: retired, authority_epoch: 3,
});

export const SNAPSHOT = {
  inventory: {
    read_at: 1000,
    frames: [
      { id: "lobby-left", player_id: "p-a", output_id: "HDMI-A-1" },
      { id: "lobby-right", player_id: "p-b", output_id: "HDMI-A-1" },
    ],
    players: [player("p-a", "device-a", 1), player("p-b", "device-b", 2), player("p-c", "device-c", 3),
      player("p-r", "device-r", 4, 500)],
    outputs: [
      { player_id: "p-a", output_id: "HDMI-A-1", observation: { connected: true } },
      { player_id: "p-b", output_id: "HDMI-A-1", observation: { connected: true } },
      { player_id: "p-c", output_id: "HDMI-A-1", observation: { connected: true } },
    ],
  },
};

export const BOOT_FACTS = { loaded: true, unavailable: false, devices: new Map<string, object>() };

export const HOSTS = {
  failed: false,
  error: null,
  read: {
    read_at: 1000,
    thresholds: THRESHOLDS,
    devices: [host("device-a", 998, 52, true), host("device-b", 999, 82, false), host("device-c", 120, 50, null)],
  },
};

export const HOSTS_FAILED = { failed: true, error: { code: "unanswered", status: null }, read: null };
