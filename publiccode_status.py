"""Recupera lo stato di validazione del publiccode.yml per ogni software del catalogo.

Usa l'API pubblica dei log di Developers Italia:

    GET https://api.developers.italia.it/v1/software/{id}/logs?page_size=1

Come il badge ufficiale del portale, legge soltanto il log più recente
(`data[0]`): se il messaggio contiene "GOOD publiccode.yml" il file è valido,
altrimenti riporta il problema rilevato dal crawler.

Produce data/publiccode.jsonl (un record per software):

    {"id": "...", "fetched": true, "message": "<testo dell'ultimo log o ''>"}

Lo *stato* (valido/avviso/errore/sconosciuto), la *severità* e la *categoria*
del problema NON sono calcolati qui: vengono derivati a build-time da
build_dashboard.py a partire dal messaggio grezzo, così le regole di
classificazione si possono affinare senza dover rifare il fetch.

Uso:
    python3 publiccode_status.py                 # tutto il catalogo
    python3 publiccode_status.py --limit 20      # primi 20 (test)
    python3 publiccode_status.py --delay 0.5     # ritmo richieste
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import requests

API_BASE = "https://api.developers.italia.it/v1"
USER_AGENT = "catalogo-software-crawler/1.0 (+https://developers.italia.it)"


def fetch_latest_log(session: requests.Session, software_id: str) -> tuple[bool, str]:
    """Restituisce (fetched, message) per l'ultimo log del software.

    `fetched` è False solo se la richiesta fallisce in modo definitivo.
    `message` è "" se non esistono log per quel software.
    """
    url = f"{API_BASE}/software/{software_id}/logs"
    params = {"page_size": 1}
    backoff = 2.0
    for attempt in range(5):
        try:
            r = session.get(url, params=params, timeout=30)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            if r.status_code == 404:
                return True, ""  # software senza log noti
            r.raise_for_status()
            data = (r.json() or {}).get("data") or []
            return True, (data[0].get("message", "") if data else "")
        except (requests.RequestException, ValueError) as e:
            if attempt == 4:
                print(f"  ! {software_id}: {e}", file=sys.stderr)
                return False, ""
            time.sleep(backoff ** attempt)
    return False, ""


def iter_ids(jsonl_path: Path):
    """Itera gli id dei software dal catalogo (data/software.jsonl)."""
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            sid = rec.get("id")
            if sid:
                pc = rec.get("publiccode") or {}
                yield sid, pc.get("name", "")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Stato publiccode.yml dal catalogo Developers Italia")
    p.add_argument("--input", default="data/software.jsonl", help="catalogo JSONL di partenza")
    p.add_argument("--out", default="data/publiccode.jsonl", help="file JSONL di output")
    p.add_argument("--delay", type=float, default=1.0, help="secondi tra le richieste (default 1.0)")
    p.add_argument("--limit", type=int, help="processa solo i primi N software")
    args = p.parse_args(argv)

    targets = list(iter_ids(Path(args.input)))
    if args.limit:
        targets = targets[: args.limit]

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Target: {len(targets)} software")

    n_valid = n_problem = n_empty = n_fail = 0
    with out_path.open("w", encoding="utf-8") as jf:
        for i, (sid, name) in enumerate(targets, 1):
            fetched, message = fetch_latest_log(session, sid)
            if not fetched:
                n_fail += 1
            elif not message:
                n_empty += 1
            elif "GOOD publiccode.yml" in message:
                n_valid += 1
            else:
                n_problem += 1
            jf.write(json.dumps({"id": sid, "fetched": fetched, "message": message}, ensure_ascii=False) + "\n")
            jf.flush()
            if i % 50 == 0 or i == len(targets):
                print(f"  {i}/{len(targets)} (valido={n_valid} problema={n_problem} senza-log={n_empty} errore-fetch={n_fail})")
            time.sleep(args.delay)

    print(f"\nFatto. {out_path}")
    print(f"  valido={n_valid}  con problema={n_problem}  senza log={n_empty}  fetch fallito={n_fail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
