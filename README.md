# VgV-Radar Bau

Öffentliches Dashboard aller aktuell veröffentlichten EU-weiten Vergabeverfahren
für Planungsleistungen im Bauwesen. Datenquelle: OpenData-Schnittstelle des
Bekanntmachungsservice (Datenservice Öffentlicher Einkauf, oeffentlichevergabe.de) –
frei zugänglich, ohne Login oder Registrierung.

## Dateien

| Datei | Zweck |
|---|---|
| `vgv_radar.py` | Abruf, CPV-Filterung, Dashboard-Generierung (nur Python-Standardbibliothek) |
| `dashboard_template.html` | HTML-Vorlage mit Daten-Markern – hier Design anpassen |
| `dashboard.html` | Generiertes Dashboard (dieses File veröffentlichen) |
| `vgv_daten.json` | Gefilterte Rohdaten für Weiterverarbeitung |

## Schnellstart

```bash
python3 vgv_radar.py --demo      # Vorschau mit Beispieldaten
python3 vgv_radar.py             # Live-Abruf, letzte 14 Tage
python3 vgv_radar.py --tage 30   # längerer Zeitraum
python3 vgv_radar.py --mit-bau   # zusätzlich Bauleistungen (CPV 45...)
```

Danach `dashboard.html` im Browser öffnen bzw. auf den Webspace hochladen.

## Vor dem ersten Live-Lauf prüfen

Der Export-Endpoint ist im Skript als
`GET /api/notice-exports?pubDay=YYYY-MM-DD&format=ocds.zip` hinterlegt.
Bitte einmal gegen die aktuelle Swagger-Doku abgleichen (Pfad oder Parameter
können sich ändern):
https://oeffentlichevergabe.de/documentation/swagger-ui/opendata/index.html

Falls sich der Pfad geändert hat: nur die Konstante `EXPORT_URL` oben im
Skript anpassen. Der OCDS-Parser ist defensiv geschrieben und übersteht
fehlende Felder.

## Automatisierung (täglich aktuell)

Einfachste Variante – Cronjob auf einem beliebigen Server/NAS/Raspberry:

```cron
15 6 * * * cd /pfad/zu/vgv-radar && python3 vgv_radar.py --tage 21 && cp dashboard.html /var/www/html/index.html
```

Serverlose Variante – GitHub Actions (kostenlos) + GitHub Pages:
täglicher Workflow führt das Skript aus und committet `dashboard.html`
in den `gh-pages`-Branch. Kein eigener Server nötig.

## Filterlogik

Standard-CPV-Präfixe (Planungsleistungen Bau):
- 712 – Architekturleistungen (u. a. 71221 Objektplanung Gebäude, 71240 Architektur & Planung)
- 713 – Ingenieurleistungen (u. a. 71327 Tragwerk, 71321 TGA)
- 715 – baubezogene Dienstleistungen
- 716 – technische Prüfung und Analyse
- optional 45 – Bauleistungen (`--mit-bau`)

Anpassen: Konstanten `CPV_PLANUNG` / `CPV_BAU` / `CPV_LABELS` im Skript.

## Rechtliches / Fairness

- Die Bekanntmachungsdaten sind Open Data und zur Nachnutzung bestimmt;
  Nutzungsbedingungen unter oeffentlichevergabe.de (Open-Data-Richtlinie) beachten.
- Quellenangabe im Dashboard-Footer nicht entfernen.
- Abrufrate niedrig halten (Skript pausiert zwischen Tages-Abrufen).
- Maßgeblich ist immer die Originalbekanntmachung – Disclaimer im Footer.
