"""Kleinanzeigen abrufen und parsen (Suchliste + Detailseite)."""
import html as htmllib
import json
import re
import subprocess
import time
import urllib.parse

BASE = "https://www.kleinanzeigen.de"
# Umkreise, die Kleinanzeigen in der Suche anbietet
RADII = (5, 10, 20, 30, 50, 100, 150, 200)
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
# Nominatim verlangt einen User-Agent, der die Anwendung erkennbar macht.
GEOCODER_UA = "kleinanzeigen-bot (https://github.com/vitoleonardo/kleinanzeigen-bot)"


class LocationError(Exception):
    """KA_LOCATION lässt sich nicht eindeutig auf einen Kleinanzeigen-Ort abbilden."""


def fetch(url, retries=2, timeout=30, ua=UA):
    """Holt eine Seite per curl. Gibt (http_status, html) zurück; Status 0 = Netzwerkfehler."""
    status, body = 0, ""
    for attempt in range(retries + 1):
        try:
            p = subprocess.run(
                ["curl", "-s", "-L", "--compressed", "-m", str(timeout), "-A", ua,
                 "-H", "Accept-Language: de-DE,de;q=0.9", "-w", "\n%{http_code}", url],
                capture_output=True, text=True, timeout=timeout + 10)
            body, _, code = p.stdout.rpartition("\n")
            status = int(code) if code.strip().isdigit() else 0
        except (subprocess.TimeoutExpired, OSError):
            status, body = 0, ""
        # Nur bei Netzwerk-/Serverfehlern erneut versuchen, nicht bei 403/429
        if status == 200 or 400 <= status < 500:
            break
        if attempt < retries:
            time.sleep(5 * (attempt + 1))
    return status, body


def _text(fragment):
    return htmllib.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def parse_list(page):
    """Parst die Suchergebnisseite in eine Liste von Anzeigen-Dicts (neueste zuerst)."""
    ads = []
    for chunk in page.split("<article")[1:]:
        chunk = chunk[:chunk.find("</article>")]
        m_id = re.search(r'data-adid="(\d+)"', chunk)
        m_href = re.search(r'data-href="([^"]+)"', chunk)
        if not m_id or not m_href:
            continue
        ad = {"id": m_id.group(1), "url": BASE + htmllib.unescape(m_href.group(1)),
              "title": "", "description": "", "image": "", "location": "",
              "distance": "", "posted": ""}
        m_ld = re.search(r'<script type="application/ld\+json">(.*?)</script>', chunk, re.S)
        if m_ld:
            try:
                ld = json.loads(m_ld.group(1))
                ad["title"] = ld.get("title", "").strip()
                ad["description"] = ld.get("description", "").strip()
                ad["image"] = ld.get("contentUrl", "")
            except json.JSONDecodeError:
                pass
        if not ad["title"]:
            m_h3 = re.search(r"<h3[^>]*>(.*?)</h3>", chunk, re.S)
            ad["title"] = _text(m_h3.group(1)) if m_h3 else ""
        # Ort + Entfernung + Zeit stehen als <span> ohne SVG-Icons
        spans = [_text(s) for s in re.findall(
            r"<span[^>]*>(.*?)</span>", re.sub(r"<svg.*?</svg>", "", chunk, flags=re.S), re.S)]
        spans = [s for s in spans if s]
        for s in spans:
            if not ad["location"] and re.match(r"^\d{5}\s", s):
                ad["location"] = s
            elif not ad["distance"] and re.match(r"^\(.*km\)$", s):
                ad["distance"] = s.strip("()")
            elif not ad["posted"] and re.search(r"(Heute|Gestern|\d{2}\.\d{2}\.\d{4})", s):
                ad["posted"] = s
        ads.append(ad)
    return ads


def parse_detail(page):
    """Liest Koordinaten und vollständige Beschreibung von der Detailseite."""
    out = {"lat": None, "lon": None, "description": "", "image": ""}
    m_lat = re.search(r'latitude"\s+content="([\d.\-]+)"', page)
    m_lon = re.search(r'longitude"\s+content="([\d.\-]+)"', page)
    if m_lat and m_lon:
        out["lat"], out["lon"] = float(m_lat.group(1)), float(m_lon.group(1))
    m_desc = re.search(r'id="viewad-description-text"[^>]*>(.*?)</p>', page, re.S)
    if m_desc:
        desc = re.sub(r"<br\s*/?>", "\n", m_desc.group(1))
        out["description"] = _text(desc)
    m_img = re.search(r'<meta property="og:image" content="([^"]+)"', page)
    if m_img:
        out["image"] = htmllib.unescape(m_img.group(1))
    return out


def pick_location(query, suggestions):
    """Wählt aus den Ortsvorschlägen {"_945": "Köln - Nordrhein-Westfalen", ...} genau einen."""
    places = {key.lstrip("_"): label for key, label in suggestions.items() if key != "_0"}
    wanted = query.casefold()
    exact = {i: label for i, label in places.items()
             if wanted in (label.casefold(), label.split(" - ")[0].casefold())}
    candidates = exact or places
    if len(candidates) == 1:
        return next(iter(candidates.items()))
    if not candidates:
        raise LocationError(f"KA_LOCATION: Ort „{query}“ nicht gefunden. Beispiel: Köln oder 50672")
    options = "\n".join(f"  KA_LOCATION={label}" for label in candidates.values())
    raise LocationError(f"KA_LOCATION: „{query}“ ist nicht eindeutig. Trag eine dieser Zeilen ein:\n{options}")


def find_location(query):
    """Löst Ort oder PLZ über die Ortssuche von Kleinanzeigen auf. Gibt (id, label) zurück."""
    q = urllib.parse.urlencode({"query": query})
    status, body = fetch(f"{BASE}/s-ort-empfehlungen.json?{q}", retries=1)
    if status != 200:
        raise LocationError(f"Ortssuche von Kleinanzeigen nicht erreichbar (HTTP {status}).")
    try:
        suggestions = json.loads(body)
    except json.JSONDecodeError as e:
        raise LocationError("Ortssuche von Kleinanzeigen lieferte eine unerwartete Antwort.") from e
    if not isinstance(suggestions, dict):
        raise LocationError("Ortssuche von Kleinanzeigen lieferte eine unerwartete Antwort.")
    return pick_location(query, suggestions)


def search_url(location_id, radius_km):
    return f"{BASE}/s-zu-verschenken/sortierung:neueste/c192l{location_id}r{radius_km}"


def _nominatim(**params):
    q = urllib.parse.urlencode({**params, "countrycodes": "de", "format": "json", "limit": 1})
    status, body = fetch("https://nominatim.openstreetmap.org/search?" + q,
                         retries=1, timeout=15, ua=GEOCODER_UA)
    if status != 200:
        return None
    try:
        hits = json.loads(body)
        return (float(hits[0]["lat"]), float(hits[0]["lon"])) if hits else None
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def geocode_plz(plz):
    """Mittelpunkt einer PLZ über Nominatim (OpenStreetMap). Gibt (lat, lon) oder None."""
    return _nominatim(postalcode=plz)


def geocode_location(label):
    """Mittelpunkt für einen Ortsnamen von Kleinanzeigen, z. B. „Köln - Nordrhein-Westfalen“."""
    m = re.match(r"\d{5}\b", label)
    return geocode_plz(m.group()) if m else _nominatim(q=label.replace(" - ", ", "))
