"""Boot offers as Central issued them before migration 117, for tests that run today's Central on
a schema older than 117 (decision 0034, S2a).

Until 117 an admission named its offer through `node_offer_contexts`, and Central wrote one
context per issued offer (basis 'node_v2', naming itself). Today's `NodeBootService.offer` writes
none, so on an older schema the admission's foreign key refuses the enrollment. `write_contexts`
makes `offer` write the context as Central did, and only while the table exists."""

from __future__ import annotations

import pytest

from central.fleet.node_boot import NodeBootService


def write_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    offer = NodeBootService.offer

    def offer_with_context(self, request):
        result = offer(self, request)
        with self.sessions.db.transaction() as conn:
            if conn.execute("SELECT to_regclass('node_offer_contexts') AS name").fetchone()["name"]:
                conn.execute("INSERT INTO node_offer_contexts(offer_id,basis,node_offer_id) "
                             "VALUES(%s,'node_v2',%s) ON CONFLICT DO NOTHING",
                             (result.offer_id, result.offer_id))
        return result

    monkeypatch.setattr(NodeBootService, "offer", offer_with_context)
