#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VgV-Radar Bau
=============
Ruft Bekanntmachungen über die OpenData-Schnittstelle des Bekanntmachungsservice
(Datenservice Öffentlicher Einkauf, oeffentlichevergabe.de) ab, filtert auf
Planungs-/Bauleistungen (CPV) und erzeugt ein statisches HTML-Dashboard.

Nutzung:
    python3 vgv_radar.py                 # letzte 14 Tage abrufen, Dashboard bauen
    python3 vgv_radar.py --tage 30       # längerer Zeitraum
    python3 vgv_radar.py --demo          # Demo-Daten statt Live-Abruf (Vorschau)
    python3 vgv_radar.py --mit-bau       # zusätzlich Bauleistungen (CPV 45...)

Abhängigkeiten: nur Python-Standardbibliothek (urllib, zipfile, json).

WICHTIG:
- Endpoint und Parameter bitte einmalig gegen die aktuelle Swagger-Doku prüfen:
  https://oeffentlichevergabe.de/documentation/swagger-ui/opendata/index.html
- Bitte fair abrufen (1 Request pro Tag/Datum, kurze Pausen). Die Daten sind
  Open Data, aber der Dienst ist eine öffentliche Infrastruktur.
"""

import argparse
import io
import json
import re
import sys
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ---------------------------------------------------------------- Konfiguration

BASE_URL = "https://oeffentlichevergabe.de"
# OpenData-Export pro Veröffentlichungstag (Format lt. Swagger-Doku):
#   GET /api/notice-exports?pubDay=YYYY-MM-DD&format=ocds.zip
EXPORT_URL = BASE_URL + "/api/notice-exports?pubDay={tag}&format=ocds.zip"
DETAIL_URL = BASE_URL + "/ui/de/search/details?noticeId={nid}"
SEARCH_URL = BASE_URL + "/ui/de/search"

USER_AGENT = "VgV-Radar-Bau/0.1 (privates Analyse-Dashboard; Kontakt siehe Impressum)"
PAUSE_SEKUNDEN = 1.5          # Pause zwischen Tages-Abrufen
TIMEOUT = 60

# CPV-Präfixe Planungsleistungen Bau (Default) und optional Bauleistungen
CPV_PLANUNG = ("712", "713", "715", "716")
CPV_BAU = ("45",)

CPV_LABELS = [
    ("7122", "Objektplanung Gebäude"),
    ("7124", "Architektur & Planung"),
    ("7125", "Bauüberwachung/Vermessung"),
    ("712", "Architekturleistungen"),
    ("7132", "Tragwerksplanung"),
    ("7131", "Ingenieurberatung"),
    ("7133", "TGA/Fachingenieur"),
    ("713", "Ingenieurleistungen"),
    ("715", "Baubezogene Dienstleistungen"),
    ("716", "Technische Prüfung/Analyse"),
    ("45", "Bauleistungen"),
]

NUTS_BUNDESLAND = {
    "DE1": "Baden-Württemberg", "DE2": "Bayern", "DE3": "Berlin",
    "DE4": "Brandenburg", "DE5": "Bremen", "DE6": "Hamburg",
    "DE7": "Hessen", "DE8": "Mecklenburg-Vorpommern", "DE9": "Niedersachsen",
    "DEA": "Nordrhein-Westfalen", "DEB": "Rheinland-Pfalz", "DEC": "Saarland",
    "DED": "Sachsen", "DEE": "Sachsen-Anhalt", "DEF": "Schleswig-Holstein",
    "DEG": "Thüringen",
}

VERFAHREN_LABELS = {
    "open": "Offenes Verfahren",
    "restricted": "Nichtoffenes Verfahren",
    "negotiated": "Verhandlungsverfahren",
    "competitive-dialogue": "Wettbewerblicher Dialog",
    "innovation-partnership": "Innovationspartnerschaft",
    "neg-w-call": "Verhandlungsverfahren mit Teilnahmewettbewerb",
    "neg-wo-call": "Verhandlungsverfahren ohne Teilnahmewettbewerb",
}

HIER = Path(__file__).parent
TEMPLATE = HIER / "dashboard_template.html"
AUSGABE_HTML = HIER / "dashboard.html"
AUSGABE_JSON = HIER / "vgv_daten.json"


# ---------------------------------------------------------------- Hilfsfunktionen

def cpv_label(cpv: str) -> str:
    for prefix, label in CPV_LABELS:
        if cpv.startswith(prefix):
            return label
    return "Sonstige"


def bundesland_aus_nuts(nuts: str) -> str:
    if not nuts:
        return ""
    return NUTS_BUNDESLAND.get(nuts[:3].upper(), "")


def hole(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def erste(d, *pfad, default=None):
    """Defensiver Zugriff auf verschachtelte dicts/lists."""
    cur = d
    for p in pfad:
        if isinstance(cur, dict):
            cur = cur.get(p)
        elif isinstance(cur, list):
            cur = cur[p] if isinstance(p, int) and len(cur) > p else None
        else:
            return default
        if cur is None:
            return default
    return cur


# ---------------------------------------------------------------- OCDS-Verarbeitung

def parse_release(rel: dict, cpv_prefixe) -> dict | None:
    tender = rel.get("tender") or {}

    # Nur Auftragsbekanntmachungen (keine Zuschläge/Berichtigungen als eigene Zeile)
    tags = rel.get("tag") or []
    if tags and not any(t in ("tender", "planning") for t in tags):
        return None

    # CPV ermitteln: Haupt-Klassifikation oder Items
    cpvs = []
    haupt = erste(tender, "classification", "id")
    if haupt:
        cpvs.append(str(haupt))
    for item in tender.get("items") or []:
        c = erste(item, "classification", "id")
        if c:
            cpvs.append(str(c))
        for zc in item.get("additionalClassifications") or []:
            if zc.get("id"):
                cpvs.append(str(zc["id"]))
    cpv = next((c for c in cpvs if c.startswith(cpv_prefixe)), None)
    if not cpv:
        return None

    # Auftraggeber
    ag = erste(rel, "buyer", "name") or ""
    parteien = rel.get("parties") or []
    if not ag:
        for p in parteien:
            if "buyer" in (p.get("roles") or []):
                ag = p.get("name") or ""
                break

    # Ort / Bundesland: Buyer-Adresse, sonst Liefer-/Erfüllungsort der Items
    ort, nuts = "", ""
    for p in parteien:
        if "buyer" in (p.get("roles") or []):
            adr = p.get("address") or {}
            ort = adr.get("locality") or ort
            nuts = adr.get("region") or adr.get("nutsCode") or nuts
    for item in tender.get("items") or []:
        adr = item.get("deliveryAddress") or {}
        ort = ort or adr.get("locality") or ""
        nuts = nuts or adr.get("region") or adr.get("nutsCode") or ""

    frist = (erste(tender, "tenderPeriod", "endDate") or "")[:10]
    veroeffentlicht = (rel.get("date") or "")[:10]

    verfahren = tender.get("procurementMethodDetails") or \
        VERFAHREN_LABELS.get(tender.get("procurementMethod") or "", "") or \
        (tender.get("procurementMethod") or "")
    verfahren = VERFAHREN_LABELS.get(verfahren, verfahren)

    # Link auf die Detailseite: noticeId aus Release-/Dokument-Feldern ableiten
    nid = None
    for doc in tender.get("documents") or []:
        url = doc.get("url") or ""
        m = re.search(r"noticeId=([0-9a-fA-F-]{36})", url)
        if m:
            nid = m.group(1)
            break
    if not nid:
        m = re.search(r"([0-9a-fA-F-]{36})", str(rel.get("id") or ""))
        nid = m.group(1) if m else None
    url = DETAIL_URL.format(nid=nid) if nid else SEARCH_URL

    return {
        "id": rel.get("ocid") or rel.get("id") or "",
        "titel": (tender.get("title") or rel.get("title") or "Ohne Titel").strip(),
        "auftraggeber": ag.strip(),
        "ort": (ort or "").strip(),
        "bundesland": bundesland_aus_nuts(nuts),
        "cpv": cpv,
        "cpv_label": cpv_label(cpv),
        "verfahren": verfahren,
        "frist": frist,
        "veroeffentlicht": veroeffentlicht,
        "url": url,
    }


def lade_tag(tag: date, cpv_prefixe) -> list[dict]:
    url = EXPORT_URL.format(tag=tag.isoformat())
    try:
        daten = hole(url)
    except HTTPError as e:
        if e.code == 404:
            print(f"  {tag}: keine Daten (404)")
            return []
        print(f"  {tag}: HTTP {e.code} – {e.reason}", file=sys.stderr)
        return []
    except URLError as e:
        print(f"  {tag}: Netzwerkfehler – {e.reason}", file=sys.stderr)
        return []

    ergebnisse = []
    try:
        with zipfile.ZipFile(io.BytesIO(daten)) as zf:
            for name in zf.namelist():
                if not name.lower().endswith(".json"):
                    continue
                try:
                    paket = json.loads(zf.read(name).decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                releases = paket.get("releases") or ([paket] if "tender" in paket else [])
                for rel in releases:
                    n = parse_release(rel, cpv_prefixe)
                    if n:
                        ergebnisse.append(n)
    except zipfile.BadZipFile:
        # Möglicherweise direkt JSON statt ZIP
        try:
            paket = json.loads(daten.decode("utf-8"))
            for rel in paket.get("releases") or []:
                n = parse_release(rel, cpv_prefixe)
                if n:
                    ergebnisse.append(n)
        except Exception:
            print(f"  {tag}: unerwartetes Antwortformat", file=sys.stderr)
    print(f"  {tag}: {len(ergebnisse)} passende Bekanntmachungen")
    return ergebnisse


# ---------------------------------------------------------------- Demo-Daten

def demo_daten() -> list[dict]:
    heute = date.today()

    def d(tage):
        return (heute + timedelta(days=tage)).isoformat()

    eintraege = [
        ("Generalsanierung Gesamtschule Nord – Objektplanung Gebäude LPH 2–9",
         "Stadt Beispelhausen, Gebäudemanagement", "Beispelhausen",
         "Nordrhein-Westfalen", "71221000", 12, -3),
        ("Neubau Feuerwache 3 – Tragwerksplanung",
         "Kreisstadt Musterberg", "Musterberg",
         "Hessen", "71327000", 6, -1),
        ("Erweiterung Universitätsklinikum – TGA-Fachplanung ELT (Anlagengruppen 4–6)",
         "Universitätsklinikum Demoland AöR", "Demoland",
         "Bayern", "71321000", 21, -5),
        ("Machbarkeitsstudie Rathausquartier – Generalplanung",
         "Gemeinde Alsterfeld (Demo)", "Alsterfeld",
         "Schleswig-Holstein", "71240000", 4, -2),
        ("Neubau Kindertagesstätte Weststadt – Objektplanung LPH 1–9",
         "Stadt Neuenbrück, Amt für Hochbau", "Neuenbrück",
         "Niedersachsen", "71221000", 17, -6),
        ("Brandschutztechnische Prüfung Bestandsgebäude Verwaltungscampus",
         "Landesbetrieb Bau- und Liegenschaften (Demo)", "Erfingen",
         "Thüringen", "71630000", 9, -4),
        ("Rahmenvereinbarung Vermessungsleistungen Straßenbauprojekte",
         "Landkreis Obertal", "Obertal",
         "Baden-Württemberg", "71250000", 28, -8),
        ("Sanierung Hallenbad Mitte – Bauphysik und Schallschutz",
         "Stadtwerke Fließbach GmbH (Sektorenauftraggeber, Demo)", "Fließbach",
         "Sachsen", "71313000", 2, -1),
        ("Neubau Rechenzentrum Forschungsallianz – Objektplanung technische Gebäude",
         "Forschungsverbund Rheinland e. V. (Demo)", "Bornheim",
         "Nordrhein-Westfalen", "71221000", 33, -10),
        ("Umbau und Erweiterung Grundschule Am Anger – Bauleistungen Rohbau (Los 1)",
         "Gemeinde Hügelsheim (Demo)", "Hügelsheim",
         "Rheinland-Pfalz", "45210000", 14, -7),
    ]
    out = []
    for i, (titel, ag, ort, bl, cpv, frist_in, veroeff_vor) in enumerate(eintraege, 1):
        out.append({
            "id": f"demo-{i:03d}",
            "titel": titel,
            "auftraggeber": ag,
            "ort": ort,
            "bundesland": bl,
            "cpv": cpv,
            "cpv_label": cpv_label(cpv),
            "verfahren": "Verhandlungsverfahren mit Teilnahmewettbewerb"
                         if cpv.startswith("71") else "Offenes Verfahren",
            "frist": d(frist_in),
            "veroeffentlicht": d(veroeff_vor),
            "url": SEARCH_URL,
        })
    return out


# ---------------------------------------------------------------- Dashboard bauen

def schreibe_dashboard(notices: list[dict], demo: bool):
    if not TEMPLATE.exists():
        sys.exit(f"Template fehlt: {TEMPLATE}")
    html = TEMPLATE.read_text(encoding="utf-8")

    meta = {
        "stand": datetime.now().strftime("%d.%m.%Y, %H:%M Uhr"),
        "demo": demo,
    }
    daten_js = json.dumps(notices, ensure_ascii=False)
    meta_js = json.dumps(meta, ensure_ascii=False)

    html = re.sub(r"/\*META_START\*/.*?/\*META_END\*/",
                  f"/*META_START*/{meta_js}/*META_END*/", html, flags=re.S)
    html = re.sub(r"/\*DATA_START\*/.*?/\*DATA_END\*/",
                  f"/*DATA_START*/{daten_js}/*DATA_END*/", html, flags=re.S)

    AUSGABE_HTML.write_text(html, encoding="utf-8")
    AUSGABE_JSON.write_text(json.dumps(notices, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    print(f"\nDashboard geschrieben: {AUSGABE_HTML}")
    print(f"Rohdaten geschrieben:  {AUSGABE_JSON}")


def main():
    ap = argparse.ArgumentParser(description="VgV-Radar Bau – Dashboard-Generator")
    ap.add_argument("--tage", type=int, default=14,
                    help="Zeitraum in Tagen rückwirkend (Default: 14)")
    ap.add_argument("--demo", action="store_true",
                    help="Demo-Daten verwenden statt Live-Abruf")
    ap.add_argument("--mit-bau", action="store_true",
                    help="zusätzlich Bauleistungen (CPV 45...) einschließen")
    args = ap.parse_args()

    cpv_prefixe = CPV_PLANUNG + (CPV_BAU if args.mit_bau else ())

    if args.demo:
        notices = demo_daten()
        print(f"Demo-Modus: {len(notices)} Beispiel-Einträge")
        schreibe_dashboard(notices, demo=True)
        return

    print(f"Rufe Bekanntmachungen der letzten {args.tage} Tage ab …")
    alle: dict[str, dict] = {}
    for i in range(args.tage, 0, -1):
        tag = date.today() - timedelta(days=i)
        for n in lade_tag(tag, cpv_prefixe):
            alle[n["id"] or n["titel"]] = n   # Dedupe: letzte Version gewinnt
        time.sleep(PAUSE_SEKUNDEN)

    notices = list(alle.values())
    heute = date.today().isoformat()
    aktiv = [n for n in notices if not n["frist"] or n["frist"] >= heute]
    print(f"\nGesamt: {len(notices)} Verfahren, davon {len(aktiv)} mit laufender Frist")
    schreibe_dashboard(aktiv, demo=False)


if __name__ == "__main__":
    main()
