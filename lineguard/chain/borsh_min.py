"""Minimal Anchor/Borsh encoder driven by the vendored Txoracle IDL.

Supports exactly the type surface `validate_odds` needs: integer scalars,
bool, string, bytes-arrays, vec, option, and nested defined structs.
IDL (txoracle_idl.json) is vendored verbatim from txodds/tx-on-chain.
"""
from __future__ import annotations

import json
import struct
from functools import lru_cache
from pathlib import Path

_IDL_PATH = Path(__file__).with_name("txoracle_idl.json")


@lru_cache(maxsize=1)
def idl() -> dict:
    return json.loads(_IDL_PATH.read_text())


@lru_cache(maxsize=None)
def _type_def(name: str) -> dict:
    for t in idl()["types"]:
        if t["name"] == name:
            return t["type"]
    raise KeyError(f"type {name} not in IDL")


_SCALARS = {"u8": "<B", "u16": "<H", "u32": "<I", "u64": "<Q",
            "i8": "<b", "i16": "<h", "i32": "<i", "i64": "<q"}


def encode(ty, value) -> bytes:
    if isinstance(ty, str):
        if ty in _SCALARS:
            return struct.pack(_SCALARS[ty], int(value))
        if ty == "bool":
            return b"\x01" if value else b"\x00"
        if ty == "string":
            b = str(value).encode()
            return struct.pack("<I", len(b)) + b
        raise TypeError(f"unsupported scalar {ty}")
    if "array" in ty:
        inner, n = ty["array"]
        vals = list(value)
        assert len(vals) == n, f"array length {len(vals)} != {n}"
        return b"".join(encode(inner, v) for v in vals)
    if "vec" in ty:
        vals = list(value)
        return struct.pack("<I", len(vals)) + b"".join(encode(ty["vec"], v) for v in vals)
    if "option" in ty:
        return b"\x00" if value is None else b"\x01" + encode(ty["option"], value)
    if "defined" in ty:
        tdef = _type_def(ty["defined"]["name"])
        assert tdef["kind"] == "struct", "only struct defined-types supported"
        out = b""
        for f in tdef["fields"]:
            out += encode(f["type"], value[f["name"]])
        return out
    raise TypeError(f"unsupported type {ty}")


def encode_instruction(ix_name: str, args: dict) -> bytes:
    ix = next(i for i in idl()["instructions"] if i["name"] == ix_name)
    data = bytes(ix["discriminator"])
    for a in ix["args"]:
        data += encode(a["type"], args[a["name"]])
    return data
