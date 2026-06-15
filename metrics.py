"""Estrae metriche di salute repository per i software del catalogo.

Funziona su qualsiasi provider supportato (GitHub, GitLab — gitlab.com e
istanze self-hosted — e Bitbucket Cloud) producendo un unico CSV/JSONL con
le stesse colonne, indipendentemente da dove è ospitato il repository.

Le metriche misurano coinvolgimento, velocità e qualità del processo di
sviluppo, deliberatamente escludendo segnali di facciata (stelle, fork):
ciclo di vita delle PR (lead time apertura→merge), copertura delle review,
frequenza dei commit, gestione delle issue, freschezza di dipendenze e codice.

────────────────────────────────────────────────────────────────────────────
Architettura
────────────────────────────────────────────────────────────────────────────
Il codice è diviso in due strati per evitare duplicazione:

  1. ASSEMBLATORI DI METRICHE (provider-agnostici)
     Funzioni `*_metrics()` che, ricevuti dati già *normalizzati* nelle
     strutture `Commit` / `PullReq` / `Issue`, calcolano i blocchi di
     metriche finali. Questa logica è identica per ogni provider e vive in
     un posto solo.

  2. FETCHER PER PROVIDER (specifici)
     `fetch_github()`, `fetch_gitlab()`, `fetch_bitbucket()` parlano con le
     rispettive API (necessariamente diverse), estraggono i dati grezzi e li
     traducono nelle strutture normalizzate, poi delegano agli assemblatori.

Il dispatcher `fetch_repo()` sceglie il fetcher giusto in base all'URL.

────────────────────────────────────────────────────────────────────────────
Uso
────────────────────────────────────────────────────────────────────────────
    python3 metrics.py                               # tutto il catalogo
    python3 metrics.py --repos URL1,URL2,URL3         # solo questi
    python3 metrics.py --sample 20                    # 20 repo casuali
    python3 metrics.py --limit 50                      # primi 50
    python3 metrics.py --providers github,gitlab       # filtra per provider
    python3 metrics.py --window-days 180               # finestra diversa

Requisiti: `gh auth login` già eseguito (serve solo per i repo GitHub; il
token viene letto via `gh auth token`). GitLab e Bitbucket sono interrogati
in forma anonima e funzionano solo per repository pubblici.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
import subprocess
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple
from urllib.parse import quote, urlparse

import requests

# ════════════════════════════════════════════════════════════════════════════
# Costanti condivise
# ════════════════════════════════════════════════════════════════════════════

#: Username dei bot di aggiornamento dipendenze, su tutti i provider.
#: Il confronto è sempre in minuscolo.
DEPENDABOT_LOGINS = {
    "dependabot", "dependabot[bot]", "dependabot-preview", "dependabot-preview[bot]",
    "dependabot-bot", "renovate", "renovate[bot]", "renovate-bot", "pyup-bot", "snyk-bot",
}

#: Nomi di file (nella root del repo) che denotano un manifest di dipendenze.
DEP_FILE_NAMES = {
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "requirements.txt", "requirements-dev.txt", "Pipfile", "Pipfile.lock",
    "pyproject.toml", "poetry.lock",
    "go.mod", "go.sum",
    "pom.xml", "build.gradle", "build.gradle.kts", "gradle.lockfile",
    "Gemfile", "Gemfile.lock",
    "composer.json", "composer.lock",
    "Cargo.toml", "Cargo.lock",
    "mix.exs",
}

#: File di configurazione CI nella root (oltre alla cartella .github/workflows
#: che su GitHub è gestita a parte).
CI_FILE_NAMES = {
    ".gitlab-ci.yml", "bitbucket-pipelines.yml", "Jenkinsfile",
    "azure-pipelines.yml", ".travis.yml", ".circleci",
}

#: Colonne del CSV di output, nell'ordine voluto. Il JSONL contiene gli stessi
#: campi (più eventuali extra) senza vincoli d'ordine.
CSV_COLUMNS = [
    "url", "provider", "name_with_owner", "primary_language", "license",
    "archived", "disabled", "fork", "empty",
    "created_at", "pushed_at", "last_commit_date", "days_since_last_commit",
    "window_days",
    "commits_in_window", "active_days_in_window", "commit_authors_in_window",
    "bus_factor_top1_pct",
    "pr_total_in_window", "pr_merged_in_window",
    "pr_median_lead_time_days", "pr_p90_lead_time_days",
    "pr_review_coverage_pct", "pr_distinct_authors_in_window",
    "issues_open_now", "oldest_open_issue_days",
    "issues_opened_in_window", "issues_closed_in_window",
    "issue_close_ratio_pct", "median_issue_close_time_days",
    "dependabot_pr_in_window", "dependency_files", "has_ci",
    "releases_total", "latest_release_date",
    "error",
]


# ════════════════════════════════════════════════════════════════════════════
# Utility temporali e statistiche
# ════════════════════════════════════════════════════════════════════════════

def iso(dt: datetime) -> str:
    """Formatta un datetime in ISO-8601 UTC con suffisso Z (formato API)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_dt(s: str | None) -> datetime | None:
    """Parsa un timestamp ISO-8601 in datetime *timezone-aware* (UTC).

    Tollerante: accetta sia il suffisso ``Z`` sia offset espliciti, e
    assume UTC quando il fuso manca. Restituisce None su input vuoto/non valido.
    """
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def pct(num: float, den: float) -> float:
    """Percentuale arrotondata a 1 decimale; 0.0 se il denominatore è nullo."""
    return round(100.0 * num / den, 1) if den else 0.0


def percentile(values: list[float], p: float) -> float:
    """Percentile lineare-interpolato (p in [0,1]); 0.0 su lista vuota."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return round(s[f], 2)
    return round(s[f] + (s[c] - s[f]) * (k - f), 2)


# ════════════════════════════════════════════════════════════════════════════
# Strutture dati normalizzate
#
# Ogni fetcher converte le risposte (eterogenee) delle API in liste di questi
# tre tipi. Da qui in poi il calcolo è identico per tutti i provider.
# ════════════════════════════════════════════════════════════════════════════

class Commit(NamedTuple):
    """Un commit sul branch di default."""
    date: datetime | None
    author: str  #: chiave stabile per l'autore (login, email o nome)


class PullReq(NamedTuple):
    """Una pull request / merge request."""
    created: datetime | None
    merged: datetime | None  #: None se non mergeata
    reviewed: bool           #: True se ha ricevuto almeno una review/commento
    author: str              #: login/username/nickname dell'autore


class Issue(NamedTuple):
    """Una issue."""
    created: datetime | None
    closed: datetime | None  #: None se ancora aperta


# ════════════════════════════════════════════════════════════════════════════
# Assemblatori di metriche (provider-agnostici)
# ════════════════════════════════════════════════════════════════════════════

def commit_metrics(
    total_in_window: int,
    commits: list[Commit],
    fallback_last: datetime | None,
    now: datetime,
) -> dict[str, Any]:
    """Metriche di attività commit.

    `total_in_window` è il conteggio reale dei commit in finestra (che può
    superare il numero di `commits` materializzati: alcune API ne restituiscono
    solo i primi 100). Giorni attivi, autori e bus factor sono calcolati sul
    campione disponibile `commits`. `fallback_last` è l'ultima attività nota
    del repo, usata per la data dell'ultimo commit quando la finestra è vuota.
    """
    dates = [c.date for c in commits if c.date]
    authors: Counter = Counter(c.author for c in commits if c.author)
    last = max(dates) if dates else fallback_last
    return {
        "commits_in_window": total_in_window,
        "active_days_in_window": len({d.date() for d in dates}),
        "commit_authors_in_window": len(authors),
        "bus_factor_top1_pct": (
            pct(authors.most_common(1)[0][1], sum(authors.values())) if authors else 0.0
        ),
        "last_commit_date": iso(last) if last else "",
        "days_since_last_commit": (now - last).days if last else "",
    }


def pr_metrics(prs: list[PullReq], since: datetime) -> dict[str, Any]:
    """Metriche di velocità e qualità delle pull request.

    `prs` è il campione delle PR più recenti restituite dal provider. Il
    filtro temporale (apertura/merge dentro la finestra) è applicato qui, così
    che ogni provider passi semplicemente la lista grezza normalizzata.
    """
    in_window = [p for p in prs if p.created and p.created >= since]
    merged = [p for p in in_window if p.merged and p.merged >= since]

    lead_times = [
        (p.merged - p.created).total_seconds() / 86400.0
        for p in merged if p.created and p.merged
    ]
    reviewed = sum(1 for p in merged if p.reviewed)
    authors = {p.author for p in in_window if p.author}
    bots = sum(1 for p in in_window if (p.author or "").lower() in DEPENDABOT_LOGINS)

    return {
        "pr_total_in_window": len(in_window),
        "pr_merged_in_window": len(merged),
        "pr_median_lead_time_days": round(statistics.median(lead_times), 2) if lead_times else "",
        "pr_p90_lead_time_days": percentile(lead_times, 0.9) if lead_times else "",
        "pr_review_coverage_pct": pct(reviewed, len(merged)) if merged else "",
        "pr_distinct_authors_in_window": len(authors),
        "dependabot_pr_in_window": bots,
    }


def issue_metrics(
    open_count: int | str,
    oldest_created: datetime | None,
    recent: list[Issue] | None,
    since: datetime,
    now: datetime,
) -> dict[str, Any]:
    """Metriche sulle issue.

    `open_count` è il numero di issue aperte (passa "" se il provider non lo
    espone). `oldest_created` è la data della issue aperta più vecchia.
    `recent` è il campione di issue recenti per il calcolo del tasso di
    chiusura in finestra; passa ``None`` se il provider non rende disponibili
    questi eventi (es. Bitbucket con issue tracker disabilitato), nel qual
    caso i campi relativi alla finestra restano vuoti.
    """
    out: dict[str, Any] = {
        "issues_open_now": open_count,
        "oldest_open_issue_days": (now - oldest_created).days if oldest_created else "",
    }
    if recent is None:
        out.update({
            "issues_opened_in_window": "",
            "issues_closed_in_window": "",
            "issue_close_ratio_pct": "",
            "median_issue_close_time_days": "",
        })
        return out

    opened = [i for i in recent if i.created and i.created >= since]
    closed = [i for i in recent if i.closed and i.closed >= since]
    close_times = [
        (i.closed - i.created).total_seconds() / 86400.0
        for i in closed if i.created and i.closed
    ]
    out.update({
        "issues_opened_in_window": len(opened),
        "issues_closed_in_window": len(closed),
        "issue_close_ratio_pct": pct(len(closed), len(opened)) if opened else "",
        "median_issue_close_time_days": round(statistics.median(close_times), 2) if close_times else "",
    })
    return out


def release_metrics(total: int | str, latest_date: str | None) -> dict[str, Any]:
    """Conteggio release e data dell'ultima (passa "" se non disponibile)."""
    return {
        "releases_total": total,
        "latest_release_date": latest_date or "",
    }


def tree_metrics(root_names: set[str], extra_ci: bool = False) -> dict[str, Any]:
    """Rileva manifest di dipendenze e presenza di CI dalla root del repo.

    `root_names` è l'insieme dei nomi di file/cartelle nella root. `extra_ci`
    consente al fetcher GitHub di segnalare la presenza della cartella
    ``.github/workflows`` (che non compare come file singolo nella root).
    """
    return {
        "dependency_files": "; ".join(sorted(n for n in root_names if n in DEP_FILE_NAMES)),
        "has_ci": extra_ci or bool(root_names & CI_FILE_NAMES),
    }


def assemble(provider: str, window_days: int, meta: dict, *blocks: dict) -> dict[str, Any]:
    """Compone il record finale unendo metadati e blocchi di metriche."""
    row: dict[str, Any] = {"provider": provider, "window_days": window_days}
    row.update(meta)
    for b in blocks:
        row.update(b)
    return row


# ════════════════════════════════════════════════════════════════════════════
# Classificazione dell'URL → provider
# ════════════════════════════════════════════════════════════════════════════

class Target(NamedTuple):
    """Repository da analizzare, già classificato."""
    provider: str  #: "github" | "gitlab" | "bitbucket"
    host: str
    path: str      #: "owner/repo" (github/bitbucket) o "group/.../repo" (gitlab)
    url: str       #: URL originale dal catalogo


def classify(url: str) -> Target | None:
    """Determina provider/host/path da un URL di repository.

    GitHub e Bitbucket usano sempre il pattern ``owner/repo``. Qualsiasi altro
    host viene trattato come GitLab (gitlab.com o self-hosted), che ammette
    namespace annidati (``gruppo/sottogruppo/progetto``). Restituisce None se
    l'URL non è interpretabile (manca host o path).
    """
    p = urlparse(url.strip())
    host = p.netloc.lower()
    path = p.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not host or not path:
        return None

    if host == "github.com":
        parts = path.split("/")
        if len(parts) < 2:
            return None
        return Target("github", host, f"{parts[0]}/{parts[1]}", url)
    if host == "bitbucket.org":
        parts = path.split("/")
        if len(parts) < 2:
            return None
        return Target("bitbucket", host, f"{parts[0]}/{parts[1]}", url)
    # Tutto il resto: GitLab (path completo, namespace annidati ammessi)
    return Target("gitlab", host, path, url)


# ════════════════════════════════════════════════════════════════════════════
# Provider: GitHub (GraphQL)
# ════════════════════════════════════════════════════════════════════════════

GITHUB_GRAPHQL = "https://api.github.com/graphql"

#: Una sola query GraphQL per repo recupera tutto il necessario, minimizzando
#: il consumo di rate limit.
GRAPHQL_QUERY = """
query($owner: String!, $name: String!, $since: GitTimestamp!, $sinceDT: DateTime!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner
    isArchived
    isDisabled
    isFork
    isEmpty
    createdAt
    pushedAt
    primaryLanguage { name }
    licenseInfo { spdxId }
    defaultBranchRef {
      target {
        ... on Commit {
          history(since: $since, first: 100) {
            totalCount
            nodes { committedDate author { user { login } name email } }
          }
        }
      }
    }
    pullRequests(first: 100, orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes {
        author { login }
        createdAt
        mergedAt
        reviewDecision
        reviews(first: 1) { totalCount }
      }
    }
    openIssues: issues(states: OPEN) { totalCount }
    oldestOpenIssue: issues(states: OPEN, first: 1, orderBy: {field: CREATED_AT, direction: ASC}) {
      nodes { createdAt }
    }
    recentIssues: issues(first: 100, orderBy: {field: UPDATED_AT, direction: DESC}, filterBy: {since: $sinceDT}) {
      nodes { createdAt closedAt }
    }
    releases(first: 5, orderBy: {field: CREATED_AT, direction: DESC}) {
      totalCount
      nodes { createdAt }
    }
    workflows: object(expression: "HEAD:.github/workflows") {
      ... on Tree { entries { name } }
    }
    rootTree: object(expression: "HEAD:") {
      ... on Tree { entries { name } }
    }
  }
}
"""

_TOKEN: str | None = None


def gh_token() -> str:
    """Legge (una volta) il token di GitHub dalla CLI ``gh`` già autenticata."""
    global _TOKEN
    if _TOKEN is None:
        _TOKEN = subprocess.check_output(["gh", "auth", "token"], text=True).strip()
    return _TOKEN


def _graphql(owner: str, name: str, since: datetime) -> dict[str, Any]:
    """Esegue la query GraphQL con retry su errori transitori e rate limit."""
    headers = {"Authorization": f"bearer {gh_token()}"}
    payload = {
        "query": GRAPHQL_QUERY,
        "variables": {"owner": owner, "name": name, "since": iso(since), "sinceDT": iso(since)},
    }
    for attempt in range(5):
        r = requests.post(GITHUB_GRAPHQL, json=payload, headers=headers, timeout=45)
        if r.status_code == 200:
            data = r.json()
            if "errors" in data and not (data.get("data") or {}).get("repository"):
                raise RuntimeError(f"GraphQL errors: {data['errors']}")
            return data
        if r.status_code in (502, 503, 504):
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 403:  # secondary rate limit
            time.sleep(60)
            continue
        r.raise_for_status()
    raise RuntimeError("graphql: max retries")


def fetch_github(t: Target, since: datetime, now: datetime, window_days: int) -> dict[str, Any]:
    """Estrae le metriche di un repository GitHub via API GraphQL."""
    owner, name = t.path.split("/", 1)
    repo = (_graphql(owner, name, since).get("data") or {}).get("repository")
    if not repo:
        raise RuntimeError("repo non trovato o privato")

    # --- normalizzazione dei dati grezzi ---
    history = (((repo.get("defaultBranchRef") or {}).get("target") or {}).get("history") or {})
    commits = []
    for c in history.get("nodes") or []:
        a = c.get("author") or {}
        key = (a.get("user") or {}).get("login") or a.get("email") or a.get("name") or "unknown"
        commits.append(Commit(parse_dt(c.get("committedDate")), key))

    prs = []
    for p in (repo.get("pullRequests") or {}).get("nodes") or []:
        reviewed = ((p.get("reviews") or {}).get("totalCount", 0) > 0) or bool(p.get("reviewDecision"))
        prs.append(PullReq(
            created=parse_dt(p.get("createdAt")),
            merged=parse_dt(p.get("mergedAt")),
            reviewed=reviewed,
            author=(p.get("author") or {}).get("login", ""),
        ))

    oldest_nodes = (repo.get("oldestOpenIssue") or {}).get("nodes") or []
    oldest_created = parse_dt(oldest_nodes[0].get("createdAt")) if oldest_nodes else None
    recent = [
        Issue(parse_dt(i.get("createdAt")), parse_dt(i.get("closedAt")))
        for i in (repo.get("recentIssues") or {}).get("nodes") or []
    ]

    releases = repo.get("releases") or {}
    rel_nodes = releases.get("nodes") or []

    root_names = {e.get("name") for e in ((repo.get("rootTree") or {}).get("entries") or [])}
    has_workflows = bool((repo.get("workflows") or {}).get("entries"))

    meta = {
        "name_with_owner": repo.get("nameWithOwner", t.path),
        "primary_language": (repo.get("primaryLanguage") or {}).get("name") or "",
        "license": (repo.get("licenseInfo") or {}).get("spdxId") or "",
        "archived": bool(repo.get("isArchived")),
        "disabled": bool(repo.get("isDisabled")),
        "fork": bool(repo.get("isFork")),
        "empty": bool(repo.get("isEmpty")),
        "created_at": repo.get("createdAt", ""),
        "pushed_at": repo.get("pushedAt", ""),
    }
    return assemble(
        "github", window_days, meta,
        commit_metrics(history.get("totalCount", 0), commits, parse_dt(repo.get("pushedAt")), now),
        pr_metrics(prs, since),
        issue_metrics((repo.get("openIssues") or {}).get("totalCount", 0), oldest_created, recent, since, now),
        release_metrics(releases.get("totalCount", 0), rel_nodes[0].get("createdAt") if rel_nodes else ""),
        tree_metrics(root_names, extra_ci=has_workflows),
    )


# ════════════════════════════════════════════════════════════════════════════
# Provider: GitLab (REST v4)
# ════════════════════════════════════════════════════════════════════════════

class GitLabClient:
    """Client REST minimale per un'istanza GitLab (anonimo, repo pubblici)."""

    def __init__(self, host: str):
        self.base = f"https://{host}/api/v4"
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "catalogo-metrics/1.0"})

    def get(self, path: str, params: dict | None = None, allow_404: bool = True):
        """GET su un endpoint dell'API, con retry su 5xx.

        Restituisce il JSON deserializzato, o None su 404 quando `allow_404`.
        Solleva RuntimeError su 401/403 (repo privato o accesso negato).
        """
        url = self.base + path
        for attempt in range(3):
            try:
                r = self.s.get(url, params=params, timeout=20)
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(1.5 ** attempt)
                continue
            if r.status_code in (502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 404:
                if allow_404:
                    return None
                raise RuntimeError("gitlab: 404")
            if r.status_code in (401, 403):
                raise RuntimeError(f"gitlab: {r.status_code} (privato o auth richiesta)")
            r.raise_for_status()
            try:
                return r.json()
            except ValueError:
                return None
        return None


def fetch_gitlab(t: Target, since: datetime, now: datetime, window_days: int) -> dict[str, Any]:
    """Estrae le metriche di un progetto GitLab via API REST v4."""
    c = GitLabClient(t.host)
    enc = quote(t.path, safe="")  # il path completo va URL-encoded come ID progetto

    proj = c.get(f"/projects/{enc}", {"license": "true"}, allow_404=False)
    if not proj:
        raise RuntimeError("gitlab: progetto non trovato")
    branch = proj.get("default_branch") or "main"

    # --- chiamate API ---
    raw_commits = c.get(f"/projects/{enc}/repository/commits",
                        {"ref_name": branch, "since": iso(since), "per_page": 100}) or []
    langs = c.get(f"/projects/{enc}/languages") or {}
    raw_mrs = c.get(f"/projects/{enc}/merge_requests",
                    {"state": "all", "order_by": "created_at", "sort": "desc",
                     "created_after": iso(since), "per_page": 100}) or []
    stats = c.get(f"/projects/{enc}/issues_statistics") or {}
    open_count = stats.get("statistics", {}).get("counts", {}).get("opened", 0)
    oldest = c.get(f"/projects/{enc}/issues",
                   {"state": "opened", "order_by": "created_at", "sort": "asc", "per_page": 1}) or []
    raw_issues = c.get(f"/projects/{enc}/issues",
                       {"created_after": iso(since), "order_by": "updated_at", "sort": "desc", "per_page": 100}) or []
    releases = c.get(f"/projects/{enc}/releases", {"per_page": 5}) or []
    tree = c.get(f"/projects/{enc}/repository/tree", {"ref": branch, "per_page": 100}) or []

    # --- normalizzazione ---
    commits = [Commit(parse_dt(cm.get("committed_date")),
                      cm.get("author_email") or cm.get("author_name") or "unknown")
               for cm in raw_commits]
    prs = [PullReq(
        created=parse_dt(m.get("created_at")),
        merged=parse_dt(m.get("merged_at")) if m.get("state") == "merged" else None,
        # GitLab anonimo non espone le approvals: usiamo i commenti come proxy di review
        reviewed=bool((m.get("user_notes_count") or 0) > 0 or m.get("reviewers")),
        author=(m.get("author") or {}).get("username", ""),
    ) for m in raw_mrs]
    oldest_created = parse_dt(oldest[0].get("created_at")) if oldest else None
    issues = [Issue(parse_dt(i.get("created_at")), parse_dt(i.get("closed_at"))) for i in raw_issues]
    root_names = {e.get("name") for e in tree if isinstance(e, dict)}

    lic = proj.get("license") or {}
    meta = {
        "name_with_owner": proj.get("path_with_namespace", t.path),
        "primary_language": max(langs.items(), key=lambda x: x[1])[0] if langs else "",
        "license": lic.get("key") or lic.get("nickname") or "",
        "archived": bool(proj.get("archived")),
        "disabled": bool(proj.get("issues_access_level") == "disabled"
                         and proj.get("merge_requests_access_level") == "disabled"),
        "fork": bool(proj.get("forked_from_project")),
        "empty": bool(proj.get("empty_repo")),
        "created_at": proj.get("created_at", ""),
        "pushed_at": proj.get("last_activity_at", ""),
    }
    return assemble(
        "gitlab", window_days, meta,
        commit_metrics(len(commits), commits, parse_dt(proj.get("last_activity_at")), now),
        pr_metrics(prs, since),
        issue_metrics(open_count, oldest_created, issues, since, now),
        release_metrics(len(releases), releases[0].get("created_at") if releases else ""),
        tree_metrics(root_names),
    )


# ════════════════════════════════════════════════════════════════════════════
# Provider: Bitbucket Cloud (REST 2.0)
# ════════════════════════════════════════════════════════════════════════════

class BitbucketClient:
    """Client REST minimale per Bitbucket Cloud (anonimo, repo pubblici)."""

    BASE = "https://api.bitbucket.org/2.0"

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "catalogo-metrics/1.0", "Accept": "application/json"})

    def get(self, path: str, params: dict | None = None, allow_404: bool = True):
        """GET su un endpoint, con retry su 429/5xx."""
        url = self.BASE + path
        for attempt in range(3):
            try:
                r = self.s.get(url, params=params, timeout=20)
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(1.5 ** attempt)
                continue
            if r.status_code == 429:
                time.sleep(10)
                continue
            if r.status_code in (502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 404 and allow_404:
                return None
            r.raise_for_status()
            return r.json()
        return None

    def paginate(self, path: str, params: dict, max_pages: int = 3) -> list[dict]:
        """Segue i link `next` dell'API paginata, fino a `max_pages` pagine."""
        out: list[dict] = []
        cur_path, cur_params = path, dict(params)
        for _ in range(max_pages):
            data = self.get(cur_path, cur_params)
            if not data:
                break
            out.extend(data.get("values") or [])
            nxt = data.get("next")
            if not nxt:
                break
            cur_path, cur_params = nxt.replace(self.BASE, ""), None
        return out


def fetch_bitbucket(t: Target, since: datetime, now: datetime, window_days: int) -> dict[str, Any]:
    """Estrae le metriche di un repository Bitbucket Cloud via API REST 2.0.

    Nota: l'API di Bitbucket non espone licenza né release in forma standard,
    e l'issue tracker può essere disabilitato; i campi non disponibili restano
    vuoti (gestiti dagli assemblatori con `recent=None` e total="" ).
    """
    c = BitbucketClient()
    repo = c.get(f"/repositories/{t.path}", allow_404=False)
    if not repo:
        raise RuntimeError("bitbucket: repo non trovato")
    branch = (repo.get("mainbranch") or {}).get("name") or "main"

    # --- chiamate API ---
    raw_commits = c.paginate(f"/repositories/{t.path}/commits/{branch}", {"pagelen": 100}, max_pages=3)
    q = f'updated_on >= "{iso(since)}"'
    raw_merged = c.paginate(f"/repositories/{t.path}/pullrequests",
                            {"state": "MERGED", "q": q, "pagelen": 50}, max_pages=2)
    raw_open = c.paginate(f"/repositories/{t.path}/pullrequests",
                          {"state": "OPEN", "q": q, "pagelen": 50}, max_pages=2)
    root = c.get(f"/repositories/{t.path}/src/{branch}/", {"pagelen": 100})

    # Issue: disponibili solo se il tracker è abilitato → best-effort
    open_count: int | str = ""
    oldest_created = None
    try:
        oi = c.get(f"/repositories/{t.path}/issues",
                   {"q": 'state="new" OR state="open"', "pagelen": 1, "sort": "created_on"})
        if oi:
            open_count = oi.get("size", 0)
            vals = oi.get("values") or []
            if vals:
                oldest_created = parse_dt(vals[0].get("created_on"))
    except requests.HTTPError:
        pass  # tracker disabilitato: lasciamo i campi issue vuoti

    # --- normalizzazione ---
    commits = [Commit(parse_dt(cm.get("date")), (cm.get("author") or {}).get("raw", "unknown"))
               for cm in raw_commits]
    commits = [c for c in commits if c.date and c.date >= since]
    prs = [PullReq(
        created=parse_dt(p.get("created_on")),
        merged=parse_dt(p.get("updated_on")),  # proxy: data di chiusura/merge della MR
        reviewed=bool(p.get("participants")),
        author=(p.get("author") or {}).get("nickname", ""),
    ) for p in raw_merged]
    prs += [PullReq(parse_dt(p.get("created_on")), None, bool(p.get("participants")),
                    (p.get("author") or {}).get("nickname", "")) for p in raw_open]

    root_names = {(e.get("path") or "").split("/")[-1] for e in (root.get("values") or [])} if root else set()

    meta = {
        "name_with_owner": repo.get("full_name", t.path),
        "primary_language": repo.get("language", "") or "",
        "license": "",  # non esposta dall'API Bitbucket
        "archived": False,
        "disabled": False,
        "fork": bool(repo.get("parent")),
        "empty": False,
        "created_at": repo.get("created_on", ""),
        "pushed_at": repo.get("updated_on", ""),
    }
    return assemble(
        "bitbucket", window_days, meta,
        commit_metrics(len(commits), commits, parse_dt(repo.get("updated_on")), now),
        pr_metrics(prs, since),
        issue_metrics(open_count, oldest_created, None, since, now),  # recent=None: niente finestra
        release_metrics("", ""),  # release non esposte
        tree_metrics(root_names),
    )


# ════════════════════════════════════════════════════════════════════════════
# Dispatcher
# ════════════════════════════════════════════════════════════════════════════

#: Mappa provider → funzione di fetch. Aggiungere un provider = aggiungere una
#: riga qui e la relativa `fetch_*`.
FETCHERS: dict[str, Callable[[Target, datetime, datetime, int], dict[str, Any]]] = {
    "github": fetch_github,
    "gitlab": fetch_gitlab,
    "bitbucket": fetch_bitbucket,
}


def fetch_repo(url: str, since: datetime, now: datetime, window_days: int) -> dict[str, Any]:
    """Classifica l'URL e delega al fetcher del provider corrispondente.

    Restituisce sempre un dict con almeno `url`, `provider` e (in caso di
    problema) `error`, così il chiamante può scriverlo senza casi speciali.
    """
    t = classify(url)
    if t is None:
        return {"url": url, "provider": "unknown", "error": "URL non interpretabile"}
    try:
        row = FETCHERS[t.provider](t, since, now, window_days)
        row["url"] = url
        return row
    except Exception as e:  # noqa: BLE001 — vogliamo proseguire sul resto del catalogo
        return {"url": url, "provider": t.provider, "error": str(e)[:200]}


# ════════════════════════════════════════════════════════════════════════════
# Driver / CLI
# ════════════════════════════════════════════════════════════════════════════

def iter_catalog_repos(jsonl_path: Path):
    """Itera gli URL repository DISTINTI dal catalogo (data/software.jsonl).

    Più schede catalogo possono dichiarare lo stesso repo upstream — es. Matomo
    è pubblicato sia da `italia-software/matomo` sia da `RegioneER/publiccode-matomo`
    ma il publiccode.yml di entrambe punta a `matomo-org/matomo`. Senza dedup
    interrogheremmo la stessa API due volte ottenendo righe identiche in output.
    """
    seen: set[str] = set()
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            pc = rec.get("publiccode") or {}
            url = pc.get("url") or rec.get("url") or ""
            if url and url not in seen:
                seen.add(url)
                yield url


def select_targets(args: argparse.Namespace) -> list[str]:
    """Risolve l'elenco di URL da processare in base alle opzioni CLI."""
    if args.repos:
        urls = [u.strip() for u in args.repos.split(",") if u.strip()]
    else:
        urls = list(iter_catalog_repos(Path(args.input)))

    if args.providers:
        wanted = {p.strip().lower() for p in args.providers.split(",")}
        urls = [u for u in urls if (classify(u).provider if classify(u) else "unknown") in wanted]

    if args.sample:
        random.seed(args.seed)
        return random.sample(urls, min(args.sample, len(urls)))
    if args.limit:
        return urls[: args.limit]
    return urls


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", default="data/software.jsonl", help="catalogo JSONL di partenza")
    parser.add_argument("--out", default="data/metrics.csv", help="file CSV di output")
    parser.add_argument("--jsonl-out", default="data/metrics.jsonl", help="file JSONL di output")
    parser.add_argument("--window-days", type=int, default=90, help="ampiezza finestra metriche (default 90)")
    parser.add_argument("--delay", type=float, default=0.3, help="secondi tra le richieste")
    parser.add_argument("--repos", help="URL,URL,... invece di leggere il catalogo")
    parser.add_argument("--providers", help="filtra per provider, es. 'github,gitlab'")
    parser.add_argument("--sample", type=int, help="campione casuale di N repo")
    parser.add_argument("--limit", type=int, help="primi N repo")
    parser.add_argument("--seed", type=int, default=42, help="seed per --sample")
    args = parser.parse_args(argv)

    targets = select_targets(args)
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=args.window_days)
    print(f"Target: {len(targets)} repository (finestra: {args.window_days} giorni)")

    out_csv = Path(args.out)
    out_jsonl = Path(args.jsonl_out)
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    with out_csv.open("w", encoding="utf-8", newline="") as cf, out_jsonl.open("w", encoding="utf-8") as jf:
        writer = csv.DictWriter(cf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for i, url in enumerate(targets, 1):
            t = classify(url)
            print(f"[{i}/{len(targets)}] {t.provider if t else '?'}: {url}")
            row = fetch_repo(url, since, now, args.window_days)
            writer.writerow(row)
            jf.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            cf.flush()
            jf.flush()
            time.sleep(args.delay)

    print(f"\nFatto. Output:\n  - {out_csv}\n  - {out_jsonl}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
