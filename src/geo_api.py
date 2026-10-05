"""Official HTTP geocoding clients: Google Geocoding API, Mapbox Geocoding v5, HERE Geocoding & Search v7.

No HTML scraping. The buyer's key is sent the only way each API accepts it (query param `key` / `access_token`
/ `apiKey`), so: httpx INFO logging is muted by the caller, request URLs are never logged, and every error string
passes through `redact()` before it can reach a dataset row, report or log.
"""
from __future__ import annotations

import asyncio
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import httpx

UA = "apify-batch-geocode/0.1 (+https://apify.com)"
PROVIDERS = ("google", "mapbox", "here")

GOOGLE_GEOCODE = "https://maps.googleapis.com/maps/api/geocode/json"
GOOGLE_MATRIX = "https://maps.googleapis.com/maps/api/distancematrix/json"
MAPBOX_PLACES = "https://api.mapbox.com/geocoding/v5/mapbox.places/{query}.json"
HERE_GEOCODE = "https://geocode.search.hereapi.com/v1/geocode"
HERE_REVGEOCODE = "https://revgeocode.search.hereapi.com/v1/revgeocode"

ENV_KEYS = {"google": "GOOGLE_GEOCODING_API_KEY", "mapbox": "MAPBOX_ACCESS_TOKEN", "here": "HERE_API_KEY"}

KEY_HINTS = {
    "google": (
        "Google needs a Maps Platform API key with the Geocoding API enabled and billing on: "
        "https://console.cloud.google.com/apis/library/geocoding-backend.googleapis.com → Enable, then "
        "APIs & Services → Credentials → Create API key (restrict it to the Geocoding API; add the Distance Matrix API "
        "only if you use the matrix option)."
    ),
    "mapbox": "Mapbox needs an access token (default public token works): https://account.mapbox.com/access-tokens/",
    "here": "HERE needs a REST API key: https://platform.here.com/ → Access manager → Apps → create app → API keys.",
}


def _test_base() -> str | None:
    """Test hook: route all provider calls to a local mock. Only localhost is accepted."""
    base = (os.getenv("GEOCODE_TEST_BASE") or "").strip().rstrip("/")
    if not base:
        return None
    host = urlparse(base).hostname
    if urlparse(base).scheme == "http" and host in ("127.0.0.1", "localhost"):
        return base
    return None


def _endpoint(url: str) -> str:
    base = _test_base()
    if not base:
        return url
    p = urlparse(url)
    return base + p.path


def redact(text: Any, *secrets: str | None) -> str:
    s = "" if text is None else str(text)
    for sec in secrets:
        if sec and len(sec) >= 4:
            s = s.replace(sec, "***")
            s = s.replace(quote(sec, safe=""), "***")
    # Defensive: scrub anything that looks like a key/token query parameter.
    s = re.sub(r"(?i)\b(key|access_token|apiKey|apikey)=[^&\s\"']+", r"\1=***", s)
    return s


# ---------------------------------------------------------------------------------------------- coordinates

_COORD_RE = re.compile(r"^\s*\(?\s*([-+]?\d+(?:\.\d+)?)\s*[,;\s]\s*([-+]?\d+(?:\.\d+)?)\s*\)?\s*$")


def parse_coordinate(raw: Any) -> tuple[float, float] | None:
    """Accept {lat,lng} / {lat,lon} / {latitude,longitude} / "lat,lng" / [lat,lng]. Return (lat, lng) or None."""
    lat = lng = None
    if isinstance(raw, dict):
        lat = raw.get("lat", raw.get("latitude"))
        lng = raw.get("lng", raw.get("lon", raw.get("long", raw.get("longitude"))))
    elif isinstance(raw, (list, tuple)) and len(raw) == 2:
        lat, lng = raw
    elif isinstance(raw, str):
        m = _COORD_RE.match(raw)
        if m:
            lat, lng = m.group(1), m.group(2)
    try:
        lat_f, lng_f = float(lat), float(lng)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat_f <= 90 and -180 <= lng_f <= 180):
        return None
    return lat_f, lng_f


def fmt_coord(c: tuple[float, float]) -> str:
    return f"{c[0]:.7g},{c[1]:.7g}"


# ---------------------------------------------------------------------------------------------- results

@dataclass
class GeoResult:
    ok: bool
    item: dict[str, Any]
    attempts: int = 0
    auth_failed: bool = False
    quota_failed: bool = False


@dataclass
class _Attempt:
    ok: bool = False
    retryable: bool = False
    item: dict[str, Any] | None = None  # parsed success fields
    error: dict[str, Any] | None = None
    retry_after: float | None = None


def _base_row(query: str, kind: str, provider: str) -> dict[str, Any]:
    return {"input": query, "inputType": kind, "provider": provider, "status": "failed"}


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers.get("Retry-After"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _http_error(provider: str, resp: httpx.Response, secret: str | None) -> _Attempt:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {}
    msg = ""
    if isinstance(body, dict):
        msg = str(body.get("message") or body.get("error_description") or body.get("title")
                  or body.get("error_message") or body.get("error") or "")
    msg = redact(msg, secret)[:300] or f"HTTP {resp.status_code}"
    code = resp.status_code
    err: dict[str, Any] = {"httpStatus": code, "errorMessage": msg}
    if code in (401, 403):
        err["errorClass"] = "unauthorized"
        err["hint"] = KEY_HINTS[provider]
        return _Attempt(error=err)
    if code == 429:
        err["errorClass"] = "rate_limited"
        err["hint"] = "Provider rate limit; lower maxConcurrency or check your plan's quota."
        return _Attempt(error=err, retryable=True, retry_after=_retry_after(resp))
    if code in (400, 404, 422):
        err["errorClass"] = "invalid_request"
        err["hint"] = "The provider rejected this query (malformed address/coordinates or unsupported parameter)."
        return _Attempt(error=err)
    if code >= 500:
        err["errorClass"] = "server_error"
        return _Attempt(error=err, retryable=True)
    err["errorClass"] = "http_error"
    return _Attempt(error=err)


# ---------------------------------------------------------------------------------------------- Google

_GOOGLE_STATUS = {
    "ZERO_RESULTS": ("zero_results", False, "No match. Check spelling or add city/country."),
    "REQUEST_DENIED": ("unauthorized", False, None),
    "OVER_QUERY_LIMIT": ("rate_limited", True, "Google per-second/per-day limit or billing issue on your project."),
    "OVER_DAILY_LIMIT": ("quota_exceeded", False, "Key invalid, billing not enabled, or a self-imposed cap was hit."),
    "INVALID_REQUEST": ("invalid_request", False, "Empty or malformed address/latlng."),
    "UNKNOWN_ERROR": ("server_error", True, None),
}


def _google_components(comps: list[dict]) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def pick(t: str, short: bool = False):
        for c in comps or []:
            if t in (c.get("types") or []):
                return c.get("short_name" if short else "long_name")
        return None

    out["country"] = pick("country")
    out["countryCode"] = pick("country", short=True)
    out["admin1"] = pick("administrative_area_level_1")
    out["admin2"] = pick("administrative_area_level_2")
    out["locality"] = pick("locality") or pick("postal_town") or pick("administrative_area_level_3")
    out["sublocality"] = pick("sublocality") or pick("neighborhood")
    out["postalCode"] = pick("postal_code")
    out["street"] = pick("route")
    out["houseNumber"] = pick("street_number")
    return out


def parse_google_result(r: dict) -> dict[str, Any]:
    loc = ((r.get("geometry") or {}).get("location")) or {}
    return {
        "lat": loc.get("lat"),
        "lng": loc.get("lng"),
        "formattedAddress": r.get("formatted_address"),
        "locationType": (r.get("geometry") or {}).get("location_type"),
        "resultTypes": r.get("types") or [],
        "partialMatch": bool(r.get("partial_match")),
        "placeId": r.get("place_id"),
        "providerId": r.get("place_id"),
        "confidence": None,  # Google does not return a score; use locationType / partialMatch
        "components": _google_components(r.get("address_components") or []),
    }


def _google_attempt(resp: httpx.Response, secret: str | None, limit: int) -> _Attempt:
    if resp.status_code != 200:
        return _http_error("google", resp, secret)
    try:
        body = resp.json()
    except Exception as e:  # noqa: BLE001
        return _Attempt(retryable=True, error={"errorClass": "bad_json", "errorMessage": str(e)[:200], "httpStatus": 200})
    status = str(body.get("status") or "")
    if status == "OK" and body.get("results"):
        results = [parse_google_result(r) for r in body["results"][:max(1, limit)]]
        return _Attempt(ok=True, item={**results[0], "providerStatus": status, "resultCount": len(body["results"]),
                                       "alternatives": results[1:]})
    cls, retry, hint = _GOOGLE_STATUS.get(status, ("zero_results" if status == "OK" else "provider_error", False, None))
    msg = redact(body.get("error_message") or status or "unknown", secret)[:300]
    err = {"errorClass": cls, "errorMessage": msg, "providerStatus": status or None, "httpStatus": 200}
    if cls == "unauthorized" or (cls == "quota_exceeded"):
        err["hint"] = KEY_HINTS["google"]
    elif hint:
        err["hint"] = hint
    return _Attempt(retryable=retry, error=err)


# ---------------------------------------------------------------------------------------------- Mapbox

def parse_mapbox_feature(f: dict) -> dict[str, Any]:
    center = f.get("center") or (f.get("geometry") or {}).get("coordinates") or [None, None]
    ctx = {}
    for c in f.get("context") or []:
        cid = str(c.get("id") or "")
        ctx[cid.split(".", 1)[0]] = c
    ptype = (f.get("place_type") or [None])[0]
    own = {"id": f.get("id"), "text": f.get("text"), "short_code": (f.get("properties") or {}).get("short_code")}
    if ptype:
        ctx.setdefault(ptype, own)

    def g(k, field="text"):
        return (ctx.get(k) or {}).get(field)

    country_code = g("country", "short_code")
    region_code = g("region", "short_code")
    props = f.get("properties") or {}
    return {
        "lat": center[1] if len(center) > 1 else None,
        "lng": center[0] if center else None,
        "formattedAddress": f.get("place_name"),
        "locationType": ptype,
        "accuracy": props.get("accuracy"),
        "resultTypes": f.get("place_type") or [],
        "mapboxId": f.get("id") or props.get("mapbox_id"),
        "providerId": f.get("id") or props.get("mapbox_id"),
        "confidence": f.get("relevance"),
        "components": {
            "country": g("country"),
            "countryCode": country_code.upper() if isinstance(country_code, str) else None,
            "admin1": g("region"),
            "admin1Code": region_code,
            "admin2": g("district"),
            "locality": g("place") or g("locality"),
            "sublocality": g("neighborhood") or (g("locality") if g("place") else None),
            "postalCode": g("postcode"),
            "street": f.get("text") if ptype == "address" else None,
            "houseNumber": f.get("address") if ptype == "address" else None,
        },
    }


def _mapbox_attempt(resp: httpx.Response, secret: str | None, limit: int) -> _Attempt:
    if resp.status_code != 200:
        return _http_error("mapbox", resp, secret)
    try:
        body = resp.json()
    except Exception as e:  # noqa: BLE001
        return _Attempt(retryable=True, error={"errorClass": "bad_json", "errorMessage": str(e)[:200], "httpStatus": 200})
    feats = body.get("features") or []
    if not feats:
        return _Attempt(error={"errorClass": "zero_results", "errorMessage": "No features returned", "httpStatus": 200,
                               "providerStatus": "ZERO_RESULTS", "hint": "No match. Check spelling or add city/country."})
    results = [parse_mapbox_feature(f) for f in feats[:max(1, limit)]]
    return _Attempt(ok=True, item={**results[0], "providerStatus": "OK", "resultCount": len(feats),
                                   "alternatives": results[1:]})


# ---------------------------------------------------------------------------------------------- HERE

def parse_here_item(it: dict) -> dict[str, Any]:
    pos = it.get("position") or {}
    addr = it.get("address") or {}
    scoring = it.get("scoring") or {}
    return {
        "lat": pos.get("lat"),
        "lng": pos.get("lng"),
        "formattedAddress": addr.get("label") or it.get("title"),
        "locationType": it.get("resultType"),
        "accuracy": it.get("houseNumberType"),
        "resultTypes": [t for t in [it.get("resultType")] if t],
        "hereId": it.get("id"),
        "providerId": it.get("id"),
        "confidence": scoring.get("queryScore"),
        "components": {
            "country": addr.get("countryName"),
            "countryCode": addr.get("countryCode"),  # ISO 3166-1 alpha-3 for HERE
            "admin1": addr.get("state"),
            "admin1Code": addr.get("stateCode"),
            "admin2": addr.get("county"),
            "locality": addr.get("city"),
            "sublocality": addr.get("district"),
            "postalCode": addr.get("postalCode"),
            "street": addr.get("street"),
            "houseNumber": addr.get("houseNumber"),
        },
    }


def _here_attempt(resp: httpx.Response, secret: str | None, limit: int) -> _Attempt:
    if resp.status_code != 200:
        return _http_error("here", resp, secret)
    try:
        body = resp.json()
    except Exception as e:  # noqa: BLE001
        return _Attempt(retryable=True, error={"errorClass": "bad_json", "errorMessage": str(e)[:200], "httpStatus": 200})
    items = body.get("items") or []
    if not items:
        return _Attempt(error={"errorClass": "zero_results", "errorMessage": "No items returned", "httpStatus": 200,
                               "providerStatus": "ZERO_RESULTS", "hint": "No match. Check spelling or add city/country."})
    results = [parse_here_item(i) for i in items[:max(1, limit)]]
    return _Attempt(ok=True, item={**results[0], "providerStatus": "OK", "resultCount": len(items),
                                   "alternatives": results[1:]})


# ---------------------------------------------------------------------------------------------- request builders

def build_request(provider: str, kind: str, query: str, coord: tuple[float, float] | None, *, api_key: str,
                  language: str | None, region: str | None, mapbox_country: str | None, limit: int) -> tuple[str, dict]:
    """Return (url, params). `kind` is 'forward' or 'reverse'."""
    if provider == "google":
        params: dict[str, Any] = {"key": api_key}
        if kind == "reverse":
            params["latlng"] = fmt_coord(coord)  # type: ignore[arg-type]
        else:
            params["address"] = query
            if region:
                params["region"] = region
        if language:
            params["language"] = language
        return _endpoint(GOOGLE_GEOCODE), params
    if provider == "mapbox":
        if kind == "reverse":
            path_q = f"{coord[1]:.7g},{coord[0]:.7g}"  # type: ignore[index]  # Mapbox wants lng,lat
            params = {"access_token": api_key, "limit": 1}
        else:
            path_q = quote(query.replace(";", ","), safe="")
            params = {"access_token": api_key, "limit": max(1, min(limit, 10)), "autocomplete": "false"}
        if mapbox_country:
            params["country"] = mapbox_country
        if language:
            params["language"] = language
        return _endpoint(MAPBOX_PLACES.format(query=path_q)), params
    if provider == "here":
        params = {"apiKey": api_key, "limit": max(1, min(limit, 100))}
        if language:
            params["lang"] = language
        if kind == "reverse":
            params["at"] = fmt_coord(coord)  # type: ignore[arg-type]
            return _endpoint(HERE_REVGEOCODE), params
        params["q"] = query
        return _endpoint(HERE_GEOCODE), params
    raise ValueError(f"Unknown provider {provider!r}")


_PARSERS = {"google": _google_attempt, "mapbox": _mapbox_attempt, "here": _here_attempt}


async def geocode_one(
    client: httpx.AsyncClient,
    provider: str,
    query: str,
    *,
    api_key: str | None,
    kind: str = "forward",
    coord: tuple[float, float] | None = None,
    language: str | None = None,
    region: str | None = None,
    mapbox_country: str | None = None,
    limit: int = 1,
    max_retries: int = 3,
    timeout: float = 30.0,
    backoff_base: float = 1.0,
) -> GeoResult:
    base = _base_row(query, kind, provider)
    t0 = time.time()
    if not api_key:
        return GeoResult(False, {**base, "errorClass": "missing_key", "httpStatus": None, "attempts": 0,
                                 "errorMessage": f"No API key for provider '{provider}' (input `apiKey`). Not called; not charged.",
                                 "hint": KEY_HINTS[provider], "durationMs": 0})
    if kind == "forward" and not query.strip():
        return GeoResult(False, {**base, "errorClass": "invalid_input", "errorMessage": "Empty address", "attempts": 0})
    if kind == "reverse" and coord is None:
        return GeoResult(False, {**base, "errorClass": "invalid_input", "attempts": 0,
                                 "errorMessage": "Invalid coordinate; expected 'lat,lng' or {lat,lng} within range"})
    url, params = build_request(provider, kind, query, coord, api_key=api_key, language=language, region=region,
                                mapbox_country=mapbox_country, limit=limit)
    parse = _PARSERS[provider]
    attempts = 0
    last: _Attempt = _Attempt()
    while True:
        attempts += 1
        try:
            resp = await client.get(url, params=params, timeout=timeout)
        except httpx.TimeoutException:
            last = _Attempt(retryable=True, error={"errorClass": "timeout", "httpStatus": None,
                                                    "errorMessage": f"No answer within {timeout:.0f}s"})
        except httpx.HTTPError as e:
            last = _Attempt(retryable=True, error={"errorClass": "network", "httpStatus": None,
                                                    "errorMessage": redact(f"{type(e).__name__}: {e}", api_key)[:300]})
        else:
            last = parse(resp, api_key, limit)
            if last.ok:
                row = {**base, **(last.item or {}), "status": "ok", "httpStatus": 200, "attempts": attempts,
                       "errorClass": None, "errorMessage": None, "hint": None,
                       "durationMs": int((time.time() - t0) * 1000)}
                if kind == "reverse":
                    row["queryLat"], row["queryLng"] = coord  # type: ignore[misc]
                return GeoResult(True, row, attempts)
        if not last.retryable or attempts > max_retries:
            err = dict(last.error or {"errorClass": "error", "errorMessage": "unknown"})
            err["errorMessage"] = redact(err.get("errorMessage"), api_key)
            row = {**base, **err, "attempts": attempts, "durationMs": int((time.time() - t0) * 1000)}
            if kind == "reverse" and coord:
                row["queryLat"], row["queryLng"] = coord
            return GeoResult(False, row, attempts, auth_failed=err.get("errorClass") == "unauthorized",
                             quota_failed=err.get("errorClass") == "quota_exceeded")
        delay = last.retry_after if last.retry_after is not None else backoff_base * (2 ** (attempts - 1))
        await asyncio.sleep(min(max(delay, 0) + (random.uniform(0, 0.5) if backoff_base else 0), 30))


# ---------------------------------------------------------------------------------------------- Distance Matrix

def chunk_matrix(n_orig: int, n_dest: int, max_elements: int = 100, max_side: int = 25) -> list[tuple[range, range]]:
    """Split an origins×destinations grid into requests within Google's 25-per-side / 100-elements limits."""
    out: list[tuple[range, range]] = []
    d_step = min(max_side, n_dest, max_elements) or 1
    for d0 in range(0, n_dest, d_step):
        d_rng = range(d0, min(d0 + d_step, n_dest))
        o_step = max(1, min(max_side, max_elements // len(d_rng)))
        for o0 in range(0, n_orig, o_step):
            out.append((range(o0, min(o0 + o_step, n_orig)), d_rng))
    return out


async def distance_matrix(
    client: httpx.AsyncClient,
    origins: list[str],
    destinations: list[str],
    *,
    api_key: str | None,
    mode: str = "driving",
    units: str = "metric",
    language: str | None = None,
    region: str | None = None,
    max_retries: int = 3,
    timeout: float = 30.0,
    backoff_base: float = 1.0,
) -> tuple[list[dict], dict | None]:
    """Return (element rows, fatal_error). Each row has status ok/failed; only ok rows are billable."""
    rows: list[dict] = []
    if not api_key:
        fatal = {"errorClass": "missing_key", "errorMessage": "No API key; Distance Matrix not called.",
                 "hint": KEY_HINTS["google"]}
        return [{"origin": o, "destination": d, "status": "failed", **fatal, "attempts": 0}
                for o in origins for d in destinations], fatal
    url = _endpoint(GOOGLE_MATRIX)
    fatal: dict | None = None
    for o_rng, d_rng in chunk_matrix(len(origins), len(destinations)):
        o_list = [origins[i] for i in o_rng]
        d_list = [destinations[i] for i in d_rng]
        params: dict[str, Any] = {"origins": "|".join(o_list), "destinations": "|".join(d_list), "mode": mode,
                                  "units": units, "key": api_key}
        if language:
            params["language"] = language
        if region:
            params["region"] = region
        attempts, body, err = 0, None, None
        while True:
            attempts += 1
            retry = False
            try:
                resp = await client.get(url, params=params, timeout=timeout)
            except httpx.TimeoutException:
                err, retry = {"errorClass": "timeout", "errorMessage": f"No answer within {timeout:.0f}s"}, True
            except httpx.HTTPError as e:
                err, retry = {"errorClass": "network", "errorMessage": redact(f"{type(e).__name__}: {e}", api_key)[:300]}, True
            else:
                if resp.status_code != 200:
                    a = _http_error("google", resp, api_key)
                    err, retry = a.error, a.retryable
                else:
                    try:
                        body = resp.json()
                    except Exception as e:  # noqa: BLE001
                        err, retry, body = {"errorClass": "bad_json", "errorMessage": str(e)[:200]}, True, None
                    else:
                        st = str(body.get("status") or "")
                        if st == "OK":
                            err = None
                        else:
                            cls, retry, hint = _GOOGLE_STATUS.get(st, ("provider_error", False, None))
                            err = {"errorClass": cls, "providerStatus": st,
                                   "errorMessage": redact(body.get("error_message") or st, api_key)[:300]}
                            if cls in ("unauthorized", "quota_exceeded"):
                                err["hint"] = ("Enable the Distance Matrix API for this key: "
                                               "https://console.cloud.google.com/apis/library/distance-matrix-backend.googleapis.com")
                            body = None
            if err is None or not retry or attempts > max_retries:
                break
            await asyncio.sleep(min(backoff_base * (2 ** (attempts - 1)), 30))
        if err is not None:
            for o in o_list:
                for d in d_list:
                    rows.append({"origin": o, "destination": d, "status": "failed", **err, "attempts": attempts})
            if err.get("errorClass") in ("unauthorized", "quota_exceeded"):
                fatal = err
                break
            continue
        o_addr = body.get("origin_addresses") or []
        d_addr = body.get("destination_addresses") or []
        for i, r in enumerate(body.get("rows") or []):
            for j, el in enumerate(r.get("elements") or []):
                st = el.get("status")
                row = {"origin": o_list[i] if i < len(o_list) else None,
                       "destination": d_list[j] if j < len(d_list) else None,
                       "originAddress": o_addr[i] if i < len(o_addr) else None,
                       "destinationAddress": d_addr[j] if j < len(d_addr) else None,
                       "elementStatus": st, "mode": mode, "attempts": attempts}
                if st == "OK":
                    row.update({"status": "ok", "distanceMeters": (el.get("distance") or {}).get("value"),
                                "distanceText": (el.get("distance") or {}).get("text"),
                                "durationSecs": (el.get("duration") or {}).get("value"),
                                "durationText": (el.get("duration") or {}).get("text"),
                                "durationInTrafficSecs": (el.get("duration_in_traffic") or {}).get("value"),
                                "errorClass": None})
                else:
                    row.update({"status": "failed", "errorClass": "zero_results" if st in ("ZERO_RESULTS", "NOT_FOUND")
                                else "element_error", "errorMessage": f"Element status {st}"})
                rows.append(row)
    if fatal:  # fill the not-attempted pairs so the grid is complete
        seen = {(r["origin"], r["destination"]) for r in rows}
        for o in origins:
            for d in destinations:
                if (o, d) not in seen:
                    rows.append({"origin": o, "destination": d, "status": "failed", **fatal, "attempts": 0,
                                 "skipped": True})
    return rows, fatal
