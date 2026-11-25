"""Map between the device's TSL thing-model and local TTLV field ids.

The cloud ``productTSL`` endpoint returns every property with its numeric
id, code, data type and (for STRUCT/ARRAY) the sub-field ids and codes. Local
transports only see numeric ids, so this map translates local reads into the
same ``{code: value}`` dictionary the cloud path produces, and translates
writes from code to id.

The static fallback tables come from pecron-monitor (MIT) and are only used
when the cached TSL has no entry for an id.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .ttlv import Field, encode_bool_field, encode_num_field

_LOGGER = logging.getLogger(__name__)

FALLBACK_TOP: dict[int, str] = {
    1: "battery_percentage",
    2: "remain_time",
    3: "remain_charging_time",
    4: "total_input_power",
    5: "total_output_power",
    27: "ups_status_hm",
    28: "dc_data_input_hm",
    29: "ac_data_input_hm",
    30: "dc_data_output_hm",
    31: "ac_data_output_hm",
    35: "host_packet_data_jdb",
    36: "charging_pack_data_jdb",
    37: "device_status_hm",
    38: "dc_switch_hm",
    40: "ac_switch_hm",
    100: "high_frequency_reporting",
}

FALLBACK_SUB: dict[str, dict[int, str]] = {
    "host_packet_data_jdb": {
        1: "host_packet_electric_percentage",
        2: "host_packet_voltage",
        3: "host_packet_current",
        4: "host_packet_temp",
        5: "host_packet_status",
    },
    "ac_data_output_hm": {
        1: "ac_output_hz",
        2: "ac_output_voltage",
        3: "ac_output_pf",
        4: "ac_output_power",
    },
    "dc_data_output_hm": {1: "dc_output_power"},
    "ac_data_input_hm": {1: "ac_power"},
    "dc_data_input_hm": {1: "dc_input_power"},
    "charging_pack_data_jdb": {
        1: "charging_pack_num",
        2: "charging_pack_status",
        3: "charging_pack_voltage",
        4: "charging_pack_current",
        5: "charging_pack_temp",
        6: "charging_pack_battery",
    },
}


@dataclass
class TslEntry:
    """One TSL property, reduced to what the transports need."""

    pid: int
    code: str
    data_type: str
    writable: bool
    decimals: int = 0
    sub: dict[int, str] = field(default_factory=dict)


def _type_of(item: dict) -> str:
    dt = item.get("dataType", "")
    if isinstance(dt, dict):
        dt = dt.get("type", "")
    return str(dt).upper()


def _decimals_from_specs(specs: Any) -> int:
    if not isinstance(specs, dict):
        return 0
    step = str(specs.get("step", "") or "")
    if "." in step:
        return len(step.split(".", 1)[1].rstrip("0")) or 0
    return 0


def _find_sub_fields(specs: Any) -> dict[int, str]:
    """Locate the sub-field id->code list inside STRUCT/ARRAY specs (shape varies)."""
    found: dict[int, str] = {}
    if isinstance(specs, str):
        try:
            specs = json.loads(specs)
        except ValueError:
            return found
    if isinstance(specs, list):
        for item in specs:
            if isinstance(item, dict) and "id" in item and "code" in item:
                try:
                    found[int(item["id"])] = str(item["code"])
                except (TypeError, ValueError):
                    continue
            elif isinstance(item, dict | list):
                found.update(_find_sub_fields(item))
    elif isinstance(specs, dict):
        for value in specs.values():
            if isinstance(value, dict | list | str):
                found.update(_find_sub_fields(value))
    return found


class TslMap:
    """Bidirectional id <-> code map for one product."""

    def __init__(self, raw_properties: list[dict] | None) -> None:
        self.by_id: dict[int, TslEntry] = {}
        self.by_code: dict[str, TslEntry] = {}
        for item in raw_properties or []:
            try:
                pid = int(item["id"])
                code = str(item.get("code") or item.get("resourceCode"))
            except (KeyError, TypeError, ValueError):
                continue
            sub_type = str(item.get("subType", item.get("accessMode", "R"))).upper()
            specs = item.get("specs")
            entry = TslEntry(
                pid=pid,
                code=code,
                data_type=_type_of(item),
                writable=sub_type in ("RW", "W"),
                decimals=_decimals_from_specs(specs),
                sub=_find_sub_fields(specs),
            )
            self.by_id[pid] = entry
            self.by_code[code] = entry

    def __bool__(self) -> bool:
        return bool(self.by_id)

    def code_for(self, pid: int) -> str | None:
        entry = self.by_id.get(pid)
        return entry.code if entry else FALLBACK_TOP.get(pid)

    def sub_for(self, code: str, sid: int) -> str:
        entry = self.by_code.get(code)
        if entry and sid in entry.sub:
            return entry.sub[sid]
        return FALLBACK_SUB.get(code, {}).get(sid, f"field_{sid}")

    def type_for(self, code: str) -> str:
        entry = self.by_code.get(code)
        return entry.data_type if entry else ""

    # -- encoding ----------------------------------------------------------

    def encode_write(self, code: str, value: Any) -> bytes:
        """Build the plaintext TTLV payload for writing one property."""
        entry = self.by_code.get(code)
        if entry is None:
            raise KeyError(f"{code} is not in this device's TSL")
        if entry.data_type == "BOOL":
            if isinstance(value, str):
                value = value.lower() in ("true", "on", "1", "yes")
            return encode_bool_field(entry.pid, bool(value))
        if entry.data_type in ("FLOAT", "DOUBLE"):
            decimals = entry.decimals or (1 if float(value) != int(float(value)) else 0)
            return encode_num_field(entry.pid, float(value), decimals)
        if entry.data_type in ("INT", "ENUM"):
            return encode_num_field(entry.pid, int(float(value)))
        raise ValueError(f"writing {entry.data_type} property {code} locally is not supported")

    # -- decoding ----------------------------------------------------------

    def decode(self, fields: list[Field]) -> dict[str, Any]:
        """Turn a flat TTLV field list into ``{code: value}`` (structs as dicts, arrays as lists)."""
        out: dict[str, Any] = {}
        i = 0
        while i < len(fields):
            f = fields[i]
            code = self.code_for(f.fid)
            if f.kind == "STRUCT":
                children, i = _collect(fields, i + 1, f.value)
                if code is None:
                    continue
                if any(c[0].kind == "STRUCT" for c in children) or self.type_for(code) == "ARRAY":
                    out[code] = [
                        {self.sub_for(code, gc.fid): _plain(gc) for gc, _ in grand}
                        for c, grand in children
                        if c.kind == "STRUCT"
                    ]
                else:
                    out[code] = {self.sub_for(code, c.fid): _plain(c) for c, _ in children}
                continue
            i += 1
            if code is not None:
                out[code] = _plain(f)
        return out


def _collect(fields: list[Field], start: int, count: int) -> tuple[list[tuple[Field, list]], int]:
    """Collect ``count`` direct children starting at ``start``; nested structs recurse."""
    children: list[tuple[Field, list]] = []
    i = start
    while i < len(fields) and len(children) < count:
        f = fields[i]
        if f.kind == "STRUCT":
            grand, i = _collect(fields, i + 1, f.value)
            children.append((f, grand))
        else:
            children.append((f, []))
            i += 1
    return children, i


def _plain(f: Field) -> Any:
    if f.kind == "BYTES":
        try:
            return f.value.decode("utf-8")
        except UnicodeDecodeError:
            return f.value.hex()
    return f.value


# ---------------------------------------------------------------------------
# Conversion to the cloud ``customizeTslInfo`` shape
# ---------------------------------------------------------------------------


def _to_wire_str(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict | list):
        return json.dumps(value)
    return str(value)


def to_tsl_info(values: dict[str, Any], tsl: TslMap) -> list[dict]:
    """Render ``{code: value}`` as the cloud's ``customizeTslInfo`` list.

    This lets every transport feed the same ``DeviceProperties`` parser and
    keeps the entity platforms transport-agnostic.
    """
    items = []
    for code, value in values.items():
        dtype = tsl.type_for(code)
        if not dtype:
            if isinstance(value, bool):
                dtype = "BOOL"
            elif isinstance(value, dict):
                dtype = "STRUCT"
            elif isinstance(value, list):
                dtype = "ARRAY"
            elif isinstance(value, float):
                dtype = "FLOAT"
            elif isinstance(value, int):
                dtype = "INT"
            else:
                dtype = "TEXT"
        items.append(
            {"resourceCode": code, "resourceValce": _to_wire_str(value), "dataType": dtype}
        )
    return items


def from_tsl_info(items: list[dict]) -> dict[str, Any]:
    """Parse the cloud's ``customizeTslInfo`` list into ``{code: value}``."""
    out: dict[str, Any] = {}
    for item in items or []:
        code = item.get("resourceCode")
        if not code:
            continue
        raw = item.get("resourceValce")
        dtype = str(item.get("dataType", "")).upper()
        value: Any = raw
        try:
            if dtype in ("STRUCT", "ARRAY") and isinstance(raw, str):
                value = json.loads(raw)
            elif dtype == "BOOL" and isinstance(raw, str):
                value = raw.lower() == "true"
            elif dtype in ("INT", "ENUM") and isinstance(raw, str) and raw.lstrip("-").isdigit():
                value = int(raw)
            elif dtype in ("FLOAT", "DOUBLE") and isinstance(raw, str):
                value = float(raw)
        except ValueError:
            value = raw
        out[code] = value
    return out
