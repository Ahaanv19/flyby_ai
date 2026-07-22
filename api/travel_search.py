"""
Flyby AI — travel inventory search.

Generates the flight, hotel and ground-transport options the search UI renders.
Results are **deterministic for a given query**: the random generator is seeded
from the search parameters, so typing the same search twice returns the same
options, and a price the user saw is still there when they go to book it.

Field names match the ``SearchFlightResult`` / ``SearchHotelResult`` /
``SearchGroundResult`` types the frontend declares, so results render without
any translation step.
"""

import hashlib
import random

AIRLINES = [
    ("AA", "American Airlines", "🦅"), ("DL", "Delta Air Lines", "🔺"),
    ("UA", "United Airlines", "🌐"), ("WN", "Southwest Airlines", "❤️"),
    ("B6", "JetBlue Airways", "💙"), ("AS", "Alaska Airlines", "🏔️"),
    ("NK", "Spirit Airlines", "💛"), ("F9", "Frontier Airlines", "🦌"),
    ("HA", "Hawaiian Airlines", "🌺"), ("BA", "British Airways", "🇬🇧"),
    ("LH", "Lufthansa", "🇩🇪"), ("AF", "Air France", "🇫🇷"),
    ("KL", "KLM", "🇳🇱"), ("EK", "Emirates", "🇦🇪"),
    ("QR", "Qatar Airways", "🇶🇦"), ("SQ", "Singapore Airlines", "🇸🇬"),
    ("CX", "Cathay Pacific", "🇭🇰"), ("JL", "Japan Airlines", "🇯🇵"),
    ("NH", "All Nippon Airways", "🇯🇵"), ("QF", "Qantas", "🇦🇺"),
]

HOTEL_CHAINS = [
    "Marriott", "Hilton", "Hyatt", "IHG", "Wyndham", "Best Western",
    "Choice Hotels", "AccorHotels", "Radisson", "Four Seasons",
    "Ritz-Carlton", "St. Regis", "W Hotels", "Westin", "Sheraton",
]

HOTEL_SUFFIXES = ["Hotel", "Suites", "Resort", "Inn", "Lodge", "Plaza"]
AREAS = ["Downtown", "Midtown", "Financial District", "Airport", "Convention Center"]
AMENITIES = [
    "Free Wi-Fi", "Gym", "Pool", "Spa", "Restaurant", "Bar",
    "Room Service", "Business Center", "Free Breakfast", "Pet Friendly",
]
ROOM_TYPES = ["King Room", "Double Queen", "Executive Suite", "Standard Room"]
CANCELLATION = [
    "Free cancellation until 24h before check-in",
    "Free cancellation until 48h before check-in",
    "Non-refundable — best available rate",
]

GROUND_PROVIDERS = [
    ("Rideshare", "Uber", 25, ["Convenient", "Popular"]),
    ("Rideshare", "Lyft", 23, ["Best value", "Reliable"]),
    ("Rental Car", "Hertz", 55, ["Flexibility", "Popular"]),
    ("Rental Car", "Enterprise", 50, ["Great service"]),
    ("Public Transit", "Metro", 5, ["Budget", "Eco-friendly"]),
    ("Taxi", "Yellow Cab", 35, ["Traditional", "Metered"]),
]

DEPARTURE_TIMES = [
    "6:00 AM", "7:30 AM", "9:00 AM", "10:30 AM", "12:00 PM",
    "1:30 PM", "3:00 PM", "4:30 PM", "6:00 PM", "7:30 PM",
]

STOP_CITIES = ["DEN", "ORD", "ATL", "DFW", "CLT"]

# Stable photo set for hotel cards (the frontend expects an images array).
HOTEL_IMAGES = [
    "https://images.unsplash.com/photo-1566073771259-6a8506099945?w=800",
    "https://images.unsplash.com/photo-1551882547-ff40c63fe5fa?w=800",
    "https://images.unsplash.com/photo-1520250497591-112f2f40a3f4?w=800",
    "https://images.unsplash.com/photo-1590490360182-c33d57733427?w=800",
]


def _seeded_random(*parts):
    """Return a Random seeded from the query, so a search is reproducible."""
    key = "|".join(str(p or "") for p in parts)
    digest = hashlib.sha256(key.encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def _minutes_to_label(minutes):
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _add_minutes(time_label, minutes):
    """Advance a 12-hour clock label like '9:00 AM' by ``minutes``."""
    try:
        clock, meridiem = time_label.split(" ")
        hour, minute = (int(part) for part in clock.split(":"))
    except (ValueError, AttributeError):
        return time_label

    hour = hour % 12 + (12 if meridiem.upper() == "PM" else 0)
    total = (hour * 60 + minute + minutes) % (24 * 60)
    out_hour, out_minute = divmod(total, 60)
    suffix = "AM" if out_hour < 12 else "PM"
    display_hour = out_hour % 12 or 12
    return f"{display_hour}:{out_minute:02d} {suffix}"


def generate_flights(query, origin, dest, date=None, limit=50):
    """Generate flight options, cheapest first with one recommendation on top."""
    rng = _seeded_random("flights", query, origin, dest, date)
    lowered = (query or "").lower()

    matching = [a for a in AIRLINES if lowered and (lowered in a[1].lower() or lowered in a[0].lower())]
    airlines = matching or AIRLINES[:15]

    flights = []
    for i, (code, name, logo) in enumerate(airlines):
        for j in range(rng.randint(1, 3)):
            depart = DEPARTURE_TIMES[(i * 2 + j) % len(DEPARTURE_TIMES)]
            price = 180 + rng.randint(0, 400)
            roll = rng.random()
            stops = 0 if roll < 0.6 else (1 if roll < 0.88 else 2)
            duration = 120 + rng.randint(0, 180) + stops * 90

            tags = []
            if stops == 0:
                tags.append("Nonstop")
            if price < 250:
                tags.append("Budget")
            if price > 450:
                tags.append("Premium")
            if i == 0 and j == 0:
                tags.append("Recommended")

            flights.append({
                "id": f"fl-{code}-{i}-{j}",
                "airline": name,
                "airlineLogo": logo,
                "departTime": depart,
                "arriveTime": _add_minutes(depart, duration),
                "duration": _minutes_to_label(duration),
                "stops": stops,
                "stopCity": rng.choice(STOP_CITIES) if stops else None,
                "price": price,
                "priceDiff": 0,
                "tags": tags,
                "origin": origin or "SFO",
                "destination": dest or "JFK",
                "flightNumber": f"{code}{100 + rng.randint(0, 899)}",
            })

    flights.sort(key=lambda f: (0 if "Recommended" in f["tags"] else 1, f["price"]))
    flights = flights[:limit]

    # Price difference is relative to the top result, so the UI can show
    # "+$40 vs recommended" without recomputing.
    if flights:
        baseline = flights[0]["price"]
        for flight in flights:
            flight["priceDiff"] = flight["price"] - baseline
    return flights


def generate_hotels(query, city, check_in=None, check_out=None, limit=100):
    """Generate hotel options for a city, cheapest first."""
    rng = _seeded_random("hotels", query, city, check_in, check_out)
    lowered = (query or "").lower()

    matching = [c for c in HOTEL_CHAINS if lowered and lowered in c.lower()]
    chains = matching or HOTEL_CHAINS

    nights = 2
    if check_in and check_out:
        from model.base import parse_date
        start, end = parse_date(check_in), parse_date(check_out)
        if start and end and end > start:
            nights = (end - start).days

    hotels = []
    index = 0
    for chain_index, chain in enumerate(chains):
        for j in range(rng.randint(1, 4)):
            suffix = rng.choice(HOTEL_SUFFIXES)
            area = rng.choice(AREAS)
            price = 100 + rng.randint(0, 400)
            rating = round(3.5 + rng.random() * 1.5, 1)
            distance = round(0.1 + rng.random() * 2, 1)

            amenities = rng.sample(AMENITIES, rng.randint(4, 9))

            tags = []
            if chain_index == 0 and j == 0:
                tags.append("Recommended")
            if price < 150:
                tags.append("Budget friendly")
            if price > 350:
                tags.append("Luxury")
            if distance < 0.5:
                tags.append("Closest")
            if rating > 4.5:
                tags.append("Top rated")

            hotels.append({
                "id": f"ht-{index}",
                "name": f"{chain} {suffix}",
                "area": area,
                "pricePerNight": price,
                "totalPrice": price * nights,
                "rating": rating,
                "distanceToVenue": f"{distance} mi",
                "tags": tags,
                "images": HOTEL_IMAGES,
                "amenities": amenities,
                "reviewCount": 500 + rng.randint(0, 3000),
                "description": (
                    f"A {rating}-star {suffix.lower()} in {area}, "
                    f"{distance} mi from the venue."
                ),
                "cancellationPolicy": rng.choice(CANCELLATION),
                "roomTypes": rng.sample(ROOM_TYPES, rng.randint(2, 4)),
                "city": city or "San Francisco",
            })
            index += 1

    hotels.sort(key=lambda h: (0 if "Recommended" in h["tags"] else 1, h["pricePerNight"]))
    return hotels[:limit]


def generate_ground(query, city):
    """Generate ground-transport options from the airport into town."""
    rng = _seeded_random("ground", query, city)
    lowered = (query or "").lower()

    providers = GROUND_PROVIDERS
    if lowered:
        matches = [g for g in GROUND_PROVIDERS if lowered in g[1].lower() or lowered in g[0].lower()]
        providers = matches or GROUND_PROVIDERS

    options = []
    for i, (kind, provider, base_price, tags) in enumerate(providers):
        options.append({
            "id": f"ground-{i}",
            "type": kind,
            "provider": provider,
            "price": max(1, base_price + rng.randint(-5, 10)),
            "description": (
                "Per day rate, compact car" if kind == "Rental Car"
                else f"Estimated fare from airport to {city or 'city center'}"
            ),
            "tags": (["Recommended"] + list(tags)) if i == 0 else list(tags),
        })
    return options


def search(search_type="all", query="", city="", origin="", dest="",
           check_in="", check_out="", date=""):
    """
    Run a travel search and return ``{flights, hotels, ground}``.

    Categories the caller didn't ask for come back as empty lists rather than
    being omitted, so the frontend can read every key unconditionally.
    """
    results = {"flights": [], "hotels": [], "ground": []}

    if search_type in ("flights", "all"):
        results["flights"] = generate_flights(query, origin, dest, date)
    if search_type in ("hotels", "all"):
        results["hotels"] = generate_hotels(query, city, check_in, check_out)
    if search_type in ("ground", "all"):
        results["ground"] = generate_ground(query, city)

    return results
