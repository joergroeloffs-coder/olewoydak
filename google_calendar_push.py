#!/usr/bin/env python3
"""
Schreibt die Dienstplan-Termine aus state.json direkt in einen Google Kalender.

Laeuft nach dienstplan_cloud_sync.py und ersetzt fuer den eigenen Gebrauch das
ICS-Abonnement: Termine erscheinen sofort statt erst beim naechsten Abruf durch
Google.

Anmeldung ueber ein Dienstkonto (Service Account). Der Zielkalender muss in
Google Kalender fuer die E-Mail-Adresse des Dienstkontos freigegeben sein,
Berechtigung "Aenderungen an Terminen vornehmen".

Umgebungsvariablen:
  GOOGLE_SA_KEY_B64   JSON-Schluessel des Dienstkontos, base64-kodiert
  GOOGLE_CALENDAR_ID  z. B. abc123@group.calendar.google.com
  DRY_RUN             optional, "1" = nur anzeigen, nichts schreiben

Das Skript fasst ausschliesslich Termine an, die es selbst angelegt hat.
Sie sind an der privaten Eigenschaft app=dienstplan-sync erkennbar.
"""

import base64
import binascii
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "state.json"

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
MARKER_KEY = "app"
MARKER_VALUE = "dienstplan-sync"

# Farben laut Google-Kalender-Palette: 11 = Tomate (rot), 10 = Basilikum (gruen)
COLOR_DIENST = "11"
COLOR_FREI = "10"

DRY_RUN = os.environ.get("DRY_RUN") == "1"


def event_id_for(key):
    """
    Google verlangt Termin-IDs aus base32hex: nur die Zeichen a-v und 0-9,
    Laenge mindestens 5. 'w', 'x', 'y', 'z' und Bindestriche sind unzulaessig,
    deshalb wird der Schluessel '2026-W38' zu 'dienstplan202638'.
    """
    year, week = key.split("-W")
    return f"dienstplan{year}{int(week):02d}"


def build_body(key, entry):
    d_from = date.fromisoformat(entry["date_from"])
    d_to = date.fromisoformat(entry["date_to"])
    summary = entry["summary"]
    is_dienst = summary.startswith("Dienst auf ") or summary.startswith("Besatzungsliste:")
    year, week = key.split("-W")
    stand = entry.get("mtime") or "unbekannt"
    return {
        "id": event_id_for(key),
        "summary": summary,
        # Ganztaegig: 'end' ist der erste Tag NACH dem Zeitraum.
        "start": {"date": d_from.isoformat()},
        "end": {"date": (d_to + timedelta(days=1)).isoformat()},
        "description": (
            f"KW {int(week)}/{year}\n"
            f"Stand: {stand}\n"
            f"Datei: {entry.get('file', '?')}"
        ),
        "colorId": COLOR_DIENST if is_dienst else COLOR_FREI,
        "transparency": "transparent" if not is_dienst else "opaque",
        # Keine automatischen Erinnerungen - sonst meldet sich der Kalender
        # bei jeder Wochenumstellung.
        "reminders": {"useDefault": False},
        "extendedProperties": {
            "private": {
                MARKER_KEY: MARKER_VALUE,
                "week": key,
                "revision": str(entry.get("revision", 0)),
                "mtime": stand,
            }
        },
    }


def load_credentials():
    raw = os.environ.get("GOOGLE_SA_KEY_B64")
    if not raw:
        sys.exit("Fehlt: GOOGLE_SA_KEY_B64 (base64-kodierter Dienstkonto-Schluessel).")
    try:
        info = json.loads(base64.b64decode(raw))
    except (binascii.Error, ValueError) as exc:
        sys.exit(f"GOOGLE_SA_KEY_B64 ist nicht lesbar: {exc}")
    if info.get("type") != "service_account":
        sys.exit("Der hinterlegte Schluessel gehoert zu keinem Dienstkonto.")
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def list_own_events(service, calendar_id):
    """Alle vom Skript selbst angelegten Termine, als {event_id: resource}."""
    found = {}
    page_token = None
    while True:
        resp = service.events().list(
            calendarId=calendar_id,
            privateExtendedProperty=f"{MARKER_KEY}={MARKER_VALUE}",
            showDeleted=False,
            singleEvents=True,
            maxResults=250,
            pageToken=page_token,
        ).execute()
        for ev in resp.get("items", []):
            found[ev["id"]] = ev
        page_token = resp.get("nextPageToken")
        if not page_token:
            return found


def needs_update(existing, body):
    """Vergleicht nur die Felder, die das Skript selbst setzt."""
    if existing.get("summary") != body["summary"]:
        return True
    if existing.get("start", {}).get("date") != body["start"]["date"]:
        return True
    if existing.get("end", {}).get("date") != body["end"]["date"]:
        return True
    old = existing.get("extendedProperties", {}).get("private", {})
    new = body["extendedProperties"]["private"]
    return old.get("mtime") != new.get("mtime") or old.get("revision") != new.get("revision")


def main():
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
    if not calendar_id:
        sys.exit("Fehlt: GOOGLE_CALENDAR_ID.")
    if not STATE_PATH.exists():
        sys.exit("state.json nicht gefunden - zuerst dienstplan_cloud_sync.py laufen lassen.")

    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    desired = {}
    for key, entry in state.items():
        if not entry.get("date_from") or not entry.get("date_to"):
            continue
        body = build_body(key, entry)
        desired[body["id"]] = body

    service = build("calendar", "v3", credentials=load_credentials(), cache_discovery=False)

    try:
        existing = list_own_events(service, calendar_id)
    except HttpError as exc:
        if exc.resp.status == 404:
            sys.exit(
                "Kalender nicht gefunden. GOOGLE_CALENDAR_ID pruefen und "
                "sicherstellen, dass der Kalender fuer das Dienstkonto "
                "freigegeben ist."
            )
        if exc.resp.status == 403:
            sys.exit(
                "Zugriff verweigert. Ist die Google Calendar API im Projekt "
                "aktiviert und der Kalender mit der Berechtigung "
                "'Aenderungen an Terminen vornehmen' freigegeben?"
            )
        raise

    angelegt = geaendert = geloescht = unveraendert = 0

    for event_id, body in sorted(desired.items()):
        current = existing.get(event_id)
        if current is None:
            print(f"neu:       {body['summary']} ({body['start']['date']})")
            if not DRY_RUN:
                try:
                    service.events().insert(calendarId=calendar_id, body=body).execute()
                except HttpError as exc:
                    # 409 = ID existiert bereits, u. U. als geloeschter Termin.
                    if exc.resp.status != 409:
                        raise
                    service.events().update(
                        calendarId=calendar_id, eventId=event_id, body=body
                    ).execute()
            angelegt += 1
        elif needs_update(current, body):
            print(f"geaendert: {body['summary']} ({body['start']['date']})")
            if not DRY_RUN:
                service.events().update(
                    calendarId=calendar_id, eventId=event_id, body=body
                ).execute()
            geaendert += 1
        else:
            unveraendert += 1

    for event_id in sorted(set(existing) - set(desired)):
        print(f"entfernt:  {existing[event_id].get('summary', event_id)}")
        if not DRY_RUN:
            try:
                service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
            except HttpError as exc:
                if exc.resp.status not in (404, 410):
                    raise
        geloescht += 1

    print(
        f"\nangelegt {angelegt}, geaendert {geaendert}, entfernt {geloescht}, "
        f"unveraendert {unveraendert}" + (" (Probelauf)" if DRY_RUN else "")
    )


if __name__ == "__main__":
    main()
