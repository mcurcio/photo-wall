"""The console's contract with Central, exported for code generation: the one place the console's
types, limits, patterns and refusal codes come from (scripts/console_types.py turns it into
central/console/src/generated/ at build time; its --check test fails when they drift).

`console_contract()` answers one JSON object:

    {"schemas": {<name>: <JSON Schema>, ...},      # pydantic's, mode "validation" for a body the
                                                    # console sends, "serialization" for a view it reads
     "constants": {<NAME>: <number | string>, ...}, # values the console shows or steps by
     "refusals": {<context>: [<code>, ...], ...}}   # each context's closed refusal vocabulary

Keys are sorted and every value is plain JSON, so the generated files are stable for an unchanged
contract. A model, constant or refusal vocabulary the console needs is added to the tables below,
never retyped in TypeScript.
"""
from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Final

from pydantic import BaseModel

CONSOLE_BODIES: Final[tuple[type[BaseModel], ...]] = ()       # bodies the console sends
CONSOLE_VIEWS: Final[tuple[type[BaseModel], ...]] = ()        # answers the console reads
CONSOLE_CONSTANTS: Final[Mapping[str, float | int | str]] = {}
CONSOLE_REFUSALS: Final[Mapping[str, type[StrEnum]]] = {}     # context name -> its refusal enum


def console_contract() -> dict[str, object]:
    """The contract above as one JSON-able object (deterministic for unchanged tables)."""
    ...


__all__ = ["CONSOLE_BODIES", "CONSOLE_CONSTANTS", "CONSOLE_REFUSALS", "CONSOLE_VIEWS", "console_contract"]
