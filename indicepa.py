"""Arricchisce gli enti del catalogo con i contatti ufficiali da IndicePA.

Per la vista «Analisi per ente» servono i recapiti reali dell'amministrazione
(non quelli del fornitore nel publiccode). Questo script scarica il dataset
pubblico `amministrazioni` di IndicePA (indicepa.gov.it) — un file TSV con
~23.000 amministrazioni — e, per ogni codice IPA presente nel catalogo
(data/software.jsonl), estrae denominazione, responsabile, regione, tipologia,
sito istituzionale e PEC (domicilio digitale).

Produce data/indicepa.jsonl: un record per codice IPA trovato, indicizzabile
per `codice_ipa`.

Uso:
    python3 indicepa.py
    python3 indicepa.py --input data/software.jsonl --out data/indicepa.jsonl
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path

import requests

CKAN = "https://indicepa.gov.it/ipa-dati/api/3/action"
USER_AGENT = "catalogo-software-crawler/1.0 (+https://developers.italia.it)"


def codice_ipa_of(pc: dict) -> str | None:
    """Estrae il codice IPA dal publiccode (stessa logica del dashboard)."""
    org = pc.get("organisation") if isinstance(pc.get("organisation"), dict) else {}
    it_up = pc.get("IT") if isinstance(pc.get("IT"), dict) else {}
    it_low = pc.get("it") if isinstance(pc.get("it"), dict) else {}
    riuso_up = it_up.get("riuso") if isinstance(it_up.get("riuso"), dict) else {}
    riuso_low = it_low.get("riuso") if isinstance(it_low.get("riuso"), dict) else {}
    code = riuso_up.get("codiceIPA") or riuso_low.get("codiceIPA")
    if code:
        return str(code)
    uri = org.get("uri")
    if isinstance(uri, str) and uri.startswith("urn:x-italian-pa:"):
        return uri[len("urn:x-italian-pa:"):]
    return None


def catalog_ipa_codes(jsonl_path: Path) -> set[str]:
    """Codici IPA distinti nel catalogo (in minuscolo)."""
    codes: set[str] = set()
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            pc = (json.loads(line).get("publiccode") or {})
            c = codice_ipa_of(pc)
            if c:
                codes.add(c.strip().lower())
    return codes


def resolve_amministrazioni_url(session: requests.Session) -> str:
    """Ricava l'URL corrente del file amministrazioni.txt via API CKAN."""
    r = session.get(f"{CKAN}/package_show", params={"id": "amministrazioni"}, timeout=30)
    r.raise_for_status()
    resources = r.json()["result"]["resources"]
    for res in resources:
        if (res.get("format") or "").upper() == "TXT" and res.get("url"):
            return res["url"]
    raise RuntimeError("IndicePA: risorsa amministrazioni.txt non trovata")


def parse_pec_and_mails(row: dict) -> tuple[str, list[dict]]:
    """Estrae la PEC (primo domicilio digitale) e tutte le email dalla riga."""
    pec = ""
    mails: list[dict] = []
    for i in range(1, 6):
        addr = (row.get(f"mail{i}") or "").strip()
        tipo = (row.get(f"tipo_mail{i}") or "").strip()
        if not addr or addr.lower() == "null":
            continue
        mails.append({"mail": addr, "tipo": tipo})
        if tipo.lower() == "pec" and not pec:
            pec = addr
    return pec, mails


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Arricchimento enti da IndicePA")
    p.add_argument("--input", default="data/software.jsonl", help="catalogo JSONL")
    p.add_argument("--out", default="data/indicepa.jsonl", help="output JSONL per codice IPA")
    args = p.parse_args(argv)

    codes = catalog_ipa_codes(Path(args.input))
    print(f"Codici IPA da arricchire: {len(codes)}")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    url = resolve_amministrazioni_url(session)
    print(f"Scarico amministrazioni.txt …")
    resp = session.get(url, timeout=120)
    resp.raise_for_status()
    # TSV con BOM iniziale
    text = resp.content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")

    found: dict[str, dict] = {}
    for row in reader:
        cod = (row.get("cod_amm") or "").strip()
        if not cod or cod.lower() not in codes:
            continue
        pec, mails = parse_pec_and_mails(row)
        found[cod.lower()] = {
            "codice_ipa": cod,
            "denominazione": (row.get("des_amm") or "").strip(),
            "responsabile": " ".join(x for x in [
                (row.get("titolo_resp") or "").strip(),
                (row.get("nome_resp") or "").strip(),
                (row.get("cogn_resp") or "").strip(),
            ] if x and x.lower() != "null").strip(),
            "comune": (row.get("Comune") or "").strip(),
            "provincia": (row.get("Provincia") or "").strip(),
            "regione": (row.get("Regione") or "").strip(),
            "tipologia": (row.get("tipologia_amm") or "").strip(),
            "sito": (row.get("sito_istituzionale") or "").strip(),
            "pec": pec,
            "mails": mails,
        }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as jf:
        for rec in found.values():
            jf.write(json.dumps(rec, ensure_ascii=False) + "\n")

    missing = sorted(codes - set(found))
    print(f"Trovati in IndicePA: {len(found)}/{len(codes)}")
    if missing:
        print(f"Non trovati ({len(missing)}): {', '.join(missing[:20])}{' …' if len(missing) > 20 else ''}")
    print(f"Scritto {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
