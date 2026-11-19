"""Versioned AppManager executable, idle (decision 0034, S4c).

The Pi prepares its app root at boot (appliance/boot/node_bootstrap.py `prepare_roots`), and no
release switch reaches it any more: the manager has nothing to do. It logs once that it is idle and
waits for SIGTERM, until the manager root itself goes (S4c-2b).
"""
from __future__ import annotations

import logging
import signal
import sys

LOG = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    LOG.info("app manager: idle; nothing reaches it until it is retired")
    signal.signal(signal.SIGTERM, lambda _signal, _frame: sys.exit(0))
    while True:
        signal.pause()


if __name__ == "__main__":
    main()
