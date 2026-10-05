"""Unit tests with mocked HTTP (httpx.MockTransport). No network, no real keys."""
import asyncio
import json
import logging

import httpx
import pytest

import geo_api
from fixtures import GOOGLE_DENIED, GOOGLE_OK, GOOGLE_OVER, GOOGLE_ZERO, HERE_OK, MAPBOX_EMPTY, MAPBOX_OK, MATRIX_OK
from geo_api import chunk_matrix, distance_matrix, geocode_one, parse_coordinate, redact
from inputs import normalize_addresses, normalize_coordinates, split_payload
from report import build_geojson, build_markdown

SECRET = "FAKE_TEST_KEY_not_a_real_key_123456"


def run(coro):
    return asyncio.run(coro)


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def geo(handler, provider="google", query="Brandenburger Tor", **kw):
    async def go():
        async with client_for(handler) as c:
            kw.setdefault("api_key", SECRET)
            return await geocode_one(c, provider, query, backoff_base=0, **kw)
    return run(go())


def test_google_forward_ok_parses_fields_and_alternatives():
    seen = {}

    def h(req):
        seen["url"] = str(req.url)
        return httpx.Response(200, json=GOOGLE_OK)

    r = geo(h, limit=2, region="de", language="en")
    assert r.ok and r.item["status"] == "ok"
    it = r.item
    assert it["lat"] == 52.5162746 and it["lng"] == 13.3777041
    assert it["placeId"] == "ChIJiQnyVcZRqEcRY0xnhE77uyY" and it["locationType"] == "ROOFTOP"
    assert it["components"]["countryCode"] == "DE" and it["components"]["postalCode"] == "10117"
    assert it["components"]["locality"] == "Berlin" and it["components"]["admin1"] == "Berlin"
    assert len(it["alternatives"]) == 1 and it["resultCount"] == 2
    assert req_host(seen["url"]) == "maps.googleapis.com" and "region=de" in seen["url"] and "language=en" in seen["url"]


def req_host(url):
    return httpx.URL(url).host


def test_google_reverse_uses_latlng():
    seen = {}

    def h(req):
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json=GOOGLE_OK)

    r = geo(h, query="52.5163,13.3777", kind="reverse", coord=(52.5163, 13.3777))
    assert r.ok and r.item["inputType"] == "reverse" and r.item["queryLat"] == 52.5163
    assert seen["params"]["latlng"] == "52.5163,13.3777" and "address" not in seen["params"]


def test_google_zero_results_is_failed_not_retried():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(200, json=GOOGLE_ZERO)

    r = geo(h, query="nowhere 999")
    assert not r.ok and r.item["errorClass"] == "zero_results" and r.item["providerStatus"] == "ZERO_RESULTS"
    assert len(calls) == 1 and not r.auth_failed


def test_google_request_denied_redacts_and_flags_auth():
    def h(req):
        body = dict(GOOGLE_DENIED)
        body["error_message"] = f"The provided API key is invalid: {SECRET} url=...?key={SECRET}"
        return httpx.Response(200, json=body)

    r = geo(h)
    assert not r.ok and r.auth_failed and r.item["errorClass"] == "unauthorized"
    assert SECRET not in json.dumps(r.item)
    assert "console.cloud.google.com" in r.item["hint"]


def test_google_over_query_limit_retries_then_ok():
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return httpx.Response(200, json=GOOGLE_OVER if n["c"] < 3 else GOOGLE_OK)

    r = geo(h, max_retries=3)
    assert r.ok and r.attempts == 3


def test_429_exhausts_retries_free_failure():
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return httpx.Response(429, json={"message": "Too Many Requests"}, headers={"Retry-After": "0"})

    r = geo(h, provider="mapbox", max_retries=2)
    assert not r.ok and r.item["errorClass"] == "rate_limited" and n["c"] == 3


def test_missing_key_makes_no_request():
    def h(req):  # pragma: no cover - must not be called
        raise AssertionError("request sent without key")

    for p in ("google", "mapbox", "here"):
        r = geo(h, provider=p, api_key=None)
        assert not r.ok and r.item["errorClass"] == "missing_key" and r.item["attempts"] == 0
        assert r.item["hint"]


def test_mapbox_forward_ok_and_lnglat_order():
    seen = {}

    def h(req):
        seen["url"] = req.url
        return httpx.Response(200, json=MAPBOX_OK)

    r = geo(h, provider="mapbox", query="350 Fifth Avenue, New York", mapbox_country="us", language="en")
    it = r.item
    assert r.ok and it["lat"] == 40.748441 and it["lng"] == -73.98566
    assert it["mapboxId"] == "address.4356035406756260" and it["confidence"] == 0.96 and it["accuracy"] == "rooftop"
    assert it["components"]["countryCode"] == "US" and it["components"]["postalCode"] == "10118"
    assert it["components"]["locality"] == "New York" and it["components"]["houseNumber"] == "350"
    assert seen["url"].host == "api.mapbox.com" and seen["url"].params["country"] == "us"
    assert "/geocoding/v5/mapbox.places/350%20Fifth%20Avenue%2C%20New%20York.json" in str(seen["url"])


def test_mapbox_reverse_path_is_lng_lat():
    seen = {}

    def h(req):
        seen["path"] = req.url.path
        return httpx.Response(200, json=MAPBOX_OK)

    geo(h, provider="mapbox", query="40.7484,-73.9857", kind="reverse", coord=(40.7484, -73.9857))
    assert seen["path"].endswith("/-73.9857,40.7484.json")


def test_mapbox_empty_and_401():
    r = geo(lambda req: httpx.Response(200, json=MAPBOX_EMPTY), provider="mapbox", query="nowhere")
    assert r.item["errorClass"] == "zero_results"
    r = geo(lambda req: httpx.Response(401, json={"message": "Not Authorized - Invalid Token"}), provider="mapbox")
    assert r.item["errorClass"] == "unauthorized" and r.auth_failed and r.item["httpStatus"] == 401
    assert "account.mapbox.com" in r.item["hint"]


def test_here_forward_and_reverse():
    seen = []

    def h(req):
        seen.append(req.url)
        return httpx.Response(200, json=HERE_OK)

    r = geo(h, provider="here", query="Invalidenstr 116 Berlin", language="de")
    it = r.item
    assert r.ok and it["lat"] == 52.53041 and it["hereId"].startswith("here:af:") and it["confidence"] == 1.0
    assert it["components"]["countryCode"] == "DEU" and it["locationType"] == "houseNumber"
    assert seen[0].host == "geocode.search.hereapi.com" and seen[0].params["lang"] == "de"
    geo(h, provider="here", query="52.53,13.38", kind="reverse", coord=(52.53, 13.38))
    assert seen[1].host == "revgeocode.search.hereapi.com" and seen[1].params["at"] == "52.53,13.38"


def test_here_403_unauthorized_and_empty():
    r = geo(lambda req: httpx.Response(403, json={"error": "Forbidden", "error_description": "These credentials do not authorize access"}), provider="here")
    assert r.item["errorClass"] == "unauthorized" and "platform.here.com" in r.item["hint"]
    r = geo(lambda req: httpx.Response(200, json={"items": []}), provider="here", query="nowhere")
    assert r.item["errorClass"] == "zero_results"


def test_network_error_redacted():
    def h(req):
        raise httpx.ConnectError(f"boom {req.url}")

    r = geo(h, max_retries=1)
    assert r.item["errorClass"] == "network" and SECRET not in r.item["errorMessage"]
    assert "key=***" in r.item["errorMessage"]


def test_redact_helper():
    s = redact(f"x {SECRET} y access_token=tok_fake_abc123 apiKey=zzz", SECRET)
    assert SECRET not in s and "tok_fake_abc123" not in s and "zzz" not in s


def test_httpx_logging_muted_by_main():
    import main  # noqa: F401  (import sets log levels)
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_coordinate_parsing_and_input_normalization():
    assert parse_coordinate("52.5, 13.4") == (52.5, 13.4)
    assert parse_coordinate({"lat": "1", "lon": 2}) == (1.0, 2.0)
    assert parse_coordinate("91,0") is None and parse_coordinate("abc") is None
    coords, bad = normalize_coordinates(["52.5,13.4", {"lat": 52.5, "lng": 13.4}, "999,1"])
    assert coords == [(52.5, 13.4)] and bad == ["999,1"]
    assert normalize_addresses("A St, City\na st,  city\nB Rd, Town") == ["A St, City", "B Rd, Town"]
    a, c = split_payload([{"address": "X, Y"}, {"lat": 1, "lng": 2}, "3,4", "Main St, Z"])
    assert a == ["X, Y", "Main St, Z"] and len(c) == 2


def test_distance_matrix_ok_and_not_found():
    def h(req):
        assert req.url.params["key"] == SECRET
        return httpx.Response(200, json=MATRIX_OK)

    async def go():
        async with client_for(h) as c:
            return await distance_matrix(c, ["Berlin"], ["Hamburg", "Atlantis"], api_key=SECRET, backoff_base=0)
    rows, fatal = run(go())
    assert fatal is None and [r["status"] for r in rows] == ["ok", "failed"]
    assert rows[0]["distanceMeters"] == 289123 and rows[1]["errorClass"] == "zero_results"


def test_distance_matrix_denied_is_fatal_and_free():
    async def go():
        async with client_for(lambda req: httpx.Response(200, json={**GOOGLE_DENIED, "rows": []})) as c:
            return await distance_matrix(c, ["A", "B"], ["C"], api_key=SECRET, backoff_base=0)
    rows, fatal = run(go())
    assert fatal["errorClass"] == "unauthorized" and len(rows) == 2 and all(r["status"] == "failed" for r in rows)


def test_chunk_matrix_limits():
    chunks = chunk_matrix(30, 30)
    assert all(len(o) <= 25 and len(d) <= 25 and len(o) * len(d) <= 100 for o, d in chunks)
    assert sum(len(o) * len(d) for o, d in chunks) == 900


def test_geojson_and_markdown():
    ok = geo(lambda req: httpx.Response(200, json=GOOGLE_OK)).item
    bad = geo(lambda req: httpx.Response(200, json=GOOGLE_ZERO), query="nowhere").item
    miss = geo(lambda req: None, api_key=None).item
    gj = build_geojson([ok, bad, miss])
    assert gj["type"] == "FeatureCollection" and len(gj["features"]) == 1
    assert gj["features"][0]["geometry"]["coordinates"] == [13.3777041, 52.5162746]
    md = build_markdown([ok, bad, miss], {"provider": "google", "ok": 1, "failed": 2,
                                          "byClass": {"ok": 1, "zero_results": 1, "missing_key": 1}})
    assert "| 1 | Brandenburger Tor | ok | 52.516275 | 13.377704 |" in md
    assert "How to get an API key" in md and "account.mapbox.com" in md and SECRET not in md


def test_test_base_only_localhost(monkeypatch):
    monkeypatch.setenv("GEOCODE_TEST_BASE", "http://evil.example.com")
    assert geo_api._endpoint(geo_api.HERE_GEOCODE) == geo_api.HERE_GEOCODE
    monkeypatch.setenv("GEOCODE_TEST_BASE", "http://127.0.0.1:8767")
    assert geo_api._endpoint(geo_api.HERE_GEOCODE) == "http://127.0.0.1:8767/v1/geocode"
