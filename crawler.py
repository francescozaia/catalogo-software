"""Esporta tutti i software dal catalogo Developers Italia.

Usa l'API ufficiale https://api.developers.italia.it/v1/software (cursor-based
pagination, 25 risultati per pagina) e produce:

  - software.jsonl  : un oggetto JSON per riga con il record API completo
                      e il publiccode.yml parsato in `publiccode`
  - software.csv    : colonne appiattite per analisi rapida (Excel/Pandas)

Caratteristiche:
  - rate limit conservativo (~1 req/s, configurabile via --delay)
  - retry esponenziale su 429/5xx
  - resume automatico tramite checkpoint .cursor (riprende dall'ultima pagina)

Uso:
    python3 crawler.py                  # crawl completo
    python3 crawler.py --out outdir     # cambia directory di output
    python3 crawler.py --delay 0.5      # cambia delay tra le richieste
    python3 crawler.py --resume         # riprende da .cursor se presente
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import requests
import yaml

API_BASE = "https://api.developers.italia.it/v1"
SOFTWARE_ENDPOINT = f"{API_BASE}/software"
PAGE_SIZE = 25  # massimo accettato dall'API
USER_AGENT = "catalogo-software-crawler/1.0 (+https://developers.italia.it)"

CSV_COLUMNS = [
    "id",
    "name",
    "localisedName",
    "repository_url",
    "landingURL",
    "releaseDate",
    "softwareVersion",
    "developmentStatus",
    "softwareType",
    "license",
    "is_riuso",
    "codiceIPA",
    "organisation_uri",
    "platforms",
    "categories",
    "intendedAudience_countries",
    "intendedAudience_scope",
    "availableLanguages",
    "usedBy",
    "features_it",
    "shortDescription_it",
    "shortDescription_en",
    "maintenance_type",
    "maintenance_contractors",
    "maintenance_contacts",
    "spid",
    "pagopa",
    "cie",
    "anpr",
    "io",
    "conforme_lineeGuidaDesign",
    "conforme_modelloInteroperabilita",
    "conforme_misureMinimeSicurezza",
    "conforme_gdpr",
    "vitality_score",
    "active",
    "createdAt",
    "updatedAt",
    "publiccodeYmlVersion",
]


def fetch_page(session: requests.Session, cursor: str | None) -> dict[str, Any]:
    params = {"page_size": PAGE_SIZE}
    if cursor:
        params["page[after]"] = cursor
    backoff = 2.0
    for attempt in range(6):
        try:
            r = session.get(SOFTWARE_ENDPOINT, params=params, timeout=30)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            if attempt == 5:
                raise
            wait = backoff ** attempt
            print(f"  ! errore ({e}); ritento tra {wait:.1f}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def extract_cursor(next_link: str | None) -> str | None:
    if not next_link:
        return None
    # forma: "?page[after]=XYZ" oppure "...&page[after]=XYZ"
    marker = "page[after]="
    idx = next_link.find(marker)
    if idx == -1:
        return None
    return next_link[idx + len(marker):].split("&", 1)[0]


def safe_yaml_load(text: str) -> dict[str, Any]:
    if not text:
        return {}
    try:
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError as e:
        return {"_parse_error": str(e)}


def pick_lang(desc: dict, key: str, langs: tuple[str, ...] = ("it-IT", "en", "en-US")) -> str:
    if not isinstance(desc, dict):
        return ""
    for lang in langs:
        block = desc.get(lang)
        if isinstance(block, dict) and block.get(key):
            return str(block[key])
    # fallback: prima lingua disponibile
    for block in desc.values():
        if isinstance(block, dict) and block.get(key):
            return str(block[key])
    return ""


def pick_features(desc: dict, lang: str) -> list[str]:
    block = desc.get(lang) if isinstance(desc, dict) else None
    if isinstance(block, dict):
        feats = block.get("features")
        if isinstance(feats, list):
            return [str(f) for f in feats]
    return []


def joinlist(value: Any, sep: str = "; ") -> str:
    if isinstance(value, list):
        return sep.join(str(v) for v in value)
    if value is None:
        return ""
    return str(value)


def flatten_row(record: dict[str, Any]) -> dict[str, str]:
    pc = record.get("publiccode") or {}
    desc = pc.get("description") or {}
    legal = pc.get("legal") or {}
    organisation = pc.get("organisation") or {}
    maintenance = pc.get("maintenance") or {}
    localisation = pc.get("localisation") or {}
    audience = pc.get("intendedAudience") or {}
    it_ext = pc.get("IT") or {}
    conforme = it_ext.get("conforme") or {}
    piattaforme = it_ext.get("piattaforme") or {}
    riuso = it_ext.get("riuso") or {}

    # Logica ufficiale (italia/developers.italia.it searchEngine.js): è "a riuso"
    # se è valorizzato uno qualsiasi tra organisation.uri, IT.riuso.codiceIPA o
    # it.riuso.codiceIPA — senza richiedere il prefisso urn:x-italian-pa:.
    org_uri = organisation.get("uri") if isinstance(organisation, dict) else None
    riuso_low = pc.get("it") if isinstance(pc.get("it"), dict) else {}
    riuso_low = riuso_low.get("riuso") if isinstance(riuso_low.get("riuso"), dict) else {}
    riuso_code = (riuso.get("codiceIPA") if isinstance(riuso, dict) else None) or riuso_low.get("codiceIPA")
    is_riuso = bool(org_uri or riuso_code)
    if riuso_code:
        codice_ipa = str(riuso_code)
    elif isinstance(org_uri, str) and org_uri.startswith("urn:x-italian-pa:"):
        codice_ipa = org_uri[len("urn:x-italian-pa:"):]
    else:
        codice_ipa = ""

    contractors = maintenance.get("contractors") if isinstance(maintenance, dict) else None
    contractor_names: list[str] = []
    if isinstance(contractors, list):
        for c in contractors:
            if isinstance(c, dict) and c.get("name"):
                contractor_names.append(str(c["name"]))
    elif isinstance(contractors, dict) and contractors.get("name"):
        contractor_names.append(str(contractors["name"]))

    contacts = maintenance.get("contacts") if isinstance(maintenance, dict) else None
    contact_emails: list[str] = []
    if isinstance(contacts, list):
        for c in contacts:
            if isinstance(c, dict):
                email = c.get("email") or c.get("name")
                if email:
                    contact_emails.append(str(email))

    return {
        "id": record.get("id", ""),
        "name": str(pc.get("name", "")),
        "localisedName": pick_lang(desc, "localisedName"),
        "repository_url": str(pc.get("url") or record.get("url") or ""),
        "landingURL": str(pc.get("landingURL", "")),
        "releaseDate": str(pc.get("releaseDate", "")),
        "softwareVersion": str(pc.get("softwareVersion", "")),
        "developmentStatus": str(pc.get("developmentStatus", "")),
        "softwareType": str(pc.get("softwareType", "")),
        "license": str(legal.get("license", "")) if isinstance(legal, dict) else "",
        "is_riuso": "true" if is_riuso else "false",
        "codiceIPA": str(codice_ipa or ""),
        "organisation_uri": str(org_uri or ""),
        "platforms": joinlist(pc.get("platforms")),
        "categories": joinlist(pc.get("categories")),
        "intendedAudience_countries": joinlist(audience.get("countries") if isinstance(audience, dict) else None),
        "intendedAudience_scope": joinlist(audience.get("scope") if isinstance(audience, dict) else None),
        "availableLanguages": joinlist(localisation.get("availableLanguages") if isinstance(localisation, dict) else None),
        "usedBy": joinlist(pc.get("usedBy")),
        "features_it": joinlist(pick_features(desc, "it-IT")),
        "shortDescription_it": pick_lang(desc, "shortDescription", ("it-IT",)),
        "shortDescription_en": pick_lang(desc, "shortDescription", ("en", "en-US")),
        "maintenance_type": str(maintenance.get("type", "")) if isinstance(maintenance, dict) else "",
        "maintenance_contractors": joinlist(contractor_names),
        "maintenance_contacts": joinlist(contact_emails),
        "spid": str(piattaforme.get("spid", "")) if isinstance(piattaforme, dict) else "",
        "pagopa": str(piattaforme.get("pagopa", "")) if isinstance(piattaforme, dict) else "",
        "cie": str(piattaforme.get("cie", "")) if isinstance(piattaforme, dict) else "",
        "anpr": str(piattaforme.get("anpr", "")) if isinstance(piattaforme, dict) else "",
        "io": str(piattaforme.get("io", "")) if isinstance(piattaforme, dict) else "",
        "conforme_lineeGuidaDesign": str(conforme.get("lineeGuidaDesign", "")) if isinstance(conforme, dict) else "",
        "conforme_modelloInteroperabilita": str(conforme.get("modelloInteroperabilita", "")) if isinstance(conforme, dict) else "",
        "conforme_misureMinimeSicurezza": str(conforme.get("misureMinimeSicurezza", "")) if isinstance(conforme, dict) else "",
        "conforme_gdpr": str(conforme.get("gdpr", "")) if isinstance(conforme, dict) else "",
        "vitality_score": json.dumps(record.get("vitality")) if record.get("vitality") is not None else "",
        "active": str(record.get("active", "")),
        "createdAt": str(record.get("createdAt", "")),
        "updatedAt": str(record.get("updatedAt", "")),
        "publiccodeYmlVersion": str(pc.get("publiccodeYmlVersion", "")),
    }


def crawl(out_dir: Path, delay: float, resume: bool) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "software.jsonl"
    csv_path = out_dir / "software.csv"
    cursor_path = out_dir / ".cursor"

    cursor: str | None = None
    mode_jsonl = "w"
    mode_csv = "w"
    if resume and cursor_path.exists():
        cursor = cursor_path.read_text().strip() or None
        if cursor:
            mode_jsonl = "a"
            mode_csv = "a"
            print(f"Resume: riprendo dal cursore salvato ({cursor[:24]}...)")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    total = 0
    page_num = 0
    csv_header_written = mode_csv == "a" and csv_path.exists() and csv_path.stat().st_size > 0

    with jsonl_path.open(mode_jsonl, encoding="utf-8") as jf, \
         csv_path.open(mode_csv, encoding="utf-8", newline="") as cf:
        writer = csv.DictWriter(cf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if not csv_header_written:
            writer.writeheader()

        while True:
            page_num += 1
            payload = fetch_page(session, cursor)
            records = payload.get("data", [])
            for rec in records:
                pc_text = rec.get("publiccodeYml", "")
                rec["publiccode"] = safe_yaml_load(pc_text)
                jf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                writer.writerow(flatten_row(rec))
                total += 1
            jf.flush()
            cf.flush()

            next_cursor = extract_cursor((payload.get("links") or {}).get("next"))
            print(f"  pagina {page_num}: +{len(records)} record (totale: {total})")

            if not next_cursor:
                cursor_path.unlink(missing_ok=True)
                break
            cursor_path.write_text(next_cursor)
            cursor = next_cursor
            time.sleep(delay)

    print(f"\nFatto. {total} software esportati in:")
    print(f"  - {jsonl_path}")
    print(f"  - {csv_path}")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crawler catalogo software Developers Italia")
    parser.add_argument("--out", default="data", help="directory di output (default: data/)")
    parser.add_argument("--delay", type=float, default=1.0, help="secondi tra le richieste (default: 1.0)")
    parser.add_argument("--resume", action="store_true", help="riprende dal cursore salvato")
    args = parser.parse_args(list(argv) if argv is not None else None)
    crawl(Path(args.out), args.delay, args.resume)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
