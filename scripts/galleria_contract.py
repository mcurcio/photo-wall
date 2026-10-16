"""Write Galleria's generated contract: custom_components/galleria/contract.py, the stdlib-only copy of
every name, key, cap and wire shape the Home Assistant integration reads or writes, taken from
`contracts` (and the two state-bucket names nodeapi.buffers builds). The console's contract.ts pattern
(scripts/console_types.py): Galleria ships through HACS on its own and imports no package of this
repository ("Galleria imports no package of this repository", pyproject), so it carries this copy and a
check test (tests/test_galleria_contract.py) fails when the copy drifts from `contracts`.

What the file holds:

- CONSTANTS: the door's path, account and user; the state buckets' streams and subject prefixes on
  Central's hub and on a Pi's bus; every state key and its largest value; the method's subject and
  refusal codes; the Pi's door port; the Player line's schema major.
- One TypedDict per object a wire value holds, from pydantic's JSON Schemas (validation mode), and a
  `type` alias per closed enum and per named union.
- The schemas themselves (`_SCHEMAS`, titles and descriptions dropped) and one stdlib checker of them,
  so each decoder is as strict as its `contracts` twin: strict JSON within the value's cap (no
  duplicate key, no NaN), exactly each object's fields, no bool for a number, every bound and pattern.
  A value must carry every field, defaulted ones too: every writer of these values writes canonical
  JSON of a whole model (contracts/wire_codec.py), so the copy is stricter than pydantic only for a
  value no writer makes.
- The codecs the integration uses, named as their `contracts` twins, and `bus_server_name`, from a
  template the check test binds to contracts.node_link.bus_server_name.

The same contracts always write the same text.

    python -m scripts.galleria_contract            # rewrite the file when it differs
    python -m scripts.galleria_contract --check    # exit 1 when the committed file differs from a fresh write
"""
from __future__ import annotations

import argparse
import keyword
import pprint
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from pydantic import TypeAdapter

from contracts import central_api, home_assistant, node_link, node_state, room_light
from nodeapi.buffers import KV_OPERATION_HEADER, state_prefix, state_stream

GENERATED: Final = (Path(__file__).resolve().parents[1] / "custom_components" / "galleria" / "contract.py")
_COMMAND: Final = "python -m scripts.galleria_contract"
_NOTES: Final = frozenset({"title", "description", "default", "discriminator", "examples"})
_KEYWORDS: Final = frozenset({
    "$ref", "anyOf", "oneOf", "const", "enum", "type", "properties", "required", "additionalProperties",
    "patternProperties", "maxProperties", "items", "prefixItems", "minItems", "maxItems", "minLength",
    "maxLength", "pattern", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
})
_SCALARS: Final = {"string": "str", "integer": "int", "number": "float", "boolean": "bool", "null": "None"}

Schema = Mapping[str, Any]

# Every wire value Galleria reads or writes, under the name its TypedDict takes. The two private
# wrappers are the bucket's and the method's whole value ({"state": ...}, {"answer": ...}).
ROOTS: Final[tuple[tuple[str, Any], ...]] = (
    *((model.__name__, model) for model in central_api.STATE_MODELS.values()),
    ("InstallPlayer", central_api.InstallPlayer),
    ("InstallAnswerValue", central_api._Answer),
    ("HomeAssistantStateValue", home_assistant._StateValue),
    ("Birth", node_state.Birth),
    ("HostBase", node_state.HostBase),
    ("FrameReports", node_state.FrameReports),
    ("LightSensorReading", room_light.LightSensorReading),
)


def constants() -> dict[str, object]:
    """Every constant the integration uses, under the name it takes in contract.py."""
    return {
        # Central's hub, through Central's origin (contracts/central_api.py)
        "DOOR_PATH": central_api.DOOR_PATH,
        "CENTRAL_ACCOUNT": central_api.CENTRAL_ACCOUNT,
        "HOME_ASSISTANT_USER": central_api.HOME_ASSISTANT_USER,
        "CENTRAL_STATE_STREAM": central_api.CENTRAL_STATE_STREAM,
        "CENTRAL_STATE_PREFIX": central_api.CENTRAL_STATE_PREFIX,
        "CENTRAL_BIRTH_KEY": central_api.BIRTH_KEY,
        "INSTALLATION_KEY": central_api.INSTALLATION_KEY,
        "LISTENING_KEY": central_api.LISTENING_KEY,
        "ROOM_LIGHTS_KEY": central_api.ROOM_LIGHTS_KEY,
        "RELEASES_KEY": central_api.RELEASES_KEY,
        "VERSIONS_KEY": central_api.VERSIONS_KEY,
        "STATES_EPOCH_KEY": central_api.STATES_EPOCH_KEY,
        "CENTRAL_STATE_TABLE": dict(central_api.CENTRAL_STATE_TABLE),
        "INSTALL_PLAYER": central_api.INSTALL_PLAYER,
        "INSTALL_PLAYER_SUBJECT": (f"{central_api.CENTRAL_COMPONENT}.{node_link.METHOD_TOKEN}."
                                   f"{central_api.INSTALL_PLAYER}"),
        "INSTALL_BYTES": central_api.INSTALL_BYTES,
        "INSTALL_REFUSALS": tuple(refusal.value for refusal in central_api.InstallRefusal),
        # Home Assistant's states (contracts/home_assistant.py)
        "HOME_ASSISTANT_STATES_PREFIX": central_api.HOME_ASSISTANT_STATES_PREFIX,
        "HOME_ASSISTANT_STATE_BYTES": home_assistant.HOME_ASSISTANT_STATE_BYTES,
        "ENTITY_ID_PATTERN": home_assistant.ENTITY_ID_PATTERN,
        "ENTITY_NAME_MAX": home_assistant.NAME_MAX,
        "LEVEL_MAX": home_assistant.LEVEL_MAX,
        "KELVIN_MIN": home_assistant.KELVIN_MIN,
        "KELVIN_MAX": home_assistant.KELVIN_MAX,
        # A Pi's read-only door and the lines read there (contracts/node_link.py, node_state.py)
        "NODE_DOOR_PORT": node_link.NODE_DOOR_PORT,
        "SERIAL_PATTERN": node_link.SERIAL_PATTERN,
        "KV_OPERATION_HEADER": KV_OPERATION_HEADER,
        "HOST_STATE_STREAM": state_stream(node_state.HOST_LINE),
        "HOST_STATE_PREFIX": state_prefix(node_state.HOST_LINE),
        "PLAYER_STATE_STREAM": state_stream(node_state.PLAYER_LINE),
        "PLAYER_STATE_PREFIX": state_prefix(node_state.PLAYER_LINE),
        "BIRTH_KEY": node_state.BIRTH_KEY,
        "BIRTH_BYTES": node_state.BIRTH_BYTES,
        "HOST_BASE_KEY": node_state.HOST_BASE_KEY,
        "HOST_BASE_BYTES": node_state.HOST_BASE_BYTES,
        "LIGHT_SENSOR_KEY": room_light.LIGHT_SENSOR_KEY,
        "LIGHT_SENSOR_BYTES": room_light.LIGHT_SENSOR_BYTES,
        "FRAMES_KEY": node_state.FRAMES_KEY,
        "FRAME_REPORTS_BYTES": node_state.FRAME_REPORTS_BYTES,
        "ROOM_LIGHTS_DRAWN_KEY": node_state.ROOM_LIGHTS_DRAWN_KEY,
        "PLAYER_COMPONENT": node_state.PLAYER_COMPONENT,
        "PLAYER_SCHEMA_MAJOR": node_state.PLAYER_SCHEMA_MAJOR,
    }


class ContractError(ValueError):
    """The schemas cannot be written as contract.py (one name, two schemas; a keyword the checker
    does not know; a field name Python cannot spell)."""


def _bare(schema: object) -> object:
    """`schema` without notes (titles, descriptions, defaults, the discriminator), refs rewritten to
    a definition's name; ContractError for a keyword the checker does not know."""
    if isinstance(schema, list):
        return [_bare(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    unknown = set(schema) - _KEYWORDS - _NOTES - {"$defs"}
    if unknown:
        raise ContractError(f"keywords the checker does not know: {sorted(unknown)}")
    out: dict[str, object] = {}
    for key, value in schema.items():
        if key in _NOTES or key == "$defs":
            continue
        if key == "$ref":
            out[key] = value.rsplit("/", 1)[1]
        elif key in ("properties", "patternProperties"):
            out[key] = {name: _bare(item) for name, item in value.items()}
        else:
            out[key] = _bare(value)
    return out


def definitions() -> dict[str, Schema]:
    """Every named schema: the roots' `$defs` and the roots themselves, as pydantic writes them
    (notes kept, so a named union keeps its title)."""
    named: dict[str, Schema] = {}
    for name, root in ROOTS:
        schema = TypeAdapter(root).json_schema(mode="validation", ref_template="#/$defs/{model}")
        for def_name, definition in {**schema.get("$defs", {}), name: schema}.items():
            definition = {key: value for key, value in definition.items() if key != "$defs"}
            if named.setdefault(def_name, definition) != definition:
                raise ContractError(f"two schemas named {def_name}")
    return named


class _Writer:
    def __init__(self, named: Mapping[str, Schema]) -> None:
        self.named = named
        self.aliases: dict[str, str] = {}

    def py(self, schema: Schema, field: str | None = None) -> str:
        """The Python type of `schema`; a titled union (not a field's own title) becomes an alias."""
        if "$ref" in schema:
            return schema["$ref"].rsplit("/", 1)[1]
        if "const" in schema:
            return f"Literal[{schema['const']!r}]"
        if "enum" in schema:
            return f"Literal[{', '.join(repr(value) for value in schema['enum'])}]"
        union = schema.get("oneOf") or schema.get("anyOf")
        if union is not None:
            text = " | ".join(dict.fromkeys(self.py(item) for item in union))
            title = schema.get("title")
            if (title and "oneOf" in schema and field is not None
                    and title != field.replace("_", " ").title().replace(" ", "")):
                if self.aliases.setdefault(title, text) != text:
                    raise ContractError(f"two unions titled {title}")
                return title
            return text
        kind = schema.get("type")
        if kind in _SCALARS:
            return _SCALARS[kind]
        if kind == "array":
            items = schema.get("prefixItems") or [schema["items"]]
            return f"list[{' | '.join(dict.fromkeys(self.py(item) for item in items))}]"
        if kind == "object" and "properties" not in schema:
            values = list((schema.get("patternProperties") or {}).values())
            if isinstance(schema.get("additionalProperties"), dict):
                values.append(schema["additionalProperties"])
            return f"dict[str, {' | '.join(dict.fromkeys(self.py(value) for value in values))}]"
        raise ContractError(f"no Python type for {schema!r}")

    def declaration(self, name: str, schema: Schema) -> list[str]:
        if "enum" in schema:
            return [f"type {name} = {self.py(schema)}"]
        if schema.get("type") != "object" or "properties" not in schema:
            raise ContractError(f"{name} is neither an object nor an enum")
        lines = [f"class {name}(TypedDict):"]
        for field, field_schema in schema["properties"].items():
            if not field.isidentifier() or keyword.iskeyword(field):
                raise ContractError(f"{name}.{field} is not a Python name")
            lines.append(f"    {field}: {self.py(field_schema, field)}")
        return lines


def galleria_contract() -> str:
    """The text of custom_components/galleria/contract.py. Deterministic. Pure."""
    named = definitions()
    writer = _Writer(named)
    classes: list[str] = []
    for name in sorted(named):
        classes.extend(writer.declaration(name, named[name]))
        classes.append("")
        classes.append("")
    aliases = [f"type {title} = {text}" for title, text in sorted(writer.aliases.items())]
    schemas = {name: _bare(schema) for name, schema in sorted(named.items())}
    state_roots = {key: model.__name__ for key, model in central_api.STATE_MODELS.items()}
    lines = [
        f'"""GENERATED by `{_COMMAND}` from `contracts`: never edit by hand.',
        "",
        "Galleria's copy of every name, key, cap and wire shape it reads or writes (stdlib only; it imports",
        "no package of the Photo Wall repository). tests/test_galleria_contract.py fails when it drifts.",
        '"""',
        "from __future__ import annotations",
        "",
        "import hashlib",
        "import json",
        "import math",
        "import re",
        "from typing import Any, Final, Literal, TypedDict",
        "",
    ]
    for name, value in constants().items():
        lines.append(f"{name}: Final = {pprint.pformat(value, width=100, sort_dicts=True)}")
    lines += ["", ""]
    lines += [*aliases, "", ""] if aliases else []
    lines += classes
    lines.append(f"_CENTRAL_STATE_ROOTS: Final = {pprint.pformat(state_roots, width=100, sort_dicts=True)}")
    lines.append(f"_SCHEMAS: Final[dict[str, Any]] = {pprint.pformat(schemas, width=110, sort_dicts=True)}")
    lines.append("")
    lines.append(_TEMPLATE)
    return "\n".join(lines)


# The checker, the codecs and bus_server_name: code, not data. Each mirrors its `contracts` twin
# (contracts/strict_json.py, contracts/wire_codec.py `encode`, contracts/node_link.py
# `bus_server_name`); tests/test_galleria_contract.py binds each to its twin.
_TEMPLATE: Final = '''
CentralStateValue = CentralBirth | Installation | Listening | RoomLightsListing | Releases | Versions | StatesEpoch


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    return value


def _constant(name: str) -> object:
    raise ValueError(f"{name} is not JSON")


def _same(value: object, expected: object) -> bool:
    return type(value) is type(expected) and value == expected


def _number(value: object) -> bool:
    return type(value) in (int, float)


_TYPES: Final = {
    "string": lambda value: type(value) is str,
    "integer": lambda value: type(value) is int,
    "number": _number,
    "boolean": lambda value: type(value) is bool,
    "null": lambda value: value is None,
    "array": lambda value: type(value) is list,
    "object": lambda value: type(value) is dict,
}


def _bounded(schema: dict[str, Any], value: object) -> bool:
    if type(value) is str:
        return (schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value))
                and ("pattern" not in schema or re.search(schema["pattern"], value) is not None))
    if _number(value):
        return (schema.get("minimum", value) <= value <= schema.get("maximum", value)
                and ("exclusiveMinimum" not in schema or value > schema["exclusiveMinimum"])
                and ("exclusiveMaximum" not in schema or value < schema["exclusiveMaximum"])
                and ("multipleOf" not in schema or value % schema["multipleOf"] == 0))
    if type(value) is list:
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", len(value)):
            return False
        if "prefixItems" in schema:
            prefix = schema["prefixItems"]
            return len(value) == len(prefix) and all(map(_valid, prefix, value))
        return "items" not in schema or all(_valid(schema["items"], item) for item in value)
    if type(value) is dict:
        if len(value) > schema.get("maxProperties", len(value)):
            return False
        if "properties" in schema:
            fields = schema["properties"]
            return set(value) == set(fields) and all(_valid(fields[name], item) for name, item in value.items())
        if "patternProperties" in schema:
            return all(any(re.search(pattern, key) and _valid(item_schema, item)
                           for pattern, item_schema in schema["patternProperties"].items())
                       for key, item in value.items())
        extra = schema.get("additionalProperties")
        return isinstance(extra, dict) and all(_valid(extra, item) for item in value.values())
    return True


def _valid(schema: dict[str, Any], value: object) -> bool:
    """Whether `value` (JSON types) meets `schema` (a _SCHEMAS entry)."""
    if "$ref" in schema:
        return _valid(_SCHEMAS[schema["$ref"]], value)
    if "anyOf" in schema:
        return any(_valid(item, value) for item in schema["anyOf"])
    if "oneOf" in schema:
        return sum(_valid(item, value) for item in schema["oneOf"]) == 1
    if "const" in schema and not _same(value, schema["const"]):
        return False
    if "enum" in schema and not any(_same(value, item) for item in schema["enum"]):
        return False
    if "type" in schema and not _TYPES[schema["type"]](value):
        return False
    return _bounded(schema, value)


def _decoded(name: str, raw: bytes, limit: int, code: str) -> Any:
    """The `name` value `raw` encodes (strict JSON of at most `limit` bytes meeting its schema);
    ValueError(code) for anything else."""
    if not isinstance(raw, bytes) or len(raw) > limit:
        raise ValueError(code)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_float=_finite,
                           parse_constant=_constant)
    except (ValueError, RecursionError) as error:
        raise ValueError(code) from error
    if not _valid(_SCHEMAS[name], value):
        raise ValueError(code)
    return value


def _encoded(name: str, value: object, limit: int, code: str, too_large: str) -> bytes:
    """Canonical JSON of `value` (sorted keys, no spaces, UTF-8, no NaN); ValueError(code) when it
    does not meet `name`'s schema, ValueError(too_large) past `limit` bytes."""
    if not _valid(_SCHEMAS[name], value):
        raise ValueError(code)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    if len(raw) > limit:
        raise ValueError(too_large)
    return raw


def decode_central_state(key: str, raw: bytes) -> CentralStateValue:
    """The value `raw` encodes under Central's state key `key`; ValueError("central_state") else."""
    if key not in _CENTRAL_STATE_ROOTS:
        raise ValueError("central_state")
    return _decoded(_CENTRAL_STATE_ROOTS[key], raw, CENTRAL_STATE_TABLE[key], "central_state")


def encode_home_assistant_state(state: HomeAssistantState) -> bytes:
    """Canonical JSON {"state": <state>}; ValueError("home_assistant_state") for an invalid state,
    ValueError("home_assistant_state_too_large") past HOME_ASSISTANT_STATE_BYTES."""
    return _encoded("HomeAssistantStateValue", {"state": state}, HOME_ASSISTANT_STATE_BYTES,
                    "home_assistant_state", "home_assistant_state_too_large")


def decode_home_assistant_state(raw: bytes) -> HomeAssistantState:
    """ValueError("home_assistant_state") for anything but a state."""
    return _decoded("HomeAssistantStateValue", raw, HOME_ASSISTANT_STATE_BYTES, "home_assistant_state")["state"]


def encode_install_player(request: InstallPlayer) -> bytes:
    """ValueError("install_player") for an invalid request."""
    return _encoded("InstallPlayer", request, INSTALL_BYTES, "install_player", "install_player_too_large")


def decode_install_answer(raw: bytes) -> InstallStarted | InstallRefused:
    """ValueError("install_answer") for anything but an InstallAnswer."""
    return _decoded("InstallAnswerValue", raw, INSTALL_BYTES, "install_answer")["answer"]


def decode_birth(raw: bytes) -> Birth:
    """ValueError("birth") for anything but a Birth."""
    return _decoded("Birth", raw, BIRTH_BYTES, "birth")


def decode_host_base(raw: bytes) -> HostBase:
    """ValueError("host_base") for anything but a HostBase."""
    return _decoded("HostBase", raw, HOST_BASE_BYTES, "host_base")


def decode_frame_reports(raw: bytes) -> FrameReports:
    """ValueError("frame_reports") for anything but FrameReports."""
    return _decoded("FrameReports", raw, FRAME_REPORTS_BYTES, "frame_reports")


def decode_light_sensor(raw: bytes) -> LightSensorReading:
    """ValueError("light_sensor_reading") for anything but a LightSensorReading."""
    return _decoded("LightSensorReading", raw, LIGHT_SENSOR_BYTES, "light_sensor_reading")


def bus_server_name(serial: str) -> str:
    """A Pi bus's server name, which its door's INFO carries: "node-N" + 24 hex of sha256(serial).
    ValueError("node_link_serial") for a serial outside SERIAL_PATTERN."""
    if type(serial) is not str or not re.fullmatch(SERIAL_PATTERN, serial):
        raise ValueError("node_link_serial")
    return "node-N" + hashlib.sha256(serial.encode()).hexdigest()[:24]
'''


def main(argv: Sequence[str] | None = None) -> int:
    """Rewrite GENERATED when it differs and return 0; with `--check`, write nothing and return 1
    when it is missing or differs (naming the command that rewrites it on stderr), else 0."""
    parser = argparse.ArgumentParser(prog=_COMMAND, description="Write Galleria's contract from `contracts`.")
    parser.add_argument("--check", action="store_true", help="exit 1 when the committed file is stale")
    check = parser.parse_args(argv).check
    text = galleria_contract()
    current = GENERATED.read_text(encoding="utf-8") if GENERATED.exists() else None
    if current == text:
        return 0
    if check:
        print(f"{GENERATED.name} differs from `contracts`; rewrite it with: {_COMMAND}", file=sys.stderr)
        return 1
    GENERATED.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
