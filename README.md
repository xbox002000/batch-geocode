# Batch Geocode — Google / Mapbox / HERE → GeoJSON

Turn a list of addresses into **latitude / longitude** (forward geocoding) or coordinates into **addresses** (reverse geocoding) using the **official** geocoding APIs of Google Maps Platform, Mapbox or HERE — with **your own API key**. Get a clean dataset row per input, a **GeoJSON FeatureCollection** ready for any map tool, and a short **Markdown report**. Optional **Google Distance Matrix** (origin × destination distance and travel time).

- ✅ Official HTTP APIs only — no Google Maps page scraping, no headless browser
- ✅ Bring your own key: you pay your provider directly at your provider's rates and quotas
- ✅ 256 MB default memory, fast and cheap to run
- ✅ **Not-found and failed rows are free** (ZERO_RESULTS, invalid address, missing/rejected key, rate limit after retries)
- ✅ GeoJSON + Markdown + JSON summary in the key-value store

## Who is it for?

Logistics and delivery teams, store-locator and real-estate data, CRM / CDP address cleaning, data pipelines that need coordinates for maps, BI tools or spatial joins.

## Providers

| `provider` | API called | Key you need |
|---|---|---|
| `google` (default) | Google Geocoding API `maps.googleapis.com/maps/api/geocode/json` | Maps Platform API key with **Geocoding API** enabled (billing on) |
| `mapbox` | Mapbox Geocoding v5 `api.mapbox.com/geocoding/v5/mapbox.places` | Mapbox access token |
| `here` | HERE Geocoding & Search v7 `geocode.search.hereapi.com/v1/geocode` (+ `revgeocode`) | HERE REST API key |

### How to get a key

1. **Google Maps Platform**
   1. Open <https://console.cloud.google.com/apis/library/geocoding-backend.googleapis.com>, pick or create a project and click **Enable**.
   2. Link a **billing account** to the project (<https://console.cloud.google.com/billing>). Google requires billing for the Geocoding API even if you stay within the free monthly usage; without it every request returns `REQUEST_DENIED`.
   3. *APIs & Services → Credentials → Create credentials → API key*.
   4. Under **API restrictions**, restrict the key to **Geocoding API** (add **Distance Matrix API** only if you use the matrix option: <https://console.cloud.google.com/apis/library/distance-matrix-backend.googleapis.com>). Do not add HTTP-referrer restrictions — requests come from Apify servers, not a browser.
   5. Optional but recommended: set a **budget alert** (*Billing → Budgets & alerts*) and per-day **quotas** (*APIs & Services → Geocoding API → Quotas*) so a large batch can never surprise you. Current prices and free usage: <https://mapsplatform.google.com/pricing/>.
2. **Mapbox** — sign in at <https://account.mapbox.com/access-tokens/> and copy the **default public token** (or create a token with the default public scopes). No credit card is needed for the free tier; see <https://www.mapbox.com/pricing> for Geocoding limits.
3. **HERE** — sign up at <https://platform.here.com/>, then *Access manager → Apps → Register new app → API keys → Create API key* (the REST **API key**, not OAuth credentials). Pricing and free tier: <https://www.here.com/get-started/pricing>.

Common key errors (all free, shown as `unauthorized` with a `hint`): Google API not enabled or billing missing (`REQUEST_DENIED`), Mapbox token revoked (HTTP 401), HERE key for a different app/project (HTTP 401/403).

Paste it into **Provider API key** (`apiKey`). It is stored encrypted by Apify, never logged and never written to the output. Without a key, the Actor sends nothing to the provider and marks every row `missing_key` (free), with these instructions in `REPORT.md`.

## Input

| Field | Description |
|---|---|
| `addresses` | Addresses to forward-geocode, one per line |
| `coordinates` | Optional: `"lat,lng"` strings or `{"lat": .., "lng": ..}` objects to reverse-geocode |
| `datasetId` / `keyValueStoreId` + `keyValueRecordKey` / `addressField` | Optional: read the list from another Actor's dataset or a key-value record |
| `provider` | `google` \| `mapbox` \| `here` |
| `apiKey` | Your provider key (secret) |
| `language`, `region` (Google ccTLD bias), `mapboxCountry` | Optional provider tuning |
| `limit` | Matches per input (best one in the row, others in `alternatives`, no extra charge) |
| `enableDistanceMatrix`, `origins`, `destinations`, `travelMode`, `units`, `maxMatrixElements` | Optional Google Distance Matrix |
| `maxAddresses` (100), `maxConcurrency` (2, max 5), `maxRetries` (3), `requestTimeoutSecs` (30), `stopOnAuthError` (true) | Limits |

Example:

```json
{
  "provider": "google",
  "apiKey": "YOUR_KEY",
  "addresses": ["Brandenburger Tor, Pariser Platz, 10117 Berlin", "350 Fifth Avenue, New York, NY 10118"],
  "coordinates": ["48.8584,2.2945"],
  "language": "en"
}
```

## Output

**Dataset** — one row per address / coordinate:

```json
{
  "input": "Brandenburger Tor, Pariser Platz, 10117 Berlin",
  "inputType": "forward",
  "provider": "google",
  "status": "ok",
  "lat": 52.5162746,
  "lng": 13.3777041,
  "formattedAddress": "Pariser Platz, 10117 Berlin, Germany",
  "locationType": "ROOFTOP",
  "placeId": "ChIJiQnyVcZRqEcRY0xnhE77uyY",
  "providerId": "ChIJiQnyVcZRqEcRY0xnhE77uyY",
  "confidence": null,
  "components": {"country": "Germany", "countryCode": "DE", "admin1": "Berlin", "locality": "Berlin", "postalCode": "10117", "street": "Pariser Platz"},
  "errorClass": null
}
```

- `locationType` / `accuracy`: Google `location_type` (ROOFTOP, RANGE_INTERPOLATED, …), Mapbox `place_type` + `accuracy`, HERE `resultType` + `houseNumberType`.
- `confidence`: Mapbox `relevance` or HERE `queryScore` (Google has no score; use `locationType` and `partialMatch`).
- IDs: `placeId` (Google), `mapboxId` (Mapbox), `hereId` (HERE), and `providerId` for all.
- Failed rows have `status: "failed"`, `errorClass` (`missing_key`, `unauthorized`, `quota_exceeded`, `rate_limited`, `zero_results`, `invalid_request`, `invalid_input`, `timeout`, `network`, `server_error`) and a `hint`.

**Key-value store**

| Key | Content |
|---|---|
| `GEOJSON` | FeatureCollection of successful points (`[lng, lat]`) |
| `REPORT.md` | Counts, result table, hints, how to get a key |
| `SUMMARY` / `OUTPUT` | ok / failed / byClass / charged / duration / peak memory |
| `MATRIX` | Distance Matrix elements (only when enabled) |

## Pricing (pay per event)

| Event | Price |
|---|---|
| Actor start | $0.001 per run |
| `geocode-result` | **$0.002** per successful forward or reverse geocode ($2 per 1,000) |
| `matrix-element` | $0.004 per successful Distance Matrix element (optional) |

Not charged: missing key, REQUEST_DENIED / 401 / 403, OVER_DAILY_LIMIT, 429 after retries, ZERO_RESULTS / not found, invalid input, timeouts. Your provider bills its own API usage to your key separately (Google, Mapbox and HERE all have free monthly tiers).

## Reliability

- 429 / `OVER_QUERY_LIMIT` / 5xx / `UNKNOWN_ERROR` / timeouts are retried with exponential backoff (honours `Retry-After`).
- After the provider rejects the key, remaining inputs are marked failed without being sent (`stopOnAuthError`), so a typo in the key doesn't burn your quota.
- Duplicate addresses (case/whitespace-insensitive) and coordinates are geocoded once.

## Terms of use

You call the provider with your own key, so your provider's terms apply to the results (for example, Google Maps Platform terms limit caching and require results to be used with a Google map when displayed on a map; Mapbox temporary geocoding results may not be stored permanently). Check your provider's terms for your use case.

## FAQ

**Does it scrape Google Maps?** No. Only the documented HTTP geocoding endpoints, authenticated with your key.

**Why do I need my own key?** Geocoding providers require a billing account per user. You keep your provider's free tier, quotas and pricing.

**What happens without a key?** Nothing is sent; each row is `missing_key`, free, and `REPORT.md` explains how to get a key.
