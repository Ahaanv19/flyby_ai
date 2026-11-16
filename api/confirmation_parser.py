"""
Flyby AI — reading a pasted airline confirmation.

Some airlines can't be booked inside Flyby (United is not sold through our
flight provider), so the traveler books on the airline's own site and brings
the booking back: they paste the confirmation email and this pulls out the
confirmation code, flights, times and price so they don't retype them.

Flyby never sees the traveler's inbox — only the text they choose to paste.

The traveler reviews every field before anything is saved, but nothing here is
allowed to invent one either. Whatever Gemini returns is checked against the
pasted text: a confirmation code, flight number, airport or price that doesn't
literally appear in it is dropped. With no Gemini key, or when Gemini is busy,
a pattern-based reader finds what it reliably can and leaves the rest blank.
"""

import re
from datetime import date

from api.gemini import call_gemini

MAX_CHARS = 12000

_TIME = re.compile(r"^\d{1,2}:\d{2}\s?(AM|PM)$", re.IGNORECASE)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CODE = re.compile(r"^[A-Z0-9]{5,8}$")
_FLIGHT = re.compile(r"^([A-Z0-9]{2})\s?(\d{1,4})$")
_AIRPORT = re.compile(r"^[A-Z]{3}$")


def _system_prompt(today):
    return f"""You read an airline booking confirmation email that a traveler pasted, and extract the booking.

Today's date is {today} (ISO). Use it only to work out the year when the email gives a date without one.

Return ONLY a JSON object with these keys. Use null for anything the email does not state — never guess.
{{
  "confirmationCode": "the airline confirmation / record locator, e.g. ABC123",
  "airline": "the airline's name",
  "flightNumber": "first outbound flight, e.g. UA 1234",
  "departureAirport": "3-letter code the trip starts from",
  "arrivalAirport": "3-letter code of the destination (the last airport of the outbound journey)",
  "departureDate": "YYYY-MM-DD of the outbound departure",
  "departTime": "outbound departure local time, like 7:40 AM",
  "arrivalTime": "outbound arrival local time at the destination, like 3:55 PM",
  "stops": 0,
  "returnFlightNumber": "first flight of the journey home, or null for a one-way",
  "returnDate": "YYYY-MM-DD the journey home departs, or null",
  "returnDepartTime": "like 5:10 PM, or null",
  "returnArrivalTime": "like 8:02 PM, or null",
  "totalPaid": 0.00,
  "currency": "ISO code, e.g. USD"
}}
"totalPaid" is the total charged for the whole booking, including taxes and fees."""


# ---------------------------------------------------------------------------
# Checking a field against the text
# ---------------------------------------------------------------------------

def _digits(value):
    return re.sub(r"\D", "", str(value))


def _amount_in_text(amount, text):
    """Whether this price is written in the text (1,234.50 / 1234.5 / 1234)."""
    plain = text.replace(",", "")
    forms = {f"{amount:.2f}"}
    if amount == int(amount):
        forms.add(str(int(amount)))
    return any(re.search(rf"(?<![\d.]){re.escape(form)}(?!\d)", plain) for form in forms)


def _verified(fields, text):
    """Keep only well-formed fields that the pasted text actually supports."""
    upper = text.upper()
    out = {}

    def text_field(key, pattern=None, normalize=str.strip):
        value = fields.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
        value = normalize(value)
        if pattern and not pattern.match(value):
            return None
        return value

    code = text_field("confirmationCode", _CODE, lambda v: v.strip().upper())
    if code and re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", upper):
        out["confirmationCode"] = code

    airline = text_field("airline")
    if airline and airline.upper() in upper:
        out["airline"] = airline[:60]

    for key in ("flightNumber", "returnFlightNumber"):
        value = text_field(key, _FLIGHT, lambda v: v.strip().upper())
        if not value:
            continue
        carrier, number = _FLIGHT.match(value).groups()
        if re.search(rf"(?<![A-Z0-9]){carrier}\s?0*{number}(?!\d)", upper) \
                or re.search(rf"FLIGHT\s*#?\s*0*{number}(?!\d)", upper):
            out[key] = f"{carrier} {number}"

    for key in ("departureAirport", "arrivalAirport"):
        value = text_field(key, _AIRPORT, lambda v: v.strip().upper())
        if value and re.search(rf"(?<![A-Z]){value}(?![A-Z])", upper):
            out[key] = value

    for key in ("departTime", "arrivalTime", "returnDepartTime", "returnArrivalTime"):
        value = text_field(key, _TIME, lambda v: re.sub(r"\s+", " ", v.strip().upper()))
        # The hour and minute must be in the email, however it spells AM/PM.
        if not value:
            continue
        hhmm = re.match(r"\d{1,2}:\d{2}", value).group(0).lstrip("0") or "0"
        if re.search(rf"(?<!\d)0?{re.escape(hhmm)}(?!\d)", upper):
            out[key] = f"{hhmm} {value[-2:]}"

    for key in ("departureDate", "returnDate"):
        value = text_field(key, _ISO_DATE)
        if not value:
            continue
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            continue
        # The day of the month has to be written in the email.
        if re.search(rf"(?<!\d)0?{parsed.day}(?!\d)", text):
            out[key] = value

    try:
        amount = round(float(fields.get("totalPaid")), 2)
    except (TypeError, ValueError):
        amount = None
    if amount and 0 < amount < 100000 and _amount_in_text(amount, text):
        out["totalPaid"] = amount
        currency = text_field("currency", re.compile(r"^[A-Z]{3}$"), lambda v: v.strip().upper())
        out["currency"] = currency or "USD"

    stops = fields.get("stops")
    if isinstance(stops, int) and 0 <= stops <= 4 and "flightNumber" in out:
        out["stops"] = stops

    return out


# ---------------------------------------------------------------------------
# Without Gemini
# ---------------------------------------------------------------------------

def _read_locally(text):
    """The fields a pattern can find reliably. Dates and times are left to the traveler."""
    upper = text.upper()
    fields = {}

    code = re.search(r"(?:CONFIRMATION|RECORD LOCATOR|BOOKING REF(?:ERENCE)?)\s*(?:NUMBER|CODE|NO\.?|#)?\s*[:#]?\s*([A-Z0-9]{6})(?![A-Z0-9])", upper)
    if code:
        fields["confirmationCode"] = code.group(1)

    flights = []
    for carrier, number in re.findall(r"(?<![A-Z0-9])(UA|AA|DL|AS|WN|B6|F9|NK|HA|AC)\s?(\d{1,4})(?!\d)", upper):
        flight = f"{carrier} {int(number)}"
        if flight not in flights:
            flights.append(flight)
    if flights:
        fields["flightNumber"] = flights[0]

    airports = []
    for airport in re.findall(r"\(([A-Z]{3})(?:\s*-[^)]*)?\)", upper):
        if airport not in airports:
            airports.append(airport)
    if len(airports) >= 2:
        fields["departureAirport"], fields["arrivalAirport"] = airports[0], airports[1]

    total = re.search(r"TOTAL[^$\n]{0,40}\$\s?([\d,]+\.\d{2})", upper)
    if total:
        fields["totalPaid"] = float(total.group(1).replace(",", ""))
        fields["currency"] = "USD"

    if "UNITED" in upper:
        fields["airline"] = "United Airlines"
    return fields


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_confirmation(text):
    """
    Read a pasted confirmation. Returns ``(booking, engine)``.

    ``booking`` holds only the fields found in the text — possibly none.
    ``engine`` is "gemini" or "local".
    """
    text = (text or "").strip()[:MAX_CHARS]
    if len(text) < 20:
        return {}, "local"

    parsed = call_gemini(_system_prompt(date.today().isoformat()), text,
                         max_output_tokens=4096, temperature=0.0, timeout=20)
    if isinstance(parsed, dict):
        booking = _verified(parsed, text)
        if booking:
            return booking, "gemini"
    return _verified(_read_locally(text), text), "local"
