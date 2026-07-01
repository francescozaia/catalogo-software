"""Arricchisce gli enti del catalogo con anagrafica e territorio da IndicePA/ISTAT.

Per la vista «Analisi per ente» servono i dati reali dell'amministrazione (non
quelli del fornitore nel publiccode): contatti ufficiali, **categoria** di
appartenenza IndicePA e **regione** di competenza territoriale.

Lo script lavora su tre dataset pubblici salvati in `data/` (non versionati,
aggiornabili a mano dalle fonti ufficiali):

  - `data/enti.csv`      anagrafica IndicePA di ~23.000 amministrazioni, con
                         codice IPA, denominazione, responsabile, tipologia,
                         PEC/email, sito, **Codice_Categoria** e
                         **Codice_catastale_comune** (fonte: IndicePA opendata).
  - `data/categorie.csv` vocabolario IndicePA delle categorie di ente
                         (Codice_categoria → Nome_categoria).
  - `data/comuni.csv`    elenco ISTAT dei comuni (delimitatore `;`), che mappa
                         il codice catastale del comune alla **regione** e alla
                         ripartizione geografica.

Per ogni codice IPA presente nel catalogo (data/software.jsonl) unisce le tre
fonti e scrive `data/indicepa.jsonl`: un record per codice IPA, indicizzabile
per `codice_ipa`. La regione è ricavata dal comune di sede via codice catastale
(fonte ISTAT), così da essere coerente con il filtro territoriale del pannello.

Uso:
    python3 indicepa.py
    python3 indicepa.py --input data/software.jsonl --out data/indicepa.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


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


def _clean(v: str | None) -> str:
    """Normalizza un valore CSV: strip e scarta i placeholder 'null'."""
    s = (v or "").strip()
    return "" if s.lower() == "null" else s


def load_categorie(path: Path) -> dict[str, str]:
    """Mappa Codice_categoria → Nome_categoria (vocabolario IndicePA)."""
    idx: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            code = _clean(row.get("Codice_categoria"))
            if code:
                idx[code] = _clean(row.get("Nome_categoria"))
    return idx


def load_comuni(path: Path) -> dict[str, dict]:
    """Mappa Codice catasto (maiuscolo) → riga comune ISTAT (delimitatore ';')."""
    idx: dict[str, dict] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f, delimiter=";"):
            code = _clean(row.get("Codice catasto")).upper()
            if code:
                idx[code] = row
    return idx


def parse_pec_and_mails(row: dict) -> tuple[str, list[dict]]:
    """Estrae la PEC (primo domicilio digitale) e tutte le email dalla riga enti.csv."""
    pec = ""
    mails: list[dict] = []
    for i in range(1, 6):
        addr = _clean(row.get(f"Mail{i}"))
        tipo = _clean(row.get(f"Tipo_Mail{i}"))
        if not addr:
            continue
        mails.append({"mail": addr, "tipo": tipo})
        if tipo.lower() == "pec" and not pec:
            pec = addr
    return pec, mails


def build_record(row: dict, categorie: dict[str, str], comuni: dict[str, dict]) -> dict:
    """Costruisce il record arricchito per un ente a partire dalla riga enti.csv."""
    pec, mails = parse_pec_and_mails(row)
    cod = _clean(row.get("Codice_IPA"))

    cat_code = _clean(row.get("Codice_Categoria"))
    categoria = categorie.get(cat_code, "")

    catasto = _clean(row.get("Codice_catastale_comune")).upper()
    comune_row = comuni.get(catasto, {})
    comune = _clean(comune_row.get("Comune"))
    provincia = _clean(comune_row.get("Provincia/Uts"))
    regione = _clean(comune_row.get("Regione"))
    ripartizione = _clean(comune_row.get("Ripartizione geografica"))

    responsabile = " ".join(x for x in [
        _clean(row.get("Titolo_responsabile")),
        _clean(row.get("Nome_responsabile")),
        _clean(row.get("Cognome_responsabile")),
    ] if x).strip()

    return {
        "codice_ipa": cod,
        "denominazione": _clean(row.get("Denominazione_ente")),
        "responsabile": responsabile,
        "comune": comune,
        "provincia": provincia,
        "regione": regione,
        "ripartizione": ripartizione,
        "tipologia": _clean(row.get("Tipologia")),
        "categoria_codice": cat_code,
        "categoria": categoria,
        "sito": _clean(row.get("Sito_istituzionale")),
        "pec": pec,
        "mails": mails,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Arricchimento enti da IndicePA/ISTAT (fonti locali)")
    p.add_argument("--input", default="data/software.jsonl", help="catalogo JSONL")
    p.add_argument("--out", default="data/indicepa.jsonl", help="output JSONL per codice IPA")
    p.add_argument("--enti", default="data/enti.csv", help="anagrafica IndicePA (CSV)")
    p.add_argument("--categorie", default="data/categorie.csv", help="vocabolario categorie IndicePA (CSV)")
    p.add_argument("--comuni", default="data/comuni.csv", help="comuni ISTAT (CSV, delimitatore ';')")
    args = p.parse_args(argv)

    for label, path in [("enti", args.enti), ("categorie", args.categorie), ("comuni", args.comuni)]:
        if not Path(path).exists():
            print(f"ERRORE: file {label} non trovato: {path}", file=sys.stderr)
            return 1

    codes = catalog_ipa_codes(Path(args.input))
    print(f"Codici IPA da arricchire: {len(codes)}")

    categorie = load_categorie(Path(args.categorie))
    comuni = load_comuni(Path(args.comuni))
    print(f"Categorie IndicePA: {len(categorie)} · Comuni ISTAT: {len(comuni)}")

    found: dict[str, dict] = {}
    missing_region: list[str] = []
    missing_categoria: list[str] = []
    with Path(args.enti).open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            cod = _clean(row.get("Codice_IPA"))
            if not cod or cod.lower() not in codes:
                continue
            rec = build_record(row, categorie, comuni)
            found[cod.lower()] = rec
            if not rec["regione"]:
                missing_region.append(cod)
            if not rec["categoria"]:
                missing_categoria.append(cod)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as jf:
        for rec in found.values():
            jf.write(json.dumps(rec, ensure_ascii=False) + "\n")

    missing = sorted(codes - set(found))
    print(f"Trovati in enti.csv: {len(found)}/{len(codes)}")
    if missing:
        print(f"Non trovati ({len(missing)}): {', '.join(missing[:20])}{' …' if len(missing) > 20 else ''}")
    if missing_region:
        print(f"Senza regione ({len(missing_region)}): {', '.join(missing_region[:20])}")
    if missing_categoria:
        print(f"Senza categoria ({len(missing_categoria)}): {', '.join(missing_categoria[:20])}")
    print(f"Scritto {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
