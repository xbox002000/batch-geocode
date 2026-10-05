"""REPORT.md (short Markdown summary + table) and GEOJSON FeatureCollection builders."""
from __future__ import annotations

from typing import Any

from geo_api import KEY_HINTS


def build_geojson(rows: list[dict]) -> dict[str, Any]:
    feats = []
    for r in rows:
        if r.get("status") != "ok" or r.get("lat") is None or r.get("lng") is None:
            continue
        comps = r.get("components") or {}
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lng"], r["lat"]]},
            "properties": {
                "input": r.get("input"),
                "inputType": r.get("inputType"),
                "provider": r.get("provider"),
                "formattedAddress": r.get("formattedAddress"),
                "locationType": r.get("locationType"),
                "accuracy": r.get("accuracy"),
                "confidence": r.get("confidence"),
                "providerId": r.get("providerId"),
                "countryCode": comps.get("countryCode"),
                "locality": comps.get("locality"),
                "postalCode": comps.get("postalCode"),
            },
        })
    return {"type": "FeatureCollection", "features": feats}


def _esc(v: Any, n: int = 80) -> str:
    if v is None or v == "":
        return "—"
    s = str(v).replace("|", "\\|").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _num(v: Any, nd: int = 6) -> str:
    return "—" if v is None else f"{float(v):.{nd}f}"


def build_markdown(rows: list[dict], summary: dict, matrix_rows: list[dict] | None = None, max_rows: int = 200) -> str:
    p = summary.get("provider")
    lines = [
        "# Batch geocode report",
        "",
        f"- Provider: **{p}**",
        f"- Forward: {summary.get('forwardPlanned', 0)} · Reverse: {summary.get('reversePlanned', 0)}",
        f"- OK: **{summary.get('ok', 0)}** · Failed (free): **{summary.get('failed', 0)}** · Skipped: {summary.get('skipped', 0)}",
        f"- API key provided: {'yes' if summary.get('apiKeyProvided') else '**no**'}",
        f"- Charged events: {', '.join(f'`{k}` ×{v}' for k, v in (summary.get('charged') or {}).items()) or 'none'}",
        f"- Duration: {summary.get('durationSecs')} s",
        "",
    ]
    by = summary.get("byClass") or {}
    if by:
        lines += ["| Result | Count |", "|---|---:|"] + [f"| {_esc(k)} | {v} |" for k, v in sorted(by.items())] + [""]

    need_key = by.get("missing_key") or by.get("unauthorized") or by.get("quota_exceeded")
    if need_key:
        lines += ["## How to get an API key", ""]
        lines += [f"- **{k}**: {v}" for k, v in KEY_HINTS.items()]
        lines += ["", "Paste the key into the `apiKey` input (stored encrypted, never logged). "
                  "Rows without a working key are failed and not charged.", ""]

    lines += ["## Results", "", "| # | Input | Status | Lat | Lng | Formatted address | Type | Country | Error |",
              "|---:|---|---|---:|---:|---|---|---|---|"]
    for i, r in enumerate(rows[:max_rows], 1):
        comps = r.get("components") or {}
        lines.append(
            f"| {i} | {_esc(r.get('input'), 60)} | {r.get('status')} | {_num(r.get('lat'))} | {_num(r.get('lng'))} | "
            f"{_esc(r.get('formattedAddress'), 70)} | {_esc(r.get('locationType'), 20)} | "
            f"{_esc(comps.get('countryCode'), 4)} | {_esc(r.get('errorClass'), 24)} |"
        )
    if len(rows) > max_rows:
        lines.append(f"\n…{len(rows) - max_rows} more row(s) in the dataset.")
    hints = sorted({r.get("hint") for r in rows if r.get("hint") and r.get("errorClass") not in ("missing_key", "unauthorized")})
    if hints:
        lines += ["", "## Hints", ""] + [f"- {_esc(h, 300)}" for h in hints]

    if matrix_rows:
        lines += ["", "## Distance matrix", "", "| Origin | Destination | Status | Distance | Duration |",
                  "|---|---|---|---|---|"]
        for m in matrix_rows[:max_rows]:
            lines.append(f"| {_esc(m.get('origin'), 40)} | {_esc(m.get('destination'), 40)} | "
                         f"{m.get('status')}{'' if m.get('status') == 'ok' else ' (' + _esc(m.get('errorClass'), 20) + ')'} | "
                         f"{_esc(m.get('distanceText'))} | {_esc(m.get('durationText'))} |")
    lines += ["", "GeoJSON of successful points: key-value record `GEOJSON`."]
    return "\n".join(lines) + "\n"
