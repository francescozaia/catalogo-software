"""Genera docs/index.html: pannello interattivo standalone.

Unisce data/software.jsonl (metadati catalogo) + data/metrics.jsonl (metriche
repository, tutti i provider) in un singolo file HTML con dati embedded, filtri,
ordinamento, score composito modificabile, e dettaglio per repository.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from html import escape
from pathlib import Path
from statistics import median

OUT = Path("docs/index.html")
METRICS = Path("data/metrics.jsonl")  # output unico di metrics.py (tutti i provider)
CATALOG = Path("data/software.jsonl")
PUBLICCODE = Path("data/publiccode.jsonl")  # output di publiccode_status.py (opzionale)
INDICEPA = Path("data/indicepa.jsonl")  # output di indicepa.py — contatti enti (opzionale)
TAXONOMIES = Path("data/taxonomies.json")  # output di taxonomies.py — vocabolari publiccode (opzionale)

# Markup statico del pannello (HTML/CSS/JS) con i segnaposto __DATA__ e __INFO__.
# Tenuto in un file separato per manutenibilità (syntax highlighting, lint, ecc.);
# risolto relativamente a questo script così funziona da qualunque working dir.
TEMPLATE_PATH = Path(__file__).resolve().parent / "template.html"


def load_catalog() -> dict[str, dict]:
    """Carica data/software.jsonl indicizzato per id catalogo Developers Italia.

    Si indicizza per `id` (UUID della scheda) e non per URL del repo perché più
    schede catalogo possono dichiarare lo stesso repo upstream (es. Matomo è
    pubblicato sia da italia-software sia da Regione ER, ed entrambe puntano a
    `matomo-org/matomo`). Indicizzando per URL le due schede collidono e una
    "scompare" dietro l'altra. La vista resta guidata dalle schede catalogo:
    una riga in pannello per ogni scheda, anche se due schede condividono il
    repo upstream (in quel caso le metriche saranno identiche).
    """
    idx: dict[str, dict] = {}
    with CATALOG.open(encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            cid = r.get("id")
            if cid:
                idx[cid] = r
    return idx


def load_publiccode() -> dict[str, dict]:
    """Carica data/publiccode.jsonl indicizzato per id software (vuoto se assente)."""
    idx: dict[str, dict] = {}
    if not PUBLICCODE.exists():
        return idx
    for line in PUBLICCODE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("id"):
            idx[r["id"]] = r
    return idx


def load_taxonomies() -> dict[str, list[str]]:
    """Carica taxonomies.json — vocabolari ufficiali del publiccode.yml spec.

    Usati per misurare la copertura del catalogo (valori dichiarati vs. valori
    previsti dallo standard). Le chiavi che iniziano con `_` sono metadata.
    """
    if not TAXONOMIES.exists():
        return {}
    raw = json.loads(TAXONOMIES.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def load_indicepa() -> dict[str, dict]:
    """Carica data/indicepa.jsonl indicizzato per codice IPA minuscolo (vuoto se assente)."""
    idx: dict[str, dict] = {}
    if not INDICEPA.exists():
        return idx
    for line in INDICEPA.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("codice_ipa"):
            idx[str(r["codice_ipa"]).lower()] = r
    return idx


def pc_severity(message: str) -> str:
    """Severità della validazione publiccode.yml: valid | warning | error.

    Derivata dal messaggio dell'ultimo log del crawler. Gli avvisi (es.
    versione publiccode.yml non più recente) sono distinti dagli errori veri.
    """
    if "GOOD publiccode.yml" in message:
        return "valid"
    ml = message.lower()
    only_warning = (": warning:" in ml or "is not the latest version" in ml) and ": error:" not in ml
    return "warning" if only_warning else "error"


def pc_category(message: str) -> str:
    """Etichetta della tipologia di problema (per raggruppamento/filtro).

    L'avviso "versione non più recente" è quasi sempre presente accanto a un
    errore vero, quindi viene considerato per ultimo: la categoria riflette
    l'errore di validazione reale, non l'avviso di formato.
    """
    ml = message.lower()
    if "publiccode.yml" in ml and "not found" in ml:
        return "publiccode.yml non trovato"
    if any(s in ml for s in ("could not clone", "repository not found", "fatal:", "remote:")):
        return "Repository irraggiungibile"
    if "organisation is" in ml and "was expected" in ml:
        return "Codice organisation/IPA"
    # Primo errore di validazione del parser: riga "... error: <campo>: ..."
    for line in message.splitlines():
        low = line.lower()
        if ": error:" not in low:
            continue
        seg = low.split(": error:", 1)[1].strip()
        field = re.split(r"[\[:\s]", seg, 1)[0]
        if field.startswith("localisation"):
            return "Localizzazione"
        if field.startswith("maintenance"):
            return "Manutenzione / contatti"
        if field.startswith("description"):
            return "Descrizione"
        if field.startswith(("legal", "license")) or "license" in seg:
            return "Licenza"
        if field.startswith(("url", "landingurl", "isbasedon", "logo", "monochromelogo")):
            return "URL / asset non valido"
        if "required" in seg:
            return "Campo obbligatorio mancante"
        if any(s in low for s in ("mapping", "unmarshal", "yaml")):
            return "Errore di sintassi YAML"
        return "Altro errore di validazione"
    if "is not the latest version" in ml or "publiccodeymlversion" in ml:
        return "Versione publiccode.yml non aggiornata"
    return "Altro"


def pc_clean_message(message: str) -> str:
    """Estrae il motivo del problema dal log, ripulito dal boilerplate."""
    if not message:
        return ""
    idx = message.find("BAD publiccode.yml:")
    if idx != -1:
        text = message[idx + len("BAD publiccode.yml:"):]
    else:
        text = "\n".join(l for l in message.splitlines() if l.strip() and "found at" not in l)
    return " ".join(text.split()).strip()


def classify_reuse(pc: dict) -> tuple[bool, str, str]:
    """Distingue software a riuso da open, replicando la logica ufficiale.

    Riferimento: italia/developers.italia.it searchEngine.js — è "a riuso" se è
    valorizzato uno qualsiasi tra organisation.uri, IT.riuso.codiceIPA o
    it.riuso.codiceIPA (senza richiedere il prefisso urn:x-italian-pa:).

    Restituisce (is_riuso, organisation_uri, codice_ipa). Il codice IPA mostrato
    è il codiceIPA esplicito se presente, altrimenti l'URN organisation senza
    prefisso; vuoto quando organisation.uri è un URL e non c'è un codiceIPA.
    """
    org = pc.get("organisation") if isinstance(pc.get("organisation"), dict) else {}
    org_uri = org.get("uri")
    it_up = pc.get("IT") if isinstance(pc.get("IT"), dict) else {}
    it_low = pc.get("it") if isinstance(pc.get("it"), dict) else {}
    riuso_up = it_up.get("riuso") if isinstance(it_up.get("riuso"), dict) else {}
    riuso_low = it_low.get("riuso") if isinstance(it_low.get("riuso"), dict) else {}
    riuso_code = riuso_up.get("codiceIPA") or riuso_low.get("codiceIPA")

    is_riuso = bool(org_uri or riuso_code)
    if riuso_code:
        codice_ipa = str(riuso_code)
    elif isinstance(org_uri, str) and org_uri.startswith("urn:x-italian-pa:"):
        codice_ipa = org_uri[len("urn:x-italian-pa:"):]
    else:
        codice_ipa = ""
    return is_riuso, (org_uri or ""), codice_ipa


def pick_localised(desc: dict, key: str) -> str:
    """Testo localizzato dalla description publiccode (it-IT preferito)."""
    if not isinstance(desc, dict):
        return ""
    for lang in ("it-IT", "it", "en", "en-US"):
        blk = desc.get(lang)
        if isinstance(blk, dict) and blk.get(key):
            return str(blk[key])
    for blk in desc.values():
        if isinstance(blk, dict) and blk.get(key):
            return str(blk[key])
    return ""


def _people(lst, keys: list[str]) -> list[dict]:
    """Normalizza una lista di persone/contraenti tenendo solo i campi presenti."""
    out = []
    for x in (lst if isinstance(lst, list) else []):
        if isinstance(x, dict):
            out.append({k: x.get(k) for k in keys if x.get(k)})
    return out


def enrich(metrics: list[dict], catalog: dict[str, dict], publiccode: dict[str, dict]) -> list[dict]:
    """Combina metriche e catalogo. Si itera sulle schede del catalogo (chiave:
    id catalogo) e si recupera la metrica per URL del repo, perché due schede
    possono condividere lo stesso repo upstream e iterando sulle metriche se ne
    perderebbe una (vedi load_catalog). Le metriche sono indicizzate per URL e
    quando due schede puntano allo stesso repo otterranno la stessa metrica.
    """
    metrics_by_url: dict[str, dict] = {}
    for m in metrics:
        u = m.get("url")
        if u and u not in metrics_by_url:
            metrics_by_url[u] = m

    out = []
    for cid, cat in catalog.items():
        pc = cat.get("publiccode") or {}
        url = pc.get("url") or cat.get("url") or ""
        if not url:
            continue
        m = metrics_by_url.get(url) or {"url": url, "error": "metrica non disponibile"}
        is_riuso, org_uri, codice_ipa = classify_reuse(pc)
        org = pc.get("organisation") if isinstance(pc.get("organisation"), dict) else {}
        maint = pc.get("maintenance") if isinstance(pc.get("maintenance"), dict) else {}
        ia = pc.get("intendedAudience") if isinstance(pc.get("intendedAudience"), dict) else {}
        used_by = pc.get("usedBy") if isinstance(pc.get("usedBy"), list) else []
        desc = pc.get("description") or {}
        m2 = dict(m)
        m2["catalog_id"] = cid
        m2["catalog_name"] = pc.get("name", "")
        m2["development_status"] = pc.get("developmentStatus", "")
        m2["release_date"] = pc.get("releaseDate", "")
        m2["organisation_uri"] = org_uri or ""
        m2["organisation_name"] = org.get("name") or ""
        m2["is_riuso"] = is_riuso
        m2["codice_ipa"] = codice_ipa
        m2["categories"] = pc.get("categories") or []
        m2["platforms"] = pc.get("platforms") or []
        m2["software_type"] = pc.get("softwareType") or ""
        m2["publiccode_version"] = str(pc.get("publiccodeYmlVersion") or "")
        m2["audience_scope"] = ia.get("scope") or []
        m2["maintenance_type"] = maint.get("type") or ""
        m2["contractors"] = _people(maint.get("contractors"), ["name", "until", "website", "email"])
        m2["contacts"] = _people(maint.get("contacts"), ["name", "email", "phone", "affiliation"])
        m2["used_by_count"] = len(used_by)
        m2["used_by"] = [str(x) for x in used_by[:1000]]  # cap per contenere il payload
        m2["short_description"] = pick_localised(desc, "shortDescription")
        m2["long_description"] = pick_localised(desc, "longDescription")

        # --- Stato di validazione publiccode.yml (da data/publiccode.jsonl) ---
        pcrec = publiccode.get(cid)
        msg = (pcrec or {}).get("message") or ""
        if pcrec is None or not pcrec.get("fetched", True) or not msg:
            m2["publiccode_severity"] = "unknown"
            m2["publiccode_category"] = ""
            m2["publiccode_message"] = ""
        else:
            sev = pc_severity(msg)
            m2["publiccode_severity"] = sev
            m2["publiccode_category"] = "" if sev == "valid" else pc_category(msg)
            m2["publiccode_message"] = "" if sev == "valid" else pc_clean_message(msg)
        out.append(m2)
    return out


def compute_findings(data: list[dict]) -> dict:
    ok = [r for r in data if not r.get("error")]
    n = len(ok)
    days = [r["days_since_last_commit"] for r in ok if isinstance(r.get("days_since_last_commit"), (int, float))]

    f: dict = {}
    f["total"] = len(data)
    f["analyzed"] = n
    f["errors"] = len(data) - n
    f["riuso"] = sum(1 for r in ok if r.get("is_riuso"))
    f["os"] = n - f["riuso"]
    f["archived"] = sum(1 for r in ok if r.get("archived"))

    f["median_stale_days"] = int(median(days)) if days else 0
    f["bucket_recent_30"] = sum(1 for d in days if d <= 30)
    f["bucket_90"] = sum(1 for d in days if d <= 90)
    f["bucket_over_1y"] = sum(1 for d in days if d > 365)
    f["bucket_over_2y"] = sum(1 for d in days if d > 730)

    silent = sum(
        1 for r in ok
        if isinstance(r.get("days_since_last_commit"), (int, float))
        and r["days_since_last_commit"] > 365
        and not r.get("archived")
    )
    f["silent_abandoned"] = silent
    f["silent_pct"] = round(100 * silent / n, 1) if n else 0

    has_ci = sum(1 for r in ok if r.get("has_ci"))
    f["has_ci"] = has_ci
    f["has_ci_pct"] = round(100 * has_ci / n, 1) if n else 0

    dep = sum(1 for r in ok if (r.get("dependabot_pr_in_window") or 0) > 0)
    f["has_dependabot"] = dep
    f["has_dependabot_pct"] = round(100 * dep / n, 1) if n else 0

    active = [r for r in ok if (r.get("commits_in_window") or 0) > 0]
    f["active_count"] = len(active)
    direct = sum(1 for r in active if (r.get("pr_merged_in_window") or 0) == 0)
    f["direct_push"] = direct
    f["direct_push_pct_of_active"] = round(100 * direct / len(active), 1) if active else 0

    prs = [r for r in ok if (r.get("pr_merged_in_window") or 0) > 0]
    f["repos_with_pr"] = len(prs)
    lead = [r["pr_median_lead_time_days"] for r in prs if isinstance(r.get("pr_median_lead_time_days"), (int, float))]
    f["overall_lead_median"] = round(median(lead), 1) if lead else 0
    rev = [r["pr_review_coverage_pct"] for r in prs if isinstance(r.get("pr_review_coverage_pct"), (int, float))]
    f["overall_review_median"] = round(median(rev), 1) if rev else 0
    no_review = sum(1 for r in prs if (r.get("pr_review_coverage_pct") or 0) == 0)
    f["repos_with_pr_no_review"] = no_review
    f["repos_with_pr_no_review_pct"] = round(100 * no_review / len(prs), 1) if prs else 0

    bus_risk = sum(
        1 for r in ok
        if (r.get("bus_factor_top1_pct") or 0) >= 90
        and (r.get("commit_authors_in_window") or 0) > 0
    )
    f["bus_risk"] = bus_risk

    top_active = sorted(ok, key=lambda r: r.get("commits_in_window") or 0, reverse=True)[:10]
    f["top_active"] = [
        (r.get("catalog_name") or r.get("name_with_owner") or "", r.get("name_with_owner") or "",
         r.get("commits_in_window") or 0, r.get("url") or "")
        for r in top_active
    ]

    zombies = [
        r for r in ok
        if r.get("development_status") == "stable"
        and (r.get("days_since_last_commit") or 0) > 730
        and not r.get("archived")
    ]
    zombies.sort(key=lambda r: r.get("days_since_last_commit") or 0, reverse=True)
    f["zombies_count"] = len(zombies)
    f["zombies_top10"] = [
        (r.get("catalog_name") or r.get("name_with_owner") or "", r.get("name_with_owner") or "",
         r.get("days_since_last_commit") or 0, r.get("url") or "")
        for r in zombies[:10]
    ]

    lang_c: Counter = Counter()
    lic_c: Counter = Counter()
    for r in ok:
        if r.get("primary_language"):
            lang_c[r["primary_language"]] += 1
        if r.get("license"):
            lic_c[r["license"]] += 1
    f["top_langs"] = lang_c.most_common(10)
    f["top_licenses"] = lic_c.most_common(8)

    prov_c: Counter = Counter()
    prov_err: Counter = Counter()
    for r in data:
        p = r.get("provider") or "github"
        if r.get("error"):
            prov_err[p] += 1
        else:
            prov_c[p] += 1
    f["by_provider"] = sorted(prov_c.items(), key=lambda x: -x[1])
    f["by_provider_err"] = dict(prov_err)

    pc_sev: Counter = Counter(r.get("publiccode_severity", "unknown") for r in data)
    f["pc_valid"] = pc_sev.get("valid", 0)
    f["pc_warning"] = pc_sev.get("warning", 0)
    f["pc_error"] = pc_sev.get("error", 0)
    f["pc_unknown"] = pc_sev.get("unknown", 0)
    pc_cat: Counter = Counter(r["publiccode_category"] for r in data if r.get("publiccode_category"))
    f["pc_top_categories"] = pc_cat.most_common(8)

    return f


def render_info_html(f: dict) -> str:
    def kpi(label: str, value: str, note: str = "") -> str:
        note_html = f'<div class="kpi-n">{escape(note)}</div>' if note else ""
        return (
            f'<div class="kpi"><div class="kpi-v">{escape(str(value))}</div>'
            f'<div class="kpi-l">{escape(label)}</div>{note_html}</div>'
        )

    def repo_list(items: list[tuple]) -> str:
        lis = []
        for cat_name, nwo, val, url in items:
            lis.append(
                f'<li><a href="{escape(url)}" target="_blank" rel="noopener">{escape(cat_name or nwo)}</a> '
                f'<span class="ml">{escape(nwo)}</span> <b>{val}</b></li>'
            )
        return "<ol class='ranklist'>" + "".join(lis) + "</ol>"

    def bar_row(label: str, value: int, total: int) -> str:
        pct = round(100 * value / total, 1) if total else 0
        return (
            f'<div class="bar-row"><span class="bar-l">{escape(label)}</span>'
            f'<span class="bar-bg"><i style="width:{pct}%"></i></span>'
            f'<span class="bar-v">{value} <em>({pct}%)</em></span></div>'
        )

    kpis = "".join([
        kpi("Repository analizzati", f"{f['analyzed']}", f"+ {f['errors']} non recuperabili (privati/cancellati)"),
        kpi("Inattivi >1 anno", f"{f['bucket_over_1y']}", f"{round(100*f['bucket_over_1y']/f['analyzed'],1)}% del campione"),
        kpi("Inattivi >1 anno e non archiviati", f"{f['silent_abandoned']}", f"{f['silent_pct']}% del campione · {f['archived']} archiviati"),
        kpi("Mediana giorni dall'ultimo commit", f"{f['median_stale_days']}", "metà dei repository ha un valore superiore"),
        kpi("Repo con CI configurato", f"{f['has_ci']}", f"{f['has_ci_pct']}% del campione"),
        kpi("Repo con PR Dependabot/Renovate", f"{f['has_dependabot']}", f"{f['has_dependabot_pct']}% del campione"),
        kpi("Repo con ≥1 PR mergeata in 90gg", f"{f['repos_with_pr']}", "campione per lead time e review"),
        kpi("…di cui senza review registrata", f"{f['repos_with_pr_no_review']}", f"{f['repos_with_pr_no_review_pct']}% del sottoinsieme"),
    ])

    activity_bars = "".join([
        bar_row("Push ≤ 30 giorni", f["bucket_recent_30"], f["analyzed"]),
        bar_row("Push ≤ 90 giorni", f["bucket_90"], f["analyzed"]),
        bar_row("Inattivi > 1 anno", f["bucket_over_1y"], f["analyzed"]),
        bar_row("Inattivi > 2 anni", f["bucket_over_2y"], f["analyzed"]),
    ])

    lang_bars = "".join([bar_row(lang, n, f["analyzed"]) for lang, n in f["top_langs"]])
    lic_bars = "".join([bar_row(lic, n, f["analyzed"]) for lic, n in f["top_licenses"]])
    provider_bars = "".join([bar_row(p, n, f["analyzed"]) for p, n in f["by_provider"]])
    prov_err_html = ", ".join(f"{p}: {n}" for p, n in f["by_provider_err"].items()) or "0"
    pc_problems = f["pc_warning"] + f["pc_error"]
    pc_cat_bars = "".join(bar_row(lbl, n, pc_problems) for lbl, n in f["pc_top_categories"]) or \
        '<div class="bar-row"><span class="bar-l">—</span></div>'

    return f"""
<section id="view-info" hidden>
  <div class="info-wrap">
    <h2>Metodologia & findings principali</h2>
    <p class="lead">
      Catalogo di {f['total']} software; metriche di salute calcolate per
      {f['analyzed']} repository pubblici (GitHub, GitLab, Bitbucket). Le metriche
      descrivono coinvolgimento, velocità e qualità del processo di sviluppo —
      ciclo delle pull request, copertura delle review, frequenza dei commit,
      gestione delle issue, freschezza di codice e dipendenze — senza considerare
      metriche di facciata come le stelle. Dati estratti dal catalogo pubblico di
      Developers Italia.
    </p>

    <h3>1. Numeri chiave</h3>
    <div class="kpis">{kpis}</div>

    <h3>2. Findings</h3>

    <h4>Recency dell'attività</h4>
    <p>
      Distribuzione dei repository per data dell'ultimo commit sul branch di
      default. <b>{f['bucket_over_1y']}</b> repository non ricevono commit da
      oltre un anno; di questi <b>{f['archived']}</b> risultano dichiarati
      <code>archived</code> sul provider.
    </p>
    <div class="bars">{activity_bars}</div>

    <h4>Pull request e review</h4>
    <p>
      <b>{f['repos_with_pr']}</b> repository hanno mergeato almeno una PR negli
      ultimi 90 giorni; tra questi, <b>{f['repos_with_pr_no_review']}
      ({f['repos_with_pr_no_review_pct']}%)</b> non registrano review sulle PR
      mergeate. Tra i <b>{f['active_count']}</b> repository con commit recenti,
      <b>{f['direct_push']} ({f['direct_push_pct_of_active']}%)</b> registrano
      commit nella finestra senza passare da pull request.
    </p>
    <p>
      Lead time mediano apertura→merge (tra i repo con PR):
      <b>{f['overall_lead_median']} giorni</b>. Copertura review mediana:
      <b>{f['overall_review_median']}%</b>.
    </p>

    <h4>Integrazione continua e aggiornamento dipendenze</h4>
    <p>
      <b>{f['has_ci']} ({f['has_ci_pct']}%)</b> repository hanno una configurazione
      CI (workflow in <code>.github/workflows</code> o file equivalenti nella root).
      <b>{f['has_dependabot']} ({f['has_dependabot_pct']}%)</b> hanno ricevuto, nella
      finestra, PR automatiche di aggiornamento delle dipendenze (Dependabot/Renovate).
    </p>

    <h4>Validazione del publiccode.yml</h4>
    <p>
      Esito dell'ultima validazione del <code>publiccode.yml</code> registrata dal
      crawler di Developers Italia: <b>{f['pc_valid']}</b> validi,
      <b>{f['pc_warning']}</b> con avvisi, <b>{f['pc_error']}</b> con errori,
      <b>{f['pc_unknown']}</b> senza esito disponibile. Gli avvisi (es. versione del
      formato non più recente) sono distinti dagli errori di validazione veri e propri.
    </p>
    <p>Tipologie di problema più diffuse:</p>
    <div class="bars">{pc_cat_bars}</div>

    <h4>Concentrazione dei contributi</h4>
    <p>
      In <b>{f['bus_risk']}</b> repository oltre il 90% dei commit recenti proviene
      da un singolo autore.
    </p>

    <h4>Repository dichiarati «stable» senza commit da oltre 2 anni</h4>
    <p>
      <b>{f['zombies_count']} repository</b> hanno
      <code>developmentStatus: stable</code> nel <code>publiccode.yml</code> ma non
      ricevono commit da oltre due anni e non risultano archiviati. Primi 10 per
      inattività:
    </p>
    {repo_list(f['zombies_top10'])}

    <h4>Repository più attivi (commit sul branch di default, ultimi 90 giorni)</h4>
    {repo_list(f['top_active'])}

    <h3>3. Linguaggi e licenze</h3>
    <div class="cols">
      <div><h4>Linguaggio principale</h4><div class="bars">{lang_bars}</div></div>
      <div><h4>Licenza</h4><div class="bars">{lic_bars}</div></div>
    </div>

    <h3>4. Provider</h3>
    <p>
      Il catalogo non è ospitato solo su GitHub. Sono coperti anche
      GitLab (gitlab.com e istanze self-hosted di amministrazioni come
      Regione Puglia, KDE, ecc.) e Bitbucket Cloud. La stessa metrica
      è calcolata per ogni provider con le sue API native.
    </p>
    <div class="bars">{provider_bars}</div>
    <p style="color:var(--muted); font-size:12px;">
      Repository non recuperabili (privati, cancellati o URL non
      interpretabile): {prov_err_html}.
    </p>

    <h3>5. Metodologia</h3>

    <h4>Pipeline dati</h4>
    <ol>
      <li><b>Catalogo</b>: <code>crawler.py</code> interroga l'API ufficiale
      <code>api.developers.italia.it/v1/software</code> con paginazione cursor-based
      (25 record/pagina, ~1 req/s). Per ogni record viene parsato il
      <code>publiccode.yml</code>.</li>
      <li><b>Metriche</b>: <code>metrics.py</code> processa i repository GitHub
      con una singola query GraphQL per repo (tramite token <code>gh</code>),
      catturando: cronologia commit del default branch, ultime 100 PR e issue,
      release, struttura root e <code>.github/workflows</code>.</li>
      <li><b>Dashboard</b>: <code>build_dashboard.py</code> arricchisce le
      metriche con i metadati del catalogo (IPA, ente, descrizione, status) e
      genera questo HTML standalone con dati embedded.</li>
    </ol>

    <h4>Finestra temporale</h4>
    <p>
      Salvo dove diversamente indicato (es. <code>last_commit_date</code>,
      <code>oldest_open_issue_days</code> che sono assoluti), tutte le metriche
      di attività si riferiscono alla finestra di <b>90 giorni</b> antecedenti
      l'esecuzione del crawl. La scelta riflette un orizzonte rilevante per il
      software di produzione: né troppo breve da catturare il rumore, né troppo
      lungo da nascondere abbandoni recenti.
    </p>

    <h4>Classificazione «a riuso» vs «open source»</h4>
    <p>
      Un software è <b>a riuso</b> se nel suo <code>publiccode.yml</code> è
      valorizzato almeno uno tra <code>organisation.uri</code>,
      <code>IT.riuso.codiceIPA</code> o <code>it.riuso.codiceIPA</code>; altrimenti
      è <b>open source</b>. È la stessa regola del motore di ricerca ufficiale.
      Su questo dataset: <b>{f['riuso']}</b> a riuso, <b>{f['os']}</b> open source.
    </p>
    <p>
      Questo conteggio può differire di qualche unità da quello mostrato su
      developers.italia.it, e <b>non è un errore</b>. Qui si classifica in base al
      <b>publiccode.yml così come dichiarato</b> e servito dall'API ufficiale
      <code>api.developers.italia.it</code>; l'indice di ricerca del sito è invece
      <b>arricchito in fase di indicizzazione</b>. Per i software pubblicati da una
      PA, quell'indice inietta il codice IPA dell'editore in
      <code>organisation.uri</code> / <code>IT.riuso.codiceIPA</code> anche quando
      l'autore non l'ha scritto nel file (spostandoli così tra gli «a riuso»);
      viceversa scarta valori di <code>organisation</code> non-PA (es. l'URL di
      un'azienda privata). Si è scelto di restare fedeli a ciò che è
      effettivamente dichiarato nei file, anche per rendere visibili i metadati da
      correggere.
    </p>

    <h4>Score composito (0–100, modificabile dal pannello)</h4>
    <p>Quattro sub-score 0–100, combinati come media pesata. I pesi di default sono 25/25/25/25; l'utente può modificarli con gli slider per far emergere repo diversi a seconda dell'angolo di analisi.</p>
    <ul>
      <li><b>Freshness</b>: giorni dall'ultimo commit (soglia a 14/30/90/180/365 gg). <code>archived</code> = 0.</li>
      <li><b>Velocity</b>: 60% volume di commit in finestra + 40% lead time mediano delle PR mergeate. Premia chi spedisce frequentemente <i>e</i> rapidamente.</li>
      <li><b>Quality</b>: 40% review coverage + 30% presenza di CI + 30% bus factor (penalizza la concentrazione su singolo autore).</li>
      <li><b>Maintenance</b>: 25% età della issue aperta più vecchia + 25% tasso chiusura issue + 25% freschezza delle dipendenze (Dependabot/file di lock presenti) + 25% recency dell'ultima release.</li>
    </ul>

    <h4>Limiti noti</h4>
    <ul>
      <li><b>Multi-provider</b>: GitHub, GitLab (gitlab.com + self-hosted) e Bitbucket sono supportati. Su Bitbucket alcune metriche (release, tasso chiusura issue, license) non sono esposte dall'API e vengono lasciate vuote.</li>
      <li><b>Review coverage GitLab</b>: usa <code>user_notes_count &gt; 0</code> come proxy (commenti sulla MR). Senza autenticazione l'API delle <code>approvals</code> non è disponibile.</li>
      <li><b>Troncamento a 100</b>: la query GraphQL/REST prende le 100 PR/issue più recenti. Per progetti molto attivi (improbabili in PA) il conteggio in finestra è un lower bound.</li>
      <li><b>Bus factor</b>: calcolato sui soli autori della finestra di 90 giorni, non sull'intera storia del repo.</li>
      <li><b>Repo non recuperabili</b>: il catalogo include URL che restituiscono 404 (repo cancellati o rinominati) o 403 (repo resi privati). Restano comunque <b>visibili in tabella</b>, marcati «non recuperabile» con il motivo, perché conservano i dati di catalogo (classificazione riuso/open, stato publiccode.yml); non hanno però metriche di attività.</li>
    </ul>

    <h4>Riproducibilità</h4>
    <p>Per rigenerare tutto: <code>python3 crawler.py &amp;&amp; python3 metrics.py &amp;&amp; python3 build_dashboard.py</code>. Lo script <code>metrics.py</code> richiede <code>gh auth login</code> già fatto.</p>
  </div>
</section>
"""




def main() -> None:
    if not METRICS.exists():
        raise SystemExit("manca data/metrics.jsonl — esegui prima metrics.py")
    metrics = [json.loads(l) for l in METRICS.read_text(encoding="utf-8").splitlines() if l.strip()]
    for m in metrics:
        m.setdefault("provider", "github")  # retrocompat. con eventuali record vecchi
    catalog = load_catalog()
    publiccode = load_publiccode()
    if not publiccode:
        print("  (data/publiccode.jsonl assente: stato publiccode.yml = sconosciuto)")
    indicepa = load_indicepa()
    if not indicepa:
        print("  (data/indicepa.jsonl assente: contatti enti non disponibili)")
    taxonomies = load_taxonomies()
    enriched = enrich(metrics, catalog, publiccode)
    payload = json.dumps(enriched, ensure_ascii=False, default=str)
    indicepa_json = json.dumps(indicepa, ensure_ascii=False)
    # Coverage: per ciascun campo a vocabolario chiuso calcoliamo, contro la
    # tassonomia ufficiale, l'elenco dei valori MAI dichiarati nel catalogo
    # (insieme alla copertura totale e al link alla documentazione). L'elenco
    # è il risultato del confronto fra i dati nostri e lo standard, derivato
    # a build-time così la pagina lo può mostrare in un dettaglio espandibile.
    def _unused(field: str, taxonomy_key: str) -> list[str]:
        std = set(taxonomies.get(taxonomy_key, []))
        if not std:
            return []
        used = set()
        for r in enriched:
            for v in (r.get(field) or []):
                if v:
                    used.add(v)
        return sorted(std - used)

    coverage = {
        "categories": {
            "total": len(taxonomies.get("categories", [])),
            "unused": _unused("categories", "categories"),
            "spec": "https://yml.publiccode.tools/",
        },
        "audience_scope": {
            "total": len(taxonomies.get("audience_scope", [])),
            "unused": _unused("audience_scope", "audience_scope"),
            "spec": "https://yml.publiccode.tools/",
        },
        "platforms": {
            "total": len(taxonomies.get("platforms", [])),
            "unused": _unused("platforms", "platforms"),
            "spec": "https://yml.publiccode.tools/schema.core.html#key-platforms",
            "open": True,  # set semi-aperto: lo standard ammette anche valori fuori dalla lista
        },
    }
    coverage_json = json.dumps(coverage, ensure_ascii=False)
    findings = compute_findings(enriched)
    info_html = render_info_html(findings)
    # data dell'ultimo scaricamento delle metriche (mtime di data/metrics.jsonl)
    updated = datetime.fromtimestamp(METRICS.stat().st_mtime).strftime("%d/%m/%Y")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    # il payload (blob JSON, grande) viene sostituito per ultimo
    html = (template
            .replace("__UPDATED__", updated)
            .replace("__INDICEPA__", indicepa_json)
            .replace("__COVERAGE__", coverage_json)
            .replace("__INFO__", info_html)
            .replace("__DATA__", payload))
    OUT.write_text(html, encoding="utf-8")
    print(f"Generato {OUT} ({OUT.stat().st_size // 1024} KB) con {len(enriched)} repository")


if __name__ == "__main__":
    main()
