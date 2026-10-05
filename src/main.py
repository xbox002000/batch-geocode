"""Apify Actor: batch geocoding via OFFICIAL HTTP APIs (Google Geocoding, Mapbox Geocoding v5, HERE Geocoding & Search v7).

Bring your own provider key. Forward (addresses) and reverse (coordinates) geocoding, optional Google Distance Matrix.
Output: one dataset row per address/coordinate + REPORT.md, GEOJSON, SUMMARY, OUTPUT (+ MATRIX when enabled).
Pay-per-event: `geocode-result` per successful geocode, `matrix-element` per successful matrix element.
Everything that fails (missing key, REQUEST_DENIED/401/403, 429 after retries, ZERO_RESULTS, invalid input) is free.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import resource
import time
from collections import Counter

import httpx
from apify import Actor

from charging import Charger
from geo_api import ENV_KEYS, KEY_HINTS, PROVIDERS, UA, distance_matrix, fmt_coord, geocode_one, parse_coordinate
from inputs import address_from_item, coord_from_item, normalize_addresses, normalize_coordinates, split_payload
from report import build_geojson, build_markdown

EVENT_GEOCODE = "geocode-result"
EVENT_MATRIX = "matrix-element"
TRAVEL_MODES = ("driving", "walking", "bicycling", "transit")

# httpx logs full request URLs (which carry the provider key as a query param) at INFO. Never let that happen.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


def _int(inp: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = inp.get(key)
    try:
        v = int(default if v is None or v == "" else v)
    except (TypeError, ValueError):
        v = default
    return max(lo, min(v, hi))


def _str(inp: dict, key: str) -> str | None:
    v = inp.get(key)
    return str(v).strip() or None if v is not None else None


async def load_from_dataset(dataset_id: str, address_field: str | None, limit: int | None) -> tuple[list[str], list, list[str]]:
    addrs: list[str] = []
    coords: list = []
    try:
        ds = await Actor.open_dataset(id=dataset_id, force_cloud=bool(os.getenv("APIFY_IS_AT_HOME")))
        async for item in ds.iterate_items():
            if not isinstance(item, dict):
                continue
            a = address_from_item(item, address_field)
            if a:
                addrs.append(a)
            else:
                c = coord_from_item(item)
                if c:
                    coords.append({"lat": c[0], "lng": c[1]})
            if limit and len(addrs) + len(coords) >= limit:
                break
    except Exception as e:  # noqa: BLE001
        return [], [], [f"Could not read dataset {dataset_id}: {e}"]
    return addrs, coords, ([] if addrs or coords else [f"No address/lat-lng fields found in dataset {dataset_id}"])


async def load_from_kv(store_id: str, record_key: str, address_field: str | None) -> tuple[list[str], list, list[str]]:
    try:
        store = await Actor.open_key_value_store(id=store_id, force_cloud=bool(os.getenv("APIFY_IS_AT_HOME")))
        rec = await store.get_value(record_key)
    except Exception as e:  # noqa: BLE001
        return [], [], [f"Could not read KV {store_id}/{record_key}: {e}"]
    if rec is None:
        return [], [], [f"KV record {record_key} not found in store {store_id}"]
    a, c = split_payload(rec, address_field)
    return a, c, ([] if a or c else [f"No addresses found in KV record {record_key}"])


def _points(values) -> list[str]:
    """Distance-matrix origins/destinations: addresses or coordinates → strings Google accepts."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [v for v in values.splitlines() if v.strip()]
    out: list[str] = []
    for v in values:
        c = parse_coordinate(v) if not isinstance(v, str) or "," in v else None
        s = fmt_coord(c) if c else (str(v).strip() if isinstance(v, str) else "")
        if s and s not in out:
            out.append(s)
    return out


async def main() -> None:
    async with Actor:
        t0 = time.time()
        inp = await Actor.get_input() or {}
        charger = Charger(EVENT_GEOCODE)

        provider = str(inp.get("provider") or "google").strip().lower()
        if provider not in PROVIDERS:
            raise ValueError(f"`provider` must be one of {PROVIDERS}, got {provider!r}")

        api_key = (inp.get("apiKey") or "").strip() or None
        key_source = "input" if api_key else None
        if not api_key:
            env_name = ENV_KEYS[provider]
            if os.getenv(env_name, "").strip():
                api_key, key_source = os.environ[env_name].strip(), f"env:{env_name}"

        language = _str(inp, "language")
        region = _str(inp, "region")
        mapbox_country = _str(inp, "mapboxCountry")
        limit = _int(inp, "limit", 1, 1, 10)
        max_items = _int(inp, "maxAddresses", 100, 0, 100000) or None
        conc = _int(inp, "maxConcurrency", 2, 1, 5)
        max_retries = _int(inp, "maxRetries", 3, 0, 6)
        timeout = float(_int(inp, "requestTimeoutSecs", 30, 5, 120))
        stop_on_auth = bool(inp.get("stopOnAuthError", True))
        address_field = _str(inp, "addressField")

        addresses = normalize_addresses(inp.get("addresses"))
        raw_coords: list = list(inp.get("coordinates") or []) if not isinstance(inp.get("coordinates"), str) \
            else [ln for ln in str(inp["coordinates"]).splitlines() if ln.strip()]
        warnings: list[str] = []
        if inp.get("datasetId"):
            a, c, w = await load_from_dataset(str(inp["datasetId"]), address_field, max_items)
            addresses += a
            raw_coords += c
            warnings += w
            Actor.log.info(f"Loaded {len(a)} address(es) + {len(c)} coordinate(s) from dataset {inp['datasetId']}")
        if inp.get("keyValueStoreId"):
            key = inp.get("keyValueRecordKey") or "INPUT_ADDRESSES"
            a, c, w = await load_from_kv(str(inp["keyValueStoreId"]), str(key), address_field)
            addresses += a
            raw_coords += c
            warnings += w
            Actor.log.info(f"Loaded {len(a)} address(es) + {len(c)} coordinate(s) from KV {inp['keyValueStoreId']}/{key}")
        addresses = normalize_addresses(addresses)
        coords, bad_coords = normalize_coordinates(raw_coords)
        for w in warnings:
            Actor.log.warning(w)

        do_matrix = bool(inp.get("enableDistanceMatrix", False))
        origins, destinations = _points(inp.get("origins")), _points(inp.get("destinations"))
        max_matrix = _int(inp, "maxMatrixElements", 100, 1, 2500)
        if do_matrix and provider != "google":
            Actor.log.warning("enableDistanceMatrix only works with provider=google; matrix skipped.")
            do_matrix = False
        if do_matrix and (not origins or not destinations):
            Actor.log.warning("enableDistanceMatrix needs both `origins` and `destinations`; matrix skipped.")
            do_matrix = False
        if do_matrix and len(origins) * len(destinations) > max_matrix:
            keep_d = max(1, min(len(destinations), max_matrix))
            destinations = destinations[:keep_d]
            origins = origins[: max(1, max_matrix // keep_d)]
            Actor.log.warning(f"Matrix capped to {len(origins)}×{len(destinations)} by maxMatrixElements={max_matrix}.")

        jobs: list[tuple[str, str, tuple[float, float] | None]] = [("forward", a, None) for a in addresses]
        jobs += [("reverse", fmt_coord(c), c) for c in coords]
        jobs += [("reverse", b, None) for b in bad_coords]  # become free invalid_input rows
        if max_items is not None and len(jobs) > max_items:
            Actor.log.warning(f"{len(jobs)} inputs; capped to maxAddresses={max_items}.")
            jobs = jobs[:max_items]
        if not jobs and not do_matrix:
            raise ValueError("Provide at least one entry in `addresses` or `coordinates` (or a datasetId / "
                             "keyValueStoreId with addresses, or a Distance Matrix with origins+destinations).")

        if not api_key:
            Actor.log.warning(
                f"No apiKey for provider '{provider}'. Nothing will be sent to the provider; every row is marked "
                f"missing_key and NOT charged. {KEY_HINTS[provider]}"
            )
        else:
            Actor.log.info(f"Provider {provider}; API key from {key_source} (value hidden).")
        Actor.log.info(f"{len(jobs)} geocode job(s); concurrency={conc}; limit/result={limit}; matrix={do_matrix}")
        await Actor.set_status_message(f"Geocoding {len(jobs)} input(s) with {provider}…")

        sem = asyncio.Semaphore(conc)
        lock = asyncio.Lock()
        results: list[tuple[int, dict]] = []
        cls_counts: Counter[str] = Counter()
        state = {"done": 0, "ok": 0, "fail": 0, "skipped": 0, "inflight": 0, "auth_failed": False}
        matrix_rows: list[dict] = []
        matrix_ok_charged = 0

        async with httpx.AsyncClient(headers={"User-Agent": UA, "Accept": "application/json"},
                                     follow_redirects=False, limits=httpx.Limits(max_connections=conc + 2)) as client:

            async def one(idx: int, kind: str, query: str, coord) -> None:
                async with sem:
                    async with lock:
                        if charger.limit_reached:
                            state["skipped"] += 1
                            return
                        b = charger.budget()
                        if b is not None and b - state["inflight"] <= 0:
                            state["skipped"] += 1
                            charger.limit_reached = True
                            return
                        skip_auth = stop_on_auth and state["auth_failed"]
                        state["inflight"] += 1
                    try:
                        if skip_auth:
                            res_ok, item = False, {
                                "input": query, "inputType": kind, "provider": provider, "status": "failed",
                                "errorClass": "unauthorized", "attempts": 0, "skipped": True,
                                "errorMessage": "Not sent: the provider rejected the API key on an earlier request.",
                                "hint": KEY_HINTS[provider]}
                        else:
                            res = await geocode_one(client, provider, query, api_key=api_key, kind=kind, coord=coord,
                                                    language=language, region=region, mapbox_country=mapbox_country,
                                                    limit=limit, max_retries=max_retries, timeout=timeout)
                            res_ok, item = res.ok, res.item
                            if res.auth_failed or res.quota_failed:
                                state["auth_failed"] = True
                    finally:
                        async with lock:
                            state["inflight"] -= 1
                    item["checkedAt"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
                    async with lock:
                        state["done"] += 1
                        if res_ok:
                            n = await charger.push_and_charge([item], event=EVENT_GEOCODE)
                            if n:
                                state["ok"] += 1
                                cls_counts["ok"] += 1
                                results.append((idx, item))
                            else:
                                state["skipped"] += 1
                        else:
                            state["fail"] += 1
                            cls_counts[item.get("errorClass") or "error"] += 1
                            results.append((idx, item))
                            await charger.push_free(item)
                        if res_ok or state["done"] <= 20 or state["done"] % 25 == 0:
                            Actor.log.info(
                                f"[{state['done']}/{len(jobs)}] {kind} {query[:60]!r}: "
                                + (f"OK {item.get('lat')},{item.get('lng')} {str(item.get('formattedAddress'))[:60]!r}"
                                   if res_ok else f"FAILED {item.get('errorClass')} (free)")
                            )
                        if state["done"] % 10 == 0 or state["done"] == len(jobs):
                            await Actor.set_status_message(
                                f"{state['done']}/{len(jobs)} — ok {state['ok']}, failed {state['fail']} (free)")

            await asyncio.gather(*(one(i, k, q, c) for i, (k, q, c) in enumerate(jobs)))

            if do_matrix:
                if stop_on_auth and state["auth_failed"]:
                    Actor.log.warning("Skipping Distance Matrix: the key was rejected during geocoding.")
                    matrix_rows = [{"origin": o, "destination": d, "status": "failed", "errorClass": "unauthorized",
                                    "skipped": True, "errorMessage": "Not sent: key rejected earlier."}
                                   for o in origins for d in destinations]
                else:
                    Actor.log.info(f"Distance Matrix {len(origins)}×{len(destinations)} ({inp.get('travelMode') or 'driving'})")
                    mode = str(inp.get("travelMode") or "driving").lower()
                    mode = mode if mode in TRAVEL_MODES else "driving"
                    units = "imperial" if str(inp.get("units") or "").lower() == "imperial" else "metric"
                    matrix_rows, fatal = await distance_matrix(client, origins, destinations, api_key=api_key,
                                                               mode=mode, units=units, language=language,
                                                               region=region, max_retries=max_retries, timeout=timeout)
                    if fatal:
                        Actor.log.warning(f"Distance Matrix failed: {fatal.get('errorClass')} — {fatal.get('errorMessage')} (free)")

        rows = [r for _, r in sorted(results, key=lambda t: t[0])]
        store = await Actor.open_key_value_store()
        matrix_ok = sum(1 for m in matrix_rows if m.get("status") == "ok")
        if matrix_rows:
            await store.set_value("MATRIX", matrix_rows)
            if matrix_ok:
                b = charger.budget(EVENT_MATRIX)
                matrix_ok_charged = matrix_ok if b is None else min(matrix_ok, b)
                if matrix_ok_charged < matrix_ok:
                    Actor.log.warning(f"Spending limit: charging {matrix_ok_charged}/{matrix_ok} matrix elements.")
                await charger.charge(EVENT_MATRIX, count=matrix_ok_charged)

        peak_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
        need_key = bool(cls_counts.get("missing_key") or cls_counts.get("unauthorized") or cls_counts.get("quota_exceeded"))
        summary = {
            "provider": provider,
            "forwardPlanned": sum(1 for j in jobs if j[0] == "forward"),
            "reversePlanned": sum(1 for j in jobs if j[0] == "reverse"),
            "ok": state["ok"],
            "failed": state["fail"],
            "skipped": state["skipped"],
            "byClass": dict(cls_counts),
            "apiKeyProvided": bool(api_key),
            "keyHint": KEY_HINTS[provider] if need_key else None,
            "matrixElements": len(matrix_rows),
            "matrixOk": matrix_ok,
            "charged": dict(charger.counts),
            "spendingLimitReached": charger.limit_reached,
            "durationSecs": round(time.time() - t0, 1),
            "peakMemoryMb": peak_mb,
            "finishedAt": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }
        geo = build_geojson(rows)
        summary["geojsonFeatures"] = len(geo["features"])
        # Serialize ourselves: the SDK only JSON-encodes values whose content type is application/json.
        await store.set_value("GEOJSON", json.dumps(geo, ensure_ascii=False), content_type="application/geo+json; charset=utf-8")
        await store.set_value("REPORT.md", build_markdown(rows, summary, matrix_rows),
                              content_type="text/markdown; charset=utf-8")
        await store.set_value("SUMMARY", summary)
        await store.set_value("OUTPUT", summary)
        msg = (f"Done: {state['ok']} geocoded, {state['fail']} failed (free), {state['skipped']} skipped"
               + (f", matrix {matrix_ok}/{len(matrix_rows)} OK" if matrix_rows else "")
               + f". Charged: {charger.counts or 'nothing'}.")
        if need_key and not state["ok"]:
            msg += f" Supply a working `apiKey` for {provider} — see REPORT.md for how to get one."
        Actor.log.info(msg + f" Peak memory {peak_mb} MB, {summary['durationSecs']} s.")
        await Actor.set_status_message(msg, is_terminal=True)


if __name__ == "__main__":
    asyncio.run(main())
