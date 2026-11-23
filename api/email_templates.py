"""
Flyby AI — what the emails say.

Each function returns ``(subject, text, html)`` for one kind of email. They are
plain functions of their arguments — no database, no request — so an email can
be read, tested and changed without running anything else.

Two rules hold for every one of them:

* **Only facts.** A booking email states what the airline or hotel confirmed —
  the confirmation code, the flights, the amount charged — taken from their
  response. Nothing is filled in or estimated.
* **Every value is escaped.** Names, cities and anything else that came from
  outside are HTML-escaped before they reach the page, so a traveler named
  ``<script>`` gets an email addressed to ``<script>``, not one that runs it.

The HTML is deliberately old-fashioned (tables, inline styles): that is what
renders the same in Gmail, Outlook and Apple Mail. Every email also carries a
plain-text version saying the same thing.
"""

from datetime import datetime
from html import escape

BRAND = "Flyby AI"
INK = "#0f172a"
MUTED = "#64748b"
LINE = "#e2e8f0"
ACCENT = "#2563eb"
PAPER = "#f1f5f9"


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def _button(label, url):
    return (f'<table role="presentation" cellspacing="0" cellpadding="0" style="margin:24px 0 4px;"><tr>'
            f'<td style="background:{ACCENT};border-radius:8px;">'
            f'<a href="{escape(url, quote=True)}" style="display:inline-block;padding:12px 22px;'
            f'font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;">{escape(label)}</a>'
            f'</td></tr></table>')


def _rows(pairs):
    """A two-column table of facts. Pairs with an empty value are left out."""
    cells = "".join(
        f'<tr><td style="padding:9px 0;border-bottom:1px solid {LINE};color:{MUTED};font-size:14px;'
        f'vertical-align:top;width:38%;">{escape(str(label))}</td>'
        f'<td style="padding:9px 0;border-bottom:1px solid {LINE};color:{INK};font-size:14px;'
        f'font-weight:600;vertical-align:top;">{escape(str(value))}</td></tr>'
        for label, value in pairs if value not in (None, "")
    )
    return (f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
            f'style="margin:18px 0 6px;border-top:1px solid {LINE};">{cells}</table>') if cells else ""


def _paragraph(text, muted=False):
    return (f'<p style="margin:0 0 14px;font-size:15px;line-height:1.6;'
            f'color:{MUTED if muted else INK};">{escape(text)}</p>')


def _page(heading, body_html, footer=None):
    """Wrap a message body in the shared frame."""
    footer = footer or f"You're receiving this because you have a {BRAND} account."
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(heading)}</title></head>
<body style="margin:0;padding:0;background:{PAPER};">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:{PAPER};">
<tr><td align="center" style="padding:32px 16px;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:560px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<tr><td style="padding:0 4px 16px;font-size:17px;font-weight:700;color:{ACCENT};letter-spacing:-0.01em;">{BRAND}</td></tr>
<tr><td style="background:#ffffff;border:1px solid {LINE};border-radius:14px;padding:30px 30px 26px;">
<h1 style="margin:0 0 16px;font-size:21px;line-height:1.3;font-weight:700;color:{INK};">{escape(heading)}</h1>
{body_html}
</td></tr>
<tr><td style="padding:18px 6px 0;font-size:12px;line-height:1.6;color:{MUTED};">{escape(footer)}</td></tr>
</table>
</td></tr></table>
</body></html>"""


def _first_name(name):
    return (name or "").strip().split(" ")[0] or "there"


def _money(amount, currency="USD"):
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return ""
    symbol = "$" if (currency or "USD").upper() == "USD" else f"{(currency or '').upper()} "
    return f"{symbol}{value:,.2f}"


def _moment(iso):
    """An ISO date-time as "Tue, Dec 8 · 7:40 AM"; a date as "Tue, Dec 8, 2026"."""
    text = (iso or "").strip()
    try:
        if "T" in text:
            when = datetime.fromisoformat(text[:19])
            hour = when.hour % 12 or 12
            return f"{when.strftime('%a, %b')} {when.day} · {hour}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'}"
        when = datetime.fromisoformat(text[:10])
        return f"{when.strftime('%a, %b')} {when.day}, {when.year}"
    except ValueError:
        return text


def _utc_stamp(when):
    hour = when.hour % 12 or 12
    return f"{when.strftime('%b')} {when.day}, {when.year} at {hour}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'} UTC"


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

def welcome(name, app_url):
    subject = f"Welcome to {BRAND}"
    intro = f"Hi {_first_name(name)}, your account is ready."
    points = [
        "Type a trip the way you'd say it — \"Chicago next Tuesday for a 3 PM meeting\" — and Flyby finds and ranks real flights for it.",
        "Connect your Google or Outlook calendar and Flyby spots the meetings that need a trip.",
        "Booked trips file their own expenses.",
    ]
    text = "\n".join([intro, "", *[f"- {p}" for p in points], "", f"Open Flyby: {app_url}"])
    html = _page(f"Welcome to {BRAND}", _paragraph(intro) + "".join(_paragraph(p, muted=True) for p in points)
                 + _button("Open Flyby", app_url))
    return subject, text, html


def sign_in(name, when, browser=None, ip_address=None, app_url=None, method=None):
    subject = f"New sign-in to your {BRAND} account"
    stamp = _utc_stamp(when)
    how = {"oauth": "Google or Microsoft sign-in"}.get(method or "", "Email and password")
    intro = f"Hi {_first_name(name)}, your {BRAND} account was just signed in to."
    warning = ("If this was you, there's nothing to do. If it wasn't, change your password now "
               "and check the devices signed in to your account under Settings → Security.")
    facts = [("When", stamp), ("How", how), ("Browser", browser if browser and browser != "Unknown" else None),
             ("IP address", ip_address if ip_address and ip_address != "unknown" else None)]
    text = "\n".join([intro, "", *[f"{k}: {v}" for k, v in facts if v], "", warning]
                     + ([f"", f"Security settings: {app_url}/settings"] if app_url else []))
    html = _page("New sign-in to your account", _paragraph(intro) + _rows(facts) + _paragraph(warning, muted=True)
                 + (_button("Review security settings", f"{app_url}/settings") if app_url else ""),
                 footer="Sign-in alerts can be turned off in Settings → Notifications.")
    return subject, text, html


def password_changed(name, when, ip_address=None, app_url=None):
    subject = f"Your {BRAND} password was changed"
    intro = f"Hi {_first_name(name)}, the password for your {BRAND} account was changed."
    warning = ("If you made this change, you're all set. If you didn't, reset your password right away "
               "— someone else may have access to your account.")
    facts = [("When", _utc_stamp(when)), ("IP address", ip_address if ip_address and ip_address != "unknown" else None)]
    text = "\n".join([intro, "", *[f"{k}: {v}" for k, v in facts if v], "", warning])
    html = _page("Your password was changed", _paragraph(intro) + _rows(facts) + _paragraph(warning, muted=True)
                 + (_button("Reset password", f"{app_url}/forgot-password") if app_url else ""),
                 footer="Security notices like this one are always sent.")
    return subject, text, html


def password_reset(name, link, minutes=30):
    subject = f"Reset your {BRAND} password"
    intro = f"Hi {_first_name(name)}, we received a request to reset your {BRAND} password."
    expiry = f"This link works for {minutes} minutes and can be used once."
    ignore = "If you didn't ask for this, you can ignore this email — your password hasn't changed."
    text = "\n".join([intro, "", f"Reset your password: {link}", "", expiry, ignore])
    html = _page("Reset your password", _paragraph(intro) + _button("Choose a new password", link)
                 + _paragraph(expiry, muted=True) + _paragraph(ignore, muted=True),
                 footer="Security notices like this one are always sent.")
    return subject, text, html


def two_factor_changed(name, enabled, when):
    state = "turned on" if enabled else "turned off"
    subject = f"Two-step verification was {state}"
    intro = f"Hi {_first_name(name)}, two-step verification on your {BRAND} account was {state}."
    warning = ("If this was you, there's nothing to do. If it wasn't, change your password now."
               if enabled else
               "Your account is now protected by your password alone. If you didn't do this, "
               "change your password now and turn two-step verification back on.")
    text = "\n".join([intro, "", f"When: {_utc_stamp(when)}", "", warning])
    html = _page(f"Two-step verification {state}", _paragraph(intro) + _rows([("When", _utc_stamp(when))])
                 + _paragraph(warning, muted=True), footer="Security notices like this one are always sent.")
    return subject, text, html


def test_message(name, sender):
    subject = f"{BRAND} email is working"
    intro = f"Hi {_first_name(name)}, this is a test from {BRAND}."
    detail = (f"It was sent from {sender}. Sign-in alerts, booking confirmations and password resets "
              "will arrive from the same address.")
    return subject, f"{intro}\n\n{detail}", _page("Email is working", _paragraph(intro) + _paragraph(detail, muted=True))


# ---------------------------------------------------------------------------
# Trips
# ---------------------------------------------------------------------------

def _order_journeys(order):
    """
    Each direction of a Duffel order as plain facts: route, times, flights.

    Read straight from the order the airline confirmed.
    """
    journeys = []
    for index, flight_slice in enumerate(order.get("slices") or []):
        segments = flight_slice.get("segments") or []
        if not segments:
            continue
        first, last = segments[0], segments[-1]
        flights = ", ".join(
            f"{(seg.get('marketing_carrier') or {}).get('iata_code') or ''}"
            f"{seg.get('marketing_carrier_flight_number') or ''}".strip()
            for seg in segments
        )
        via = [(seg.get("destination") or {}).get("iata_code") for seg in segments[:-1]]
        via = [code for code in via if code]
        journeys.append({
            "label": "Outbound" if index == 0 else ("Return" if index == 1 else f"Flight {index + 1}"),
            "route": f"{(first.get('origin') or {}).get('iata_code') or ''} → "
                     f"{(last.get('destination') or {}).get('iata_code') or ''}",
            "departs": _moment(first.get("departing_at")),
            "arrives": _moment(last.get("arriving_at")),
            "flights": flights,
            "stops": "Nonstop" if not via else f"{len(via)} stop{'s' if len(via) != 1 else ''} via {', '.join(via)}",
            "airline": (first.get("marketing_carrier") or {}).get("name") or "",
        })
    return journeys


def flight_booked(name, order, app_url=None):
    """Confirmation for a flight the airline has just ticketed."""
    reference = order.get("booking_reference") or ""
    journeys = _order_journeys(order)
    travelers = ", ".join(
        " ".join(part for part in (p.get("given_name"), p.get("family_name")) if part)
        for p in (order.get("passengers") or [])
    )
    total = _money(order.get("total_amount"), order.get("total_currency"))
    airline = ((order.get("owner") or {}).get("name")) or (journeys[0]["airline"] if journeys else "")
    route = journeys[0]["route"] if journeys else "your trip"

    subject = f"Flight booked: {route}" + (f" · confirmation {reference}" if reference else "")
    intro = f"Hi {_first_name(name)}, your flight is booked and ticketed."
    facts = [("Airline confirmation", reference), ("Airline", airline), ("Travelers", travelers), ("Total charged", total)]
    reminder = (f"Use confirmation {reference} to check in or manage the booking on the airline's website."
                if reference else "")

    text_lines = [intro, "", *[f"{k}: {v}" for k, v in facts if v]]
    blocks = _paragraph(intro) + _rows(facts)
    for journey in journeys:
        text_lines += ["", f"{journey['label']}: {journey['route']}", f"  Departs {journey['departs']}",
                       f"  Arrives {journey['arrives']}", f"  {journey['flights']} · {journey['stops']}"]
        blocks += (f'<p style="margin:20px 0 0;font-size:13px;font-weight:700;letter-spacing:.04em;'
                   f'text-transform:uppercase;color:{MUTED};">{escape(journey["label"])} · {escape(journey["route"])}</p>'
                   + _rows([("Departs", journey["departs"]), ("Arrives", journey["arrives"]),
                            ("Flights", journey["flights"]), ("Stops", journey["stops"])]))
    if reminder:
        text_lines += ["", reminder]
        blocks += _paragraph(reminder, muted=True)
    if app_url:
        text_lines += ["", f"View your trip: {app_url}/trips"]
        blocks += _button("View your trip", f"{app_url}/trips")
    return subject, "\n".join(text_lines), _page("Your flight is booked", blocks,
                                                 footer="Booking confirmations are always sent.")


def hotel_reserved(name, booking, app_url=None):
    """Confirmation for a hotel reservation the hotel has just accepted."""
    hotel = booking.get("hotelName") or "your hotel"
    reference = booking.get("reference") or ""
    subject = f"Hotel reserved: {hotel}" + (f" · confirmation {reference}" if reference else "")
    intro = f"Hi {_first_name(name)}, your room at {hotel} is reserved."
    facts = [("Hotel confirmation", reference), ("Hotel", booking.get("hotelName")),
             ("Check-in", _moment(booking.get("checkInDate"))), ("Check-out", _moment(booking.get("checkOutDate"))),
             ("Total", _money(booking.get("totalAmount") or booking.get("totalPrice"), booking.get("currency")))]
    keys = (booking.get("keyCollection") or "").strip()
    text_lines = [intro, "", *[f"{k}: {v}" for k, v in facts if v]] + (["", f"Getting your key: {keys}"] if keys else [])
    blocks = _paragraph(intro) + _rows(facts) + (_paragraph(f"Getting your key: {keys}", muted=True) if keys else "")
    if app_url:
        text_lines += ["", f"View your trip: {app_url}/trips"]
        blocks += _button("View your trip", f"{app_url}/trips")
    return subject, "\n".join(text_lines), _page("Your hotel is reserved", blocks,
                                                 footer="Booking confirmations are always sent.")


def trip_added(name, trip, app_url=None):
    """A flight the traveler booked on an airline's own site and added to Flyby."""
    flight = trip.get("flight") or {}
    site = flight.get("bookedVia") or "the airline's site"
    destination = trip.get("destination") or "your trip"
    reference = flight.get("bookingReference") or ""
    subject = f"Trip saved: {destination}"
    intro = (f"Hi {_first_name(name)}, the flight you booked on {site} is now saved in {BRAND} "
             f"with your trip and expenses.")
    note = f"Flyby didn't book or charge this — changes and cancellations are made on {site}."
    facts = [("Destination", destination), ("Airline", flight.get("airline")), ("Confirmation", reference),
             ("Departs", _moment(trip.get("startDate"))), ("Returns", _moment(trip.get("endDate"))),
             ("Amount you entered", _money(flight.get("price")) if flight.get("price") else None)]
    text_lines = [intro, "", *[f"{k}: {v}" for k, v in facts if v], "", note]
    blocks = _paragraph(intro) + _rows(facts) + _paragraph(note, muted=True)
    if app_url:
        text_lines += ["", f"View your trip: {app_url}/trips"]
        blocks += _button("View your trip", f"{app_url}/trips")
    return subject, "\n".join(text_lines), _page("Trip saved", blocks,
                                                 footer="Trip emails can be turned off in Settings → Notifications.")


def trip_cancelled(name, trip, app_url=None):
    """A trip marked cancelled in Flyby. Says plainly what that does and doesn't do."""
    flight = trip.get("flight") or {}
    destination = trip.get("destination") or "your trip"
    reference = flight.get("bookingReference") or ""
    subject = f"Trip cancelled in {BRAND}: {destination}"
    intro = f"Hi {_first_name(name)}, your trip to {destination} was marked as cancelled in {BRAND}."
    warning = (f"This does not cancel the ticket with the airline. If you hold a booking"
               f"{f' (confirmation {reference})' if reference else ''}, cancel or change it with the airline "
               f"to get any refund or credit you're due."
               if reference else
               "No airline booking was recorded on this trip, so there is nothing further to cancel.")
    facts = [("Destination", destination), ("Airline", flight.get("airline")), ("Confirmation", reference),
             ("Was departing", _moment(trip.get("startDate")))]
    text_lines = [intro, "", *[f"{k}: {v}" for k, v in facts if v], "", warning]
    blocks = _paragraph(intro) + _rows(facts) + _paragraph(warning, muted=True)
    if app_url:
        blocks += _button("View your trips", f"{app_url}/trips")
    return subject, "\n".join(text_lines), _page("Trip cancelled", blocks,
                                                 footer="Trip emails can be turned off in Settings → Notifications.")


# ---------------------------------------------------------------------------
# Company portals
# ---------------------------------------------------------------------------

def portal_requested(company_name, domain, requester_name, requester_email, console_url):
    """To whoever reviews portals at Flyby: a new request is waiting."""
    subject = f"Company portal to verify: {company_name}"
    intro = f"{requester_name} ({requester_email}) asked for a company portal."
    facts = [("Company", company_name), ("Website", domain), ("Requested by", f"{requester_name} · {requester_email}")]
    text = "\n".join([intro, "", *[f"{k}: {v}" for k, v in facts if v], "", f"Review it: {console_url}"])
    html = _page("A company portal is waiting for review", _paragraph(intro) + _rows(facts)
                 + _button("Review in the console", console_url),
                 footer="Sent to the address set as PORTAL_REVIEW_EMAIL on the Flyby server.")
    return subject, text, html


def portal_verified(name, company_name, join_code, app_url=None):
    subject = f"{company_name} is live on {BRAND}"
    intro = f"Hi {_first_name(name)}, {company_name}'s company portal is verified, and you're its admin."
    how = ("Share the join code with your team. Anyone who joins with it becomes a member, and as an admin "
           "you can see their trips and expenses.")
    keep = "Treat the code like a password: anyone with it can join. You can replace it at any time."
    facts = [("Company", company_name), ("Join code", join_code)]
    text_lines = [intro, "", *[f"{k}: {v}" for k, v in facts], "", how, keep]
    blocks = _paragraph(intro) + _rows(facts) + _paragraph(how) + _paragraph(keep, muted=True)
    if app_url:
        text_lines += ["", f"Open your company dashboard: {app_url}/company"]
        blocks += _button("Open your company dashboard", f"{app_url}/company")
    return subject, "\n".join(text_lines), _page(f"{company_name} is verified", blocks,
                                                 footer="Company portal emails are always sent.")


def portal_rejected(name, company_name, note):
    subject = f"Your {BRAND} company portal request"
    intro = f"Hi {_first_name(name)}, Flyby couldn't verify the company portal you asked for, {company_name}."
    reason = f"Reason: {note}" if note else ""
    again = "You can send a new request from Settings with the details corrected."
    text = "\n".join([intro, "", reason, "", again]).replace("\n\n\n", "\n\n")
    html = _page("We couldn't verify your company", _paragraph(intro) + (_paragraph(reason) if reason else "")
                 + _paragraph(again, muted=True), footer="Company portal emails are always sent.")
    return subject, text, html


def member_joined(admin_name, company_name, member_name, member_email, app_url=None):
    subject = f"{member_name} joined {company_name} on {BRAND}"
    intro = f"Hi {_first_name(admin_name)}, {member_name} ({member_email}) joined {company_name} with your join code."
    note = ("If you don't know them, remove them from your company dashboard and replace the join code "
            "so the old one stops working.")
    text_lines = [intro, "", note] + ([f"", f"Company dashboard: {app_url}/company"] if app_url else [])
    blocks = _paragraph(intro) + _paragraph(note, muted=True) + (
        _button("Open company dashboard", f"{app_url}/company") if app_url else "")
    return subject, "\n".join(text_lines), _page("Someone joined your company", blocks,
                                                 footer="Sent to every admin of the company.")


# ---------------------------------------------------------------------------
# Expense approval
# ---------------------------------------------------------------------------

def _expense_rows(expense, note=None, note_label="Note"):
    """The facts about an expense a manager or traveler needs to recognise it."""
    category = (expense.get("category") or "").strip()
    return [
        ("Expense", expense.get("merchant") or expense.get("description")),
        ("Amount", _money(expense.get("amount"), expense.get("currency") or "USD")),
        ("Date", _moment(str(expense.get("date") or ""))),
        ("Trip", expense.get("tripName")),
        ("Category", category[:1].upper() + category[1:] if category else ""),
        (note_label, (note or "").strip()),
    ]


def expense_submitted(approver_name, member_name, company_name, expense, note=None, app_url=None):
    subject = f"{member_name} sent you an expense to approve"
    intro = f"Hi {_first_name(approver_name)}, {member_name} sent you an expense to review for {company_name}."
    rows = _expense_rows(expense, note, f"Note from {_first_name(member_name)}")
    text_lines = [intro, ""] + [f"{label}: {value}" for label, value in rows if value] + (
        ["", f"Review it: {app_url}/company"] if app_url else [])
    blocks = _paragraph(intro) + _rows(rows) + (
        _button("Review in Flyby", f"{app_url}/company") if app_url else "")
    return subject, "\n".join(text_lines), _page("An expense is waiting for you", blocks,
                                                 footer=f"Sent because you're an admin of {company_name} on {BRAND}.")


def expense_decided(member_name, expense, decision, reviewer_name, note=None, app_url=None):
    merchant = expense.get("merchant") or expense.get("description") or "your"
    amount = _money(expense.get("amount"), expense.get("currency") or "USD")
    if decision == "approved":
        subject = f"{reviewer_name} approved your {merchant} expense"
        heading = "Expense approved"
        intro = f"Hi {_first_name(member_name)}, {reviewer_name} approved your {merchant} expense ({amount})."
    else:
        subject = f"{reviewer_name} sent back your {merchant} expense"
        heading = "Expense sent back"
        intro = (f"Hi {_first_name(member_name)}, {reviewer_name} sent back your {merchant} expense ({amount}). "
                 "You can change it and send it again from Expenses.")
    rows = _expense_rows(expense, note, f"Note from {_first_name(reviewer_name)}")
    text_lines = [intro, ""] + [f"{label}: {value}" for label, value in rows if value] + (
        ["", f"Your expenses: {app_url}/expenses"] if app_url else [])
    blocks = _paragraph(intro) + _rows(rows) + (
        _button("Open expenses", f"{app_url}/expenses") if app_url else "")
    return subject, "\n".join(text_lines), _page(heading, blocks)
