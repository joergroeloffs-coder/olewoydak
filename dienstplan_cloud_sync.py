#!/usr/bin/env python3
"""
Dienstplan-Sync fuer GitHub Actions.

Liest das Apache-Verzeichnislisting der Besatzungslisten auf faehre2.de,
waehlt je Kalenderwoche die aktuellste Fassung (hoechste "n. Aenderung"),
sucht darin nach einem Namen und schreibt zwei ICS-Kalenderdateien nach
docs/<SLUG>/:

  dienst.ics  -> Wochen mit Schiffszuordnung ("Dienst auf ...")
  frei.ics    -> Freie Tage / Urlaub / Abwesend

Diese Dateien werden von GitHub Pages veroeffentlicht. Google Kalender
(und darueber auch die Handy-Kalender-Apps) abonnieren die Adresse per
"Von URL hinzufuegen" und aktualisieren sich danach von selbst.

Ein PDF wird nur dann heruntergeladen, wenn sich Dateiname oder
Aenderungsdatum gegenueber state.json unterscheiden.

Zugangsdaten kommen aus den Umgebungsvariablen WDR_USER / WDR_PASS
(als GitHub Actions Secrets hinterlegt), nicht aus einer lokalen Datei.
"""

import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from html import unescape
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote

import pdfplumber
import requests

HERE = Path(__file__).resolve().parent

# Fester, zufaellig erzeugter Ordnername - macht die Pages-Adresse
# schwer zu erraten. Achtung: schuetzt NICHT gegen jemanden, der das
# Repository selbst aufruft (siehe README).
SLUG = "kal-76a4a349015c4272fe03f77806423a4b"

OUTPUT_DIR = HERE / "docs" / SLUG
STATE_PATH = HERE / "state.json"

DIENST_ICS_PATH = OUTPUT_DIR / "dienst.ics"
FREI_ICS_PATH = OUTPUT_DIR / "frei.ics"

BASE_URL = "https://faehre2.de/fileadmin/wdr/Schiffe/Besatzungslisten"
INDEX_URL = BASE_URL + "/"

TARGET_NAME = os.environ.get("WDR_NAME_FRAGMENT", "Roeloffs")
WEEKS_BACK = int(os.environ.get("WDR_WEEKS_BACK", "2"))
WEEKS_AHEAD = int(os.environ.get("WDR_WEEKS_AHEAD", "8"))
# Wie lange alte Wochen im Kalender stehen bleiben, bevor sie entfallen.
PRUNE_WEEKS = int(os.environ.get("WDR_PRUNE_WEEKS", "12"))

KNOWN_RANKS = {
    "NK", "NEO", "TLM", "TWB", "TWO", "GSM", "NWB", "NWB*", "AZ", "NW",
}

# Konstanter Ersatzstempel, falls das Verzeichnislisting kein Datum liefert.
# Bewusst konstant, damit die ICS nicht bei jedem Lauf neu geschrieben wird.
FALLBACK_STAMP = "20000101T000000Z"

# Eine Zeile des Apache-Listings: href zuerst, danach das Aenderungsdatum.
# Bewusst auf das href-Attribut gematcht (prozentkodiertes ASCII) statt auf
# den Linktext - so ist das Ergebnis unabhaengig von der Zeichenkodierung
# der HTML-Seite und von Apaches Namenskuerzung im Anzeigetext.
ROW_RE = re.compile(
    r'<a href="([^"?/][^"]*\.pdf)"[^>]*>[^<]*</a>'
    r'(?:\s*</td>\s*<td[^>]*>)?\s*'
    r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})?',
    re.IGNORECASE,
)

# "38KW2026.pdf" oder "38KW2026 1. Aenderung.pdf".
# Das Muster "\S*nderung" trifft absichtlich Aenderung, Änderung und eine
# eventuell falsch dekodierte Variante gleichermassen.
FILE_RE = re.compile(
    r"^(\d{1,2})KW(\d{4})(?:[ _]+(\d+)\.\s*\S*nderung)?\.pdf$",
    re.IGNORECASE,
)


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_PATH.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def iso_weeks_to_check(weeks_back, weeks_ahead):
    today = date.today()
    monday_this_week = today - timedelta(days=today.weekday())
    result = []
    for offset in range(-weeks_back, weeks_ahead + 1):
        monday = monday_this_week + timedelta(weeks=offset)
        iso_year, iso_week, _ = monday.isocalendar()
        result.append((iso_year, iso_week))
    return result


def safe_decode_href(href):
    text = unescape(href)
    try:
        return unquote(text, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        return unquote(text, encoding="latin-1", errors="replace")


def fetch_index(session):
    """
    Liest das Verzeichnislisting einmalig.

    Rueckgabe: {(jahr, kw): (revision, href, dateiname, mtime)}
    mit jeweils der hoechsten gefundenen Revision.
    """
    resp = session.get(INDEX_URL, timeout=30)
    if resp.status_code == 401:
        sys.exit("HTTP 401: WDR_USER / WDR_PASS falsch oder abgelaufen.")
    if resp.status_code == 403:
        sys.exit("HTTP 403: Zugriff auf das Verzeichnis verweigert.")
    resp.raise_for_status()

    best = {}
    rows = ROW_RE.findall(resp.text)
    if not rows:
        sys.exit(
            "Verzeichnislisting enthaelt keine PDF-Zeilen - "
            "HTML-Struktur oder Adresse pruefen."
        )

    for href, mtime in rows:
        name = safe_decode_href(href)
        m = FILE_RE.match(name)
        if not m:
            print(f"  Hinweis: unbekanntes Dateimuster '{name}'")
            continue
        week = int(m.group(1))
        year = int(m.group(2))
        revision = int(m.group(3) or 0)
        key = (year, week)
        if key not in best or revision > best[key][0]:
            best[key] = (revision, href, name, mtime or "")

    if not best:
        sys.exit("Keine auswertbaren Dateinamen im Verzeichnislisting gefunden.")
    return best


def parse_date_range(pdf):
    text = pdf.pages[0].extract_text() or ""
    m = re.search(
        r"vom\s+(\d{2}\.\d{2}\.\d{4})\s+bis\s+(\d{2}\.\d{2}\.\d{4})", text
    )
    if not m:
        return None, None
    d_from = date(*reversed([int(p) for p in m.group(1).split(".")]))
    d_to = date(*reversed([int(p) for p in m.group(2).split(".")]))
    return d_from, d_to


def column_pairs(table):
    """
    (x0, x1) je Spaltenpaar (Rang + Name), abgeleitet aus der Zeile mit den
    meisten Zellen. Kopfzeilen taugen dafuer nicht: In den Besatzungslisten
    reicht die erkannte Kopfzeile teils nicht ueber die volle Tabellenbreite.
    """
    widest = max(table.rows, key=lambda r: sum(1 for c in r.cells if c))
    cells = widest.cells
    pairs = {}
    for i in range(0, len(cells), 2):
        left = cells[i]
        right = cells[i + 1] if i + 1 < len(cells) else None
        if left and right:
            pairs[i] = (left[0], right[2])
        elif left:
            pairs[i] = (left[0], left[2])
    return pairs


def headers_in_band(page, table, top, bottom, pairs):
    """
    Ueberschriften einer Kopfzeile ueber die x-Position der Woerter zuordnen
    statt ueber die Zellen. Die letzte Spalte einer Besatzungsliste liegt
    haeufig ausserhalb der von pdfplumber erkannten Kopfzeile; ihr Text ginge
    sonst verloren und die Woche endete als "UNBEKANNT".
    """
    x0, x1 = table.bbox[0], table.bbox[2]
    band = page.crop((x0, max(top - 1, 0), x1, min(bottom + 1, page.height)))
    found = {}
    for word in sorted(band.extract_words(), key=lambda w: w["x0"]):
        center = (word["x0"] + word["x1"]) / 2
        for index, (left, right) in pairs.items():
            if left <= center <= right:
                found[index] = (found.get(index, "") + " " + word["text"]).strip()
                break
    return found


def find_status_for_name(pdf, target_name_fragment):
    hits = []
    for page in pdf.pages:
        for table in page.find_tables():
            rows = table.extract()
            pairs = column_pairs(table)
            headers = {}
            for row, meta in zip(rows, table.rows):
                ncols = len(row)
                is_header = True
                any_text = False
                for i in range(0, ncols, 2):
                    even = (row[i] or "").strip()
                    odd = row[i + 1].strip() if i + 1 < ncols and row[i + 1] else ""
                    if even:
                        any_text = True
                        if odd or even.upper() in KNOWN_RANKS:
                            is_header = False
                if not any_text:
                    continue
                if is_header:
                    # Jeder Block bringt eigene Ueberschriften mit; die alten
                    # duerfen nicht stehen bleiben.
                    headers = headers_in_band(
                        page, table, meta.bbox[1], meta.bbox[3], pairs
                    )
                    continue
                for i in range(0, ncols, 2):
                    name_cell = row[i + 1].strip() if i + 1 < ncols and row[i + 1] else ""
                    if target_name_fragment.lower() in name_cell.lower():
                        rank_cell = (row[i] or "").strip()
                        category = headers.get(i, "UNBEKANNT")
                        hits.append((category, rank_cell, name_cell))
    return hits


def ics_escape(text):
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("\n", "\\n")
    )


def ics_stamp(mtime):
    """'2026-08-26 05:23' -> '20260826T052300Z'."""
    try:
        return datetime.strptime(mtime, "%Y-%m-%d %H:%M").strftime(
            "%Y%m%dT%H%M%SZ"
        )
    except (ValueError, TypeError):
        return FALLBACK_STAMP


def build_vevent(iso_year, iso_week, entry):
    d_from = date.fromisoformat(entry["date_from"])
    d_to = date.fromisoformat(entry["date_to"])
    dtstart = d_from.strftime("%Y%m%d")
    dtend = (d_to + timedelta(days=1)).strftime("%Y%m%d")
    uid = f"dienstplan-{iso_year}-W{iso_week:02d}@wdr-besatzungsliste"
    stamp = ics_stamp(entry.get("mtime"))
    stand = entry.get("mtime") or "unbekannt"
    return (
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{stamp}\r\n"
        f"LAST-MODIFIED:{stamp}\r\n"
        f"SEQUENCE:{entry.get('sequence', 0)}\r\n"
        f"DTSTART;VALUE=DATE:{dtstart}\r\n"
        f"DTEND;VALUE=DATE:{dtend}\r\n"
        f"SUMMARY:{ics_escape(entry['summary'])}\r\n"
        f"DESCRIPTION:KW {iso_week}/{iso_year}\\, Stand: {ics_escape(stand)}\\, "
        f"Datei: {ics_escape(entry.get('file', '?'))}\r\n"
        "END:VEVENT\r\n"
    )


def wrap_calendar(vevents, name):
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Dienstplan Sync//DE\r\n"
        "CALSCALE:GREGORIAN\r\n"
        "METHOD:PUBLISH\r\n"
        f"X-WR-CALNAME:{ics_escape(name)}\r\n"
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H\r\n"
        "X-PUBLISHED-TTL:PT6H\r\n"
        + "".join(vevents)
        + "END:VCALENDAR\r\n"
    )


def category_to_summary(category):
    upper = category.upper()
    if "FREIE TAGE" in upper:
        return "Freie Tage"
    if "URLAUB" in upper:
        return "Urlaub"
    if "ABWESEND" in upper:
        return "Abwesend"
    if upper == "UNBEKANNT":
        return "Besatzungsliste: Spalte unklar (bitte PDF pruefen)"
    return f"Dienst auf {category}"


def is_dienst(summary):
    """Unklare Faelle bewusst zum Dienst zaehlen - dort fallen sie auf."""
    return summary.startswith("Dienst auf ") or summary.startswith("Besatzungsliste:")


def prune_state(state, weeks):
    """Entfernt Eintraege, deren Woche laenger als `weeks` zurueckliegt."""
    cutoff = date.today() - timedelta(weeks=weeks)
    for key in list(state):
        date_to = state[key].get("date_to")
        if date_to and date.fromisoformat(date_to) < cutoff:
            del state[key]
            print(f"{key}: aus dem Kalender entfernt (aelter als {weeks} Wochen)")


def main():
    username = os.environ.get("WDR_USER")
    password = os.environ.get("WDR_PASS")
    if not username or not password:
        sys.exit("Fehlt: Umgebungsvariablen WDR_USER / WDR_PASS (GitHub Secrets).")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    state = load_state()
    changed = False

    session = requests.Session()
    session.auth = (username, password)
    session.headers["User-Agent"] = "dienstplan-sync/2.0"

    index = fetch_index(session)
    print(f"Verzeichnislisting: {len(index)} Kalenderwochen gefunden\n")

    for iso_year, iso_week in iso_weeks_to_check(WEEKS_BACK, WEEKS_AHEAD):
        key = f"{iso_year}-W{iso_week:02d}"
        found = index.get((iso_year, iso_week))
        if not found:
            print(f"KW {iso_week}/{iso_year}: noch nicht verfuegbar")
            continue

        revision, href, filename, mtime = found
        prev = state.get(key)

        # Unveraendert? Dann kein Download.
        if prev and prev.get("file") == filename and prev.get("mtime") == mtime:
            print(f"KW {iso_week}/{iso_year}: unveraendert ({prev['summary']})")
            continue

        resp = session.get(f"{BASE_URL}/{href}", timeout=30)
        if resp.status_code != 200 or resp.content[:4] != b"%PDF":
            print(f"  Warnung: {filename} -> HTTP {resp.status_code}, uebersprungen")
            continue

        with pdfplumber.open(BytesIO(resp.content)) as pdf:
            d_from, d_to = parse_date_range(pdf)
            hits = find_status_for_name(pdf, TARGET_NAME)

        if not hits:
            print(
                f"KW {iso_week}/{iso_year} ({filename}): "
                f"'{TARGET_NAME}' nicht gefunden"
            )
            continue
        if len(hits) > 1:
            other = ", ".join(h[0] for h in hits[1:])
            print(f"  Hinweis: mehrere Treffer, weitere Kategorien: {other}")
        if not d_from or not d_to:
            print(
                f"  Warnung: KW {iso_week}/{iso_year} ({filename}) - "
                "Datumszeile im PDF nicht gefunden, kein Kalendereintrag"
            )

        category, rank, name_cell = hits[0]
        entry = {
            "date_from": d_from.isoformat() if d_from else None,
            "date_to": d_to.isoformat() if d_to else None,
            "category": category,
            "summary": category_to_summary(category),
            "file": filename,
            "mtime": mtime,
            "revision": revision,
            "sequence": (prev.get("sequence", 0) + 1) if prev else 0,
        }
        state[key] = entry
        changed = True
        rev_text = "Erstfassung" if revision == 0 else f"{revision}. Aenderung"
        print(
            f"KW {iso_week}/{iso_year}: {entry['summary']} "
            f"({rev_text}, Stand {mtime or 'unbekannt'})"
        )

    prune_state(state, PRUNE_WEEKS)

    dienst_events, frei_events = [], []
    for key, entry in sorted(state.items()):
        if not entry.get("date_from") or not entry.get("date_to"):
            continue
        iso_year, iso_week = key.split("-W")
        vevent = build_vevent(int(iso_year), int(iso_week), entry)
        target = dienst_events if is_dienst(entry["summary"]) else frei_events
        target.append(vevent)

    DIENST_ICS_PATH.write_text(
        wrap_calendar(dienst_events, "Dienst"), encoding="utf-8", newline=""
    )
    FREI_ICS_PATH.write_text(
        wrap_calendar(frei_events, "Frei"), encoding="utf-8", newline=""
    )
    save_state(state)

    print(f"\nDienst-Termine: {len(dienst_events)}, Frei-Termine: {len(frei_events)}")
    print(f"changed={str(changed).lower()}")


if __name__ == "__main__":
    main()
