"""Normalize address / coordinate lists from input, datasets and key-value records.

Addresses contain commas, so free text is split on NEW LINES only (never on commas).
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

from geo_api import fmt_coord, parse_coordinate

ADDRESS_FIELDS = ("address", "fullAddress", "full_address", "query", "location", "formattedAddress", "addressLine")
COORD_FIELD_PAIRS = (("lat", "lng"), ("lat", "lon"), ("latitude", "longitude"))


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in re.split(r"[\r\n]+", text) if ln.strip()]


def normalize_addresses(values: Iterable[Any] | str | None, address_field: str | None = None) -> list[str]:
    """Dedupe (case/whitespace-insensitive) preserving order."""
    if values is None:
        return []
    if isinstance(values, str):
        values = _lines(values)
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if isinstance(v, dict):
            v = address_from_item(v, address_field)
        s = re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""
        if not s:
            continue
        key = s.casefold()
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


def normalize_coordinates(values: Iterable[Any] | str | None) -> tuple[list[tuple[float, float]], list[str]]:
    """Return (unique coords, invalid raw strings)."""
    if values is None:
        return [], []
    if isinstance(values, str):
        values = _lines(values)
    out: list[tuple[float, float]] = []
    bad: list[str] = []
    seen: set[str] = set()
    for v in values:
        c = parse_coordinate(v)
        if c is None:
            raw = json.dumps(v) if not isinstance(v, str) else v
            if raw.strip():
                bad.append(raw[:80])
            continue
        k = fmt_coord(c)
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out, bad


def address_from_item(item: dict, address_field: str | None = None) -> str | None:
    fields = (address_field,) + ADDRESS_FIELDS if address_field else ADDRESS_FIELDS
    for f in fields:
        v = item.get(f) if f else None
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def coord_from_item(item: dict) -> tuple[float, float] | None:
    for a, b in COORD_FIELD_PAIRS:
        if item.get(a) is not None and item.get(b) is not None:
            return parse_coordinate({"lat": item.get(a), "lng": item.get(b)})
    return None


def split_payload(payload: Any, address_field: str | None = None) -> tuple[list[str], list[Any]]:
    """KV record → (addresses, raw coordinates). Accepts list, newline text, or {addresses:[...], coordinates:[...]}."""
    if payload is None:
        return [], []
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    if isinstance(payload, str):
        s = payload.strip()
        if s.startswith(("[", "{")):
            try:
                return split_payload(json.loads(s), address_field)
            except ValueError:
                pass
        addrs, coords = [], []
        for ln in _lines(s):
            (coords if parse_coordinate(ln) else addrs).append(ln)
        return addrs, coords
    if isinstance(payload, dict):
        a = payload.get("addresses") or []
        c = payload.get("coordinates") or []
        if not a and not c:
            one = address_from_item(payload, address_field)
            return ([one] if one else []), []
        return (normalize_addresses(a, address_field), list(c) if isinstance(c, list) else _lines(str(c)))
    if isinstance(payload, list):
        addrs, coords = [], []
        for v in payload:
            if isinstance(v, dict):
                a = address_from_item(v, address_field)
                if a:
                    addrs.append(a)
                elif coord_from_item(v):
                    coords.append(v)
            elif isinstance(v, str):
                (coords if parse_coordinate(v) else addrs).append(v)
            elif isinstance(v, (list, tuple)) and parse_coordinate(v):
                coords.append(list(v))
        return addrs, coords
    return [], []
