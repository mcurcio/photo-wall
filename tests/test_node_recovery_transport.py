"""Real Linux credential-packet checks; run with unittest without extra packages."""
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from appliance.node.recovery import (
    RESTORE_BUDGET_MS,
    STOP_BUDGET_MS,
    RecoveryObligation,
    RecoverySupervisor,
)
from appliance.node.recovery_linux import RecoveryClient, RecoveryServer
from appliance.node.storage import BootStore
from contracts.node_protocol import NodeProcessIdentity


@unittest.skipUnless(hasattr(socket, "SO_PASSCRED") and os.getuid() == 0, "Linux root credential socket fixture")
class RecoveryTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.boot = uuid4()
        self.store = BootStore(root, boot_id=self.boot, policy={})
        self.supervisor = RecoverySupervisor(self.store, SimpleNamespace(initiate=lambda: False),
            SimpleNamespace(stopped=lambda o: True, controlled=lambda *a, **k: True))
        self.obligation = RecoveryObligation(self.boot, uuid4(), "a" * 64,
            NodeProcessIdentity(123, 456, uuid4()), 1, "a" * 64, "b" * 64, "a" * 64,
            1000, 1000 + STOP_BUDGET_MS, 1000 + STOP_BUDGET_MS + RESTORE_BUDGET_MS)
        self.server = RecoveryServer(self.supervisor, root / "control.sock")
        self.client = RecoveryClient(root / "control.sock")

    def tearDown(self):
        self.server.close()
        self.store.close()
        self.temporary.cleanup()

    def exchange(self, callback, *, peer=True):
        result = []
        def client():
            try:
                result.append(callback())
            except Exception as error:
                result.append(error)
        thread = threading.Thread(target=client)
        # Only the client uses a thread; journal writes stay on this owner thread.
        with patch("appliance.node.recovery_linux.broker_peer", lambda pid, uid: peer and pid == os.getpid() and uid == 0), patch("appliance.node.recovery_linux.boottime_ms", lambda: 1000):
            thread.start()
            while thread.is_alive():
                self.server.serve_one()
            thread.join(timeout=2)
        return result[0]

    def test_real_packet_registration_and_immutable_retry(self):
        self.assertEqual(self.exchange(lambda: self.client.arm(self.obligation)), self.obligation.receipt)
        self.assertEqual(self.exchange(lambda: self.client.arm(self.obligation)), self.obligation.receipt)
        self.assertEqual(len(self.supervisor._load()["records"]), 1)

    def test_wrong_dedicated_peer_cannot_arm(self):
        self.assertIsInstance(self.exchange(lambda: self.client.arm(self.obligation), peer=False), ValueError)
        self.assertEqual(self.supervisor._load()["records"], {})

    def test_foreign_boot_cannot_arm(self):
        from dataclasses import replace
        self.assertIsInstance(self.exchange(lambda: self.client.arm(replace(self.obligation, boot_id=uuid4()))), ValueError)
        self.assertEqual(self.supervisor._load()["records"], {})

    def test_completion_uses_same_authenticated_port(self):
        self.exchange(lambda: self.client.arm(self.obligation))
        self.assertEqual(self.exchange(lambda: self.client.advance(self.obligation, {"kind": "stopped"})), self.obligation.receipt)
        self.assertEqual(next(iter(self.supervisor._load()["records"].values()))["phase"], "stopped")


if __name__ == "__main__":
    unittest.main()
