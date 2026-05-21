"""Genera docs/index.html: pannello interattivo standalone.

Unisce data/software.jsonl (metadati catalogo) + data/metrics.jsonl (metriche
repository, tutti i provider) in un singolo file HTML con dati embedded, filtri,
ordinamento, score composito modificabile, e dettaglio per repository.
"""

from __future__ import annotations

import json
from collections import Counter
from html import escape
from pathlib import Path
from statistics import median

OUT = Path("docs/index.html")
METRICS = Path("data/metrics.jsonl")  # output unico di metrics.py (tutti i provider)
CATALOG = Path("data/software.jsonl")

# Markup statico del pannello (HTML/CSS/JS) con i segnaposto __DATA__ e __INFO__.
# Tenuto in un file separato per manutenibilità (syntax highlighting, lint, ecc.);
# risolto relativamente a questo script così funziona da qualunque working dir.
TEMPLATE_PATH = Path(__file__).resolve().parent / "template.html"


def load_catalog() -> dict[str, dict]:
    idx: dict[str, dict] = {}
    with CATALOG.open(encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            pc = r.get("publiccode") or {}
            url = pc.get("url") or r.get("url") or ""
            if url:
                idx[url] = r
    return idx


def enrich(metrics: list[dict], catalog: dict[str, dict]) -> list[dict]:
    out = []
    for m in metrics:
        url = m.get("url", "")
        cat = catalog.get(url, {})
        pc = cat.get("publiccode") or {}
        org = pc.get("organisation") or {}
        org_uri = org.get("uri") if isinstance(org, dict) else None
        is_riuso = bool(org_uri) and str(org_uri).startswith("urn:x-italian-pa:")
        m2 = dict(m)
        m2["catalog_name"] = pc.get("name", "")
        m2["development_status"] = pc.get("developmentStatus", "")
        m2["release_date"] = pc.get("releaseDate", "")
        m2["organisation_uri"] = org_uri or ""
        m2["is_riuso"] = is_riuso
        m2["codice_ipa"] = (org_uri or "").replace("urn:x-italian-pa:", "") if is_riuso else ""
        m2["categories"] = pc.get("categories") or []
        desc = pc.get("description") or {}
        it = desc.get("it-IT") if isinstance(desc, dict) else None
        m2["short_description"] = (it or {}).get("shortDescription", "") if isinstance(it, dict) else ""
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
        kpi("Repository GitHub analizzati", f"{f['analyzed']}", f"+ {f['errors']} repo cancellati / non trovati"),
        kpi("Inattivi >1 anno", f"{f['bucket_over_1y']}", f"{round(100*f['bucket_over_1y']/f['analyzed'],1)}% del campione"),
        kpi("Abbandonati di fatto (non archiviati)", f"{f['silent_abandoned']}", f"{f['silent_pct']}% — solo {f['archived']} dichiarati"),
        kpi("Mediana giorni dall'ultimo commit", f"{f['median_stale_days']}", "metà del catalogo sopra questa soglia"),
        kpi("Repo con CI configurato", f"{f['has_ci']}", f"{f['has_ci_pct']}% del campione"),
        kpi("Repo con PR Dependabot attivo", f"{f['has_dependabot']}", f"{f['has_dependabot_pct']}% del campione"),
        kpi("Repo con almeno 1 PR mergeata in 90gg", f"{f['repos_with_pr']}", "campione su cui si misurano lead time/review"),
        kpi("…di cui senza alcuna review", f"{f['repos_with_pr_no_review']}", f"{f['repos_with_pr_no_review_pct']}% — merge senza verifica"),
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

    return f"""
<section id="view-info" hidden>
  <div class="info-wrap">
    <h2>Metodologia & findings principali</h2>
    <p class="lead">
      Estrazione completa del catalogo Developers Italia (518 software) e
      raccolta delle metriche di salute per i 476 repository ospitati su GitHub.
      Le metriche misurano <b>coinvolgimento, velocità e qualità del processo
      di sviluppo</b>, deliberatamente escludendo segnali di facciata (stelle,
      fork) che non riflettono salute manutentiva.
    </p>

    <h3>1. Numeri chiave</h3>
    <div class="kpis">{kpis}</div>

    <h3>2. Findings</h3>

    <h4>Abbandono silente diffuso</h4>
    <p>
      <b>{f['silent_abandoned']} repository ({f['silent_pct']}% del campione)</b>
      non ricevono commit da oltre un anno ma non sono dichiarati
      <code>archived</code> su GitHub. Solo <b>{f['archived']}</b> sono
      esplicitamente archiviati. Conseguenza: il catalogo li presenta come
      software vivo, ma chi prova ad adottarli non trova manutentore.
    </p>
    <div class="bars">{activity_bars}</div>

    <h4>Processo di review largamente assente</h4>
    <p>
      Solo <b>{f['repos_with_pr']}</b> repository hanno mergeato almeno una PR
      negli ultimi 90 giorni; di questi, <b>{f['repos_with_pr_no_review']}
      ({f['repos_with_pr_no_review_pct']}%)</b> hanno coverage di review pari a 0%.
      Tra i <b>{f['active_count']}</b> repository con commit recenti,
      <b>{f['direct_push']} ({f['direct_push_pct_of_active']}%)</b> spingono
      codice senza passare da pull request. La PR-with-review come pratica
      consolidata non è la norma in questo catalogo.
    </p>
    <p>
      Lead time mediano (tra repo con PR): <b>{f['overall_lead_median']} giorni</b>.
      Review coverage mediana: <b>{f['overall_review_median']}%</b>.
    </p>

    <h4>Automazione: CI presente in 1 repo su 3</h4>
    <p>
      <b>{f['has_ci']} ({f['has_ci_pct']}%)</b> hanno CI configurato (workflow
      GitHub Actions o equivalenti). <b>{f['has_dependabot']} ({f['has_dependabot_pct']}%)</b>
      ricevono aggiornamenti automatici delle dipendenze (Dependabot/Renovate).
      La maggioranza non ha test automatici né monitoraggio passivo delle CVE
      nelle dipendenze.
    </p>

    <h4>Rischio bus factor</h4>
    <p>
      <b>{f['bus_risk']}</b> repository hanno oltre il 90% dei commit recenti
      concentrati su un singolo autore. In caso di abbandono di quella persona,
      il software non avrebbe continuità.
    </p>

    <h4>"Zombie del catalogo": <code>stable</code> ma silenti da oltre 2 anni</h4>
    <p>
      <b>{f['zombies_count']} repository</b> sono dichiarati
      <code>developmentStatus: stable</code> nel publiccode.yml ma non ricevono
      commit da oltre due anni e non sono archiviati. Top 10:
    </p>
    {repo_list(f['zombies_top10'])}

    <h4>I 10 più attivi (commit nel default branch, ultimi 90 giorni)</h4>
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
      <li><b>Repo non più raggiungibili</b>: il catalogo include URL che restituiscono 404 (repo cancellati o rinominati) o 403 (repo resi privati): segnalati come errore nel pannello.</li>
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
    enriched = enrich(metrics, catalog)
    payload = json.dumps(enriched, ensure_ascii=False, default=str)
    findings = compute_findings(enriched)
    info_html = render_info_html(findings)
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    html = template.replace("__DATA__", payload).replace("__INFO__", info_html)
    OUT.write_text(html, encoding="utf-8")
    print(f"Generato {OUT} ({OUT.stat().st_size // 1024} KB) con {len(enriched)} repository")


if __name__ == "__main__":
    main()
