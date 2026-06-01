# Analisi catalogo del software open source

_Disclaimer: quanto presente è un progetto personale basato su dati pubblici_

Pipeline per analizzare lo stato di salute del software open source e a riuso
della Pubblica Amministrazione italiana, a partire dal
[catalogo Developers Italia](https://developers.italia.it/it/search).

Per ogni software del catalogo vengono raccolte metriche di **coinvolgimento,
velocità e qualità** del processo di sviluppo (lead time delle PR, copertura
delle review, frequenza dei commit, gestione delle issue, freschezza di codice
e dipendenze) — deliberatamente escludendo segnali di facciata come le stelle.
I risultati confluiscono in un pannello HTML interattivo.

🔗 **Pannello pubblicato:** https://francescozaia.github.io/catalogo-software/

## Prerequisiti

- Python 3.10+
- `pip install -r requirements.txt`
- [GitHub CLI](https://cli.github.com/) autenticata (`gh auth login`) — usata da
  `metrics.py` per interrogare l'API GitHub. GitLab e Bitbucket sono interrogati
  in forma anonima (solo repository pubblici).

## Pipeline

```
python3 crawler.py           # legge le API di Developers Italia → data/software.{jsonl,csv}   (~1 min)
python3 metrics.py           # interroga i repository (GitHub/GitLab/Bitbucket) → data/metrics.{jsonl,csv}  (~15 min)
python3 publiccode_status.py # stato di validazione del publiccode.yml → data/publiccode.jsonl  (~9 min)
python3 indicepa.py          # contatti ufficiali degli enti (PEC, ecc.) da IndicePA → data/indicepa.jsonl  (~5 s)
python3 taxonomies.py        # totali dei vocabolari publiccode.yml → data/taxonomies.json  (~1 s)
python3 build_dashboard.py   # unisce template.html + data/ → docs/index.html
```

Il pannello è un singolo file HTML autonomo: per vederlo in locale basta aprire
`docs/index.html` nel browser.

## Struttura

```
crawler.py           estrazione del catalogo dall'API ufficiale
metrics.py           metriche di salute dei repository (multi-provider)
publiccode_status.py stato di validazione del publiccode.yml (API log Developers Italia)
indicepa.py          contatti ufficiali degli enti da IndicePA (per la vista "Analisi per ente")
taxonomies.py        totali dei vocabolari publiccode.yml (per la vista "Infografiche")
build_dashboard.py   generazione del pannello
template.html        markup del pannello (HTML/CSS/JS) con segnaposto __DATA__/__INFO__
requirements.txt     dipendenze Python
data/                dati generati dagli script (rigenerabili, ignorati da git)
docs/index.html      pannello pubblicato via GitHub Pages
```

## Pubblicazione (GitHub Pages)

Il sito è servito dalla cartella `/docs` del branch `main`
(Settings → Pages → Source: *Deploy from a branch*, branch `main`, cartella `/docs`).
Solo `docs/index.html` viene pubblicato; la cartella `data/` è rigenerabile e non
è versionata, quindi su un clone pulito va rieseguita la pipeline prima di poter
ricostruire il pannello.

## Note

- `metrics.py` accetta opzioni utili: `--sample N`, `--limit N`,
  `--providers github,gitlab`, `--window-days N`. Vedi `python3 metrics.py --help`.
- `crawler.py` riprende da un checkpoint con `--resume` se interrotto.
