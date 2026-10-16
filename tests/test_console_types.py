"""The console's contract is Central's: central/console/src/generated/contract.ts is what
scripts/console_types.py writes from central.console_contract, and the writer turns pydantic's
schemas into the TypeScript the console codes against.

Mutation probes: edit a range in a contract model (or its constant) without regenerating -> the
committed file differs and --check exits 1; write a discriminated union as `unknown` -> the union
case fails; drop a refusal code -> its context's type case fails.
"""
from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Literal

import pytest
from pydantic import BaseModel, Field, TypeAdapter

from central.console_contract import console_contract
from scripts import console_types
from scripts.console_types import GENERATED, ContractError, contract_text, main

Id = Annotated[str, Field(pattern=r"^[a-z]+$", title="ThingId")]
Lux = Annotated[float, Field(ge=0, le=100, title="Lux")]


class On(BaseModel):
    kind: Literal["on"] = "on"
    lux: Lux


class Off(BaseModel):
    kind: Literal["off"] = "off"


class Thing(BaseModel):
    """A thing."""
    id: Id
    state: Annotated[On | Off, Field(discriminator="kind")]
    steps: int = Field(ge=1, le=9, multiple_of=2)
    parts: tuple[float, float]
    note: str | None = None


class Refusal(StrEnum):
    GONE = "gone"
    TAKEN = "taken"


def _contract(maximum: float = 100) -> dict:
    lux = Annotated[float, Field(ge=0, le=maximum, title="Lux")]
    return {
        "schemas": {"Thing": Thing.model_json_schema(mode="serialization"),
                    "Lux": TypeAdapter(lux).json_schema(), "ThingId": TypeAdapter(Id).json_schema()},
        "constants": {"B_SOURCE": "pi", "A_MAX": 4.0},
        "refusals": {"things": [code.value for code in Refusal]},
    }


def test_the_committed_contract_is_what_central_writes_now():
    assert main(["--check"]) == 0, f"run: python -m scripts.console_types ({GENERATED})"


def test_a_contract_changed_without_regenerating_fails_the_check(tmp_path, monkeypatch, capsys):
    stale = tmp_path / "contract.ts"
    stale.write_text(contract_text(_contract(maximum=100)))
    monkeypatch.setattr(console_types, "GENERATED", stale)
    monkeypatch.setattr(console_types, "written", lambda: contract_text(_contract(maximum=200)))
    assert main(["--check"]) == 1
    assert "python -m scripts.console_types" in capsys.readouterr().err
    assert stale.read_text() == contract_text(_contract(maximum=100))      # --check writes nothing
    assert main([]) == 0 and main(["--check"]) == 0                         # a write makes it current


def test_central_answers_one_json_contract_with_every_table():
    contract = console_contract()
    assert set(contract) >= {"schemas", "constants", "refusals"}
    assert json.loads(json.dumps(contract)) == contract
    assert {"room_light", "frame_lighting", "paper_profiles", "reports"} <= set(contract["refusals"])


def test_types_limits_constants_and_refusals_are_written_as_typescript():
    text = contract_text(_contract())
    assert "export type ThingId = string;" in text
    assert "export type Lux = number;" in text
    assert "export interface On {\n  kind: \"on\";\n  lux: Lux;\n}" in text       # an alias by its title
    assert "export interface Off {\n  kind: \"off\";\n}" in text                   # a const is always present
    assert "  id: ThingId;\n  state: On | Off;\n" in text                         # the union on `kind`
    assert "  parts: [number, number];\n" in text
    assert "  note?: string | null;\n" in text
    assert "/** A thing. */\nexport interface Thing {" in text
    assert "    steps: {\n      integer: true,\n      maximum: 9,\n      minimum: 1,\n      multipleOf: 2,\n    }," in text
    assert 'ThingId: "^[a-z]+$",' in text
    assert "export const CONSTANTS = {\n  A_MAX: 4,\n  B_SOURCE: \"pi\",\n} as const;" in text
    assert 'export const REFUSALS = {\n  things: [\n    "gone",\n    "taken",\n  ],\n} as const;' in text
    assert 'export type ThingsRefusal = (typeof REFUSALS)["things"][number];' in text


def test_an_alias_named_by_its_own_title_is_a_type_with_no_table_entry():
    text = contract_text({"schemas": {"Thing": Thing.model_json_schema(mode="serialization")}})
    assert "export type ThingId = string;" in text and "  id: ThingId;\n" in text
    assert "ThingId: {\n    value: {\n      pattern: \"^[a-z]+$\",\n    },\n  }," in text
    assert "export type Steps" not in text and "export type Note" not in text      # a field's own title is no alias


def test_the_text_is_the_same_for_the_same_contract_in_any_order():
    contract = _contract()
    shuffled = {key: dict(reversed(list(value.items()))) if isinstance(value, dict) else value
                for key, value in reversed(list(contract.items()))}
    assert contract_text(shuffled) == contract_text(contract)


def test_two_different_schemas_under_one_name_are_refused():
    class Thing(BaseModel):     # noqa: F811 - the clash is the point
        other: int

    contract = _contract()
    contract["schemas"]["Other"] = {**Thing.model_json_schema(), "$defs": {}}
    contract["schemas"]["Thing2"] = {"$defs": {"Thing": Thing.model_json_schema()}, "type": "string"}
    with pytest.raises(ContractError, match="Thing"):
        contract_text(contract)
