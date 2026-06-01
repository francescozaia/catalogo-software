"""Recupera i vocabolari ufficiali del publiccode.yml per la vista «Infografiche».

Per misurare la copertura del catalogo (quali valori previsti dallo standard
non sono mai dichiarati nei publiccode dei software), serve la lista chiusa di:

  - categorie (`categories`)
  - audience scope (`intendedAudience.scope`)
  - piattaforme (`platforms`)

Categorie e audience scope sono definite come elenchi di validazione nel
parser ufficiale di Developers Italia (`italia/publiccode-parser-go`). Lo
script li recupera al volo, ne estrae solo le chiavi (identificatori brevi
del vocabolario) e scrive `data/taxonomies.json`.

Le piattaforme sono un set chiuso piccolo e ben noto del publiccode standard;
restano hard-coded qui.

Uso:
    python3 taxonomies.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import requests

VALIDATOR_URL = (
    "https://raw.githubusercontent.com/italia/publiccode-parser-go/main/validators/v0.go"
)
USER_AGENT = "catalogo-software-crawler/1.0 (+https://developers.italia.it)"

# Valori CANONICI del campo `platforms` secondo il publiccode.yml standard:
# https://yml.publiccode.tools/schema.core.html#key-platforms
# Nota: il set è semi-aperto — lo standard ammette anche valori "human readable"
# fuori da questa lista, quindi la copertura va letta come adesione ai canonici,
# non come copertura di un vocabolario chiuso.
PLATFORMS = sorted(["web", "windows", "mac", "linux", "ios", "android"])


def extract_keys(source: str, var_name: str) -> list[str]:
    """Estrae le chiavi di un `map[string]struct{}{}` Go a partire dal nome var."""
    m = re.search(
        rf"var\s+{var_name}\s*=\s*map\[string\]struct\{{\}}\{{(.*?)\n\}}",
        source,
        re.S,
    )
    if not m:
        return []
    return re.findall(r'"([^"]+)"\s*:\s*\{\s*\}', m.group(1))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="data/taxonomies.json")
    args = p.parse_args(argv)

    print(f"Scarico {VALIDATOR_URL} …")
    r = requests.get(VALIDATOR_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()

    categories = sorted(extract_keys(r.text, "supportedCategoriesV0"))
    scopes = sorted(extract_keys(r.text, "supportedScopesV0"))
    if not categories or not scopes:
        print("ERRORE: impossibile estrarre le liste dal validator", file=sys.stderr)
        return 1

    out = {
        "_source": (
            "italia/publiccode-parser-go validators/v0.go "
            "(supportedCategoriesV0, supportedScopesV0); platforms dal publiccode standard"
        ),
        "categories": categories,
        "audience_scope": scopes,
        "platforms": PLATFORMS,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"Scritto {out_path}: {len(categories)} categorie, {len(scopes)} audience scope, "
        f"{len(PLATFORMS)} piattaforme"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
