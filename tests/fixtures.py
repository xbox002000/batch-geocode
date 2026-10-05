"""Canned provider responses (shapes copied from the official API docs)."""

GOOGLE_OK = {
    "status": "OK",
    "results": [{
        "formatted_address": "Pariser Platz, 10117 Berlin, Germany",
        "place_id": "ChIJiQnyVcZRqEcRY0xnhE77uyY",
        "types": ["establishment", "point_of_interest", "tourist_attraction"],
        "geometry": {"location": {"lat": 52.5162746, "lng": 13.3777041}, "location_type": "ROOFTOP"},
        "address_components": [
            {"long_name": "Pariser Platz", "short_name": "Pariser Platz", "types": ["route"]},
            {"long_name": "Mitte", "short_name": "Mitte", "types": ["sublocality_level_1", "sublocality", "political"]},
            {"long_name": "Berlin", "short_name": "Berlin", "types": ["locality", "political"]},
            {"long_name": "Berlin", "short_name": "BE", "types": ["administrative_area_level_1", "political"]},
            {"long_name": "Germany", "short_name": "DE", "types": ["country", "political"]},
            {"long_name": "10117", "short_name": "10117", "types": ["postal_code"]},
        ],
    }, {
        "formatted_address": "Brandenburg Gate, Berlin, Germany",
        "place_id": "ALT_PLACE",
        "types": ["route"],
        "geometry": {"location": {"lat": 52.5163, "lng": 13.3778}, "location_type": "GEOMETRIC_CENTER"},
        "address_components": [],
    }],
}
GOOGLE_ZERO = {"status": "ZERO_RESULTS", "results": []}
GOOGLE_DENIED = {"status": "REQUEST_DENIED", "results": [],
                 "error_message": "The provided API key is invalid. "}
GOOGLE_OVER = {"status": "OVER_QUERY_LIMIT", "results": [], "error_message": "You have exceeded your rate-limit."}

MAPBOX_OK = {
    "type": "FeatureCollection",
    "features": [{
        "id": "address.4356035406756260",
        "type": "Feature",
        "place_type": ["address"],
        "relevance": 0.96,
        "properties": {"accuracy": "rooftop"},
        "text": "Fifth Avenue",
        "address": "350",
        "place_name": "350 Fifth Avenue, New York, New York 10118, United States",
        "center": [-73.98566, 40.748441],
        "geometry": {"type": "Point", "coordinates": [-73.98566, 40.748441]},
        "context": [
            {"id": "neighborhood.2103290", "text": "Midtown"},
            {"id": "postcode.13482670360296810", "text": "10118"},
            {"id": "place.15278078705964500", "text": "New York"},
            {"id": "region.17349986251855570", "short_code": "US-NY", "text": "New York"},
            {"id": "country.9053006287256050", "short_code": "us", "text": "United States"},
        ],
    }],
}
MAPBOX_EMPTY = {"type": "FeatureCollection", "features": []}

HERE_OK = {
    "items": [{
        "title": "Invalidenstraße 116, 10115 Berlin, Deutschland",
        "id": "here:af:streetsection:tVuvjJYhVvLEY3HG3bBDYB:CggIBCCi-9SPARABGgMxMTY",
        "resultType": "houseNumber",
        "houseNumberType": "PA",
        "address": {"label": "Invalidenstraße 116, 10115 Berlin, Deutschland", "countryCode": "DEU",
                    "countryName": "Deutschland", "stateCode": "BE", "state": "Berlin", "countyCode": "B",
                    "county": "Berlin", "city": "Berlin", "district": "Mitte", "street": "Invalidenstraße",
                    "postalCode": "10115", "houseNumber": "116"},
        "position": {"lat": 52.53041, "lng": 13.38527},
        "scoring": {"queryScore": 1.0, "fieldScore": {"city": 1.0, "streets": [1.0], "houseNumber": 1.0}},
    }],
}

MATRIX_OK = {
    "status": "OK",
    "origin_addresses": ["Berlin, Germany"],
    "destination_addresses": ["Hamburg, Germany", "Atlantis"],
    "rows": [{"elements": [
        {"status": "OK", "distance": {"text": "289 km", "value": 289123}, "duration": {"text": "3 hours 5 mins", "value": 11100}},
        {"status": "NOT_FOUND"},
    ]}],
}
