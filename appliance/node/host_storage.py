"""Host-only durable reboot journal. No lifecycle or Player imports."""
from __future__ import annotations

import json
from dataclasses import asdict
from uuid import UUID

from appliance.boot_store import BootStore
from appliance.node.host import RebootRecord, RebootRequest
from contracts.node_protocol import NodeProducerV2, encode_node_message, parse_node_message


def request_dict(request: RebootRequest) -> dict:
    return json.loads(json.dumps(asdict(request), default=str))


def request_from(value: dict) -> RebootRequest:
    fields = dict(value)
    producer = dict(fields.pop("producer"))
    for key in ("kernel_boot_id", "incarnation_id"):
        producer[key] = UUID(producer[key])
    fields["producer"] = NodeProducerV2(**producer)
    for key in ("command_id", "command_session_id", "offer_id"):
        fields[key] = UUID(fields[key])
    return RebootRequest(**fields)


class FileRebootJournal:
    def __init__(self, store: BootStore, *, capacity: int = 1024):
        self.store = store
        self.capacity = capacity
        self.data = store.read("reboots") or {"sequence": 0, "records": {}}
        if set(self.data) != {"sequence", "records"} or type(self.data["sequence"]) is not int or self.data["sequence"] < 0 or not isinstance(self.data["records"], dict) or len(self.data["records"]) > capacity:
            raise ValueError("reboot_journal_invalid")
        for key in self.data["records"]:
            self.get(UUID(key))

    def get(self, command_id: UUID) -> RebootRecord | None:
        row = self.data["records"].get(str(command_id))
        if row is None:
            return None
        if set(row) != {"request", "response", "invocation_started", "event", "effect_unknown"} or type(row["invocation_started"]) is not bool or type(row["effect_unknown"]) is not bool:
            raise ValueError("reboot_journal_record_invalid")
        request = request_from(row["request"])
        if request.command_id != command_id:
            raise ValueError("reboot_journal_identity")
        return RebootRecord(request, parse_node_message(row["response"].encode()),
                            row["invocation_started"],
                            parse_node_message(row["event"].encode()) if row["event"] else None,
                            row["effect_unknown"])

    def put(self, record: RebootRecord) -> None:
        key = str(record.request.command_id)
        if key not in self.data["records"] and len(self.data["records"]) >= self.capacity:
            raise ValueError("reboot_journal_capacity")
        row = {"request": request_dict(record.request),
               "response": encode_node_message(record.response).decode(),
               "invocation_started": record.invocation_started,
               "event": encode_node_message(record.event).decode() if record.event else None,
               "effect_unknown": record.effect_unknown}
        data = {**self.data, "records": {**self.data["records"], key: row}}
        self.store.write("reboots", data)
        self.data = data

    def next_sequence(self) -> int:
        data = {**self.data, "sequence": self.data["sequence"] + 1}
        self.store.write("reboots", data)
        self.data = data
        return data["sequence"]


class RebootDelivery:
    """Bounded fair delivery cursor; retained anti-replay records are never removed."""

    def __init__(self, store: BootStore):
        self.store = store
        self.data = store.read('reboot-delivery') or {'cursor': 0, 'acknowledged': {}}
        value = self.data
        if (set(value) != {'cursor', 'acknowledged'} or type(value['cursor']) is not int
                or value['cursor'] < 0 or not isinstance(value['acknowledged'], dict)
                or len(value['acknowledged']) > 1024):
            raise ValueError('reboot_delivery_invalid')
        for key, slots in value['acknowledged'].items():
            UUID(key)
            if (not isinstance(slots, dict) or not set(slots) <= {'response', 'event'}
                    or any(not isinstance(digest, str) or len(digest) != 64
                           or any(c not in '0123456789abcdef' for c in digest)
                           for digest in slots.values())):
                raise ValueError('reboot_delivery_ack_invalid')

    def flush(self, journal: FileRebootJournal, *, session_id: UUID, send, budget: int = 2) -> int:
        import hashlib
        import http.client

        keys = tuple(journal.data['records'])
        count = 2 * len(keys)
        sent = 0
        for _ in range(count):
            if sent >= budget:
                break
            index = self.data['cursor'] % count
            key, slot = keys[index // 2], ('response', 'event')[index % 2]
            record = journal.get(UUID(key))
            message = getattr(record, slot)
            self.data = {**self.data, 'cursor': (index + 1) % count}
            if message is None or record.request.command_session_id != session_id:
                continue
            digest = hashlib.sha256(encode_node_message(message)).hexdigest()
            if self.data['acknowledged'].get(key, {}).get(slot) == digest:
                continue
            sent += 1
            try:
                accepted = send(message)
            except (OSError, ValueError, http.client.HTTPException):
                accepted = False
            if accepted is True:
                acknowledged = {**self.data['acknowledged']}
                acknowledged[key] = {**acknowledged.get(key, {}), slot: digest}
                self.data = {**self.data, 'acknowledged': acknowledged}
            # Also persist failed-send cursor progress: a rejected record cannot
            # starve later evidence, and an ambiguous receipt retries exact bytes.
            self.store.write('reboot-delivery', self.data)
        return sent
