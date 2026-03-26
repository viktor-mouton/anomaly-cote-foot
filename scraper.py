#!/usr/bin/env python3
"""
OddsPortal - Détecteur d'anomalies de cotes Tennis
====================================================
Scrape les matchs de tennis du jour sur oddsportal.com et détecte
les cotes anormales par rapport à la moyenne des autres bookmakers.
Focus particulier sur GGBET (cote exacte à 1.70 vs moyenne).

Usage:
    python3 scraper.py
    python3 scraper.py --threshold 0.05   # seuil anomalie 5%
    python3 scraper.py --target-odd 1.70  # cote cible GGBET
"""

import json
import re
import time
import random
import argparse
from datetime import datetime
from typing import Optional
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

BASE_URL = "https://www.oddsportal.com"
SPORT_ID = 2  # Tennis sur OddsPortal (1=Football, 2=Tennis)

# Noms possibles de GGBET sur OddsPortal
GGBET_NAMES = {"GG.bet", "GGBET", "gg.bet", "GGBet"}


class OddsPortalSession:
    """Session curl-cffi avec impersonation Chrome et gestion du rate limiting."""

    def __init__(self):
        self.session = cffi_requests.Session(impersonate="chrome120")
        self.session.headers.update(
            {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Cache-Control": "no-cache",
            }
        )
        self.csrf_token: Optional[str] = None
        self.request_count = 0

    def get(self, url: str, is_api: bool = False, max_retries: int = 5) -> Optional[cffi_requests.Response]:
        """GET avec retry exponentiel sur 429."""
        headers = {}
        if is_api:
            headers = {
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "Referer": BASE_URL + "/tennis/",
            }
            if self.csrf_token:
                headers["X-CSRF-Token"] = self.csrf_token

        for attempt in range(max_retries):
            # Délai humain entre requêtes (progressive selon les tentatives)
            if attempt == 0 and self.request_count > 0:
                time.sleep(random.uniform(3, 6))
            elif attempt > 0:
                # Backoff exponentiel : 30s, 60s, 120s, 240s
                wait = min(30 * (2 ** (attempt - 1)), 300) + random.uniform(2, 8)
                print(f"    [429] Rate limit Cloudflare, attente {wait:.0f}s (essai {attempt+1}/{max_retries})...")
                time.sleep(wait)

            try:
                r = self.session.get(url, headers=headers, timeout=30)
                self.request_count += 1

                if r.status_code == 200:
                    # Vérifier que ce n'est pas une page de challenge Cloudflare
                    if "challenge" in r.text.lower() and len(r.text) < 5000:
                        print(f"    [CF-CHALLENGE] Challenge Cloudflare détecté, attente 30s...")
                        time.sleep(30)
                        continue
                    # Extraire le CSRF token si présent
                    if not self.csrf_token:
                        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text)
                        if m:
                            self.csrf_token = m.group(1)
                    return r

                elif r.status_code == 429:
                    continue  # retry avec backoff
                elif r.status_code == 403:
                    print(f"    [403] Accès refusé (IP bloquée temporairement par Cloudflare)")
                    if attempt < max_retries - 1:
                        wait = 60 + random.uniform(10, 30)
                        print(f"    Attente {wait:.0f}s avant retry...")
                        time.sleep(wait)
                else:
                    print(f"    HTTP {r.status_code} pour {url}")
                    return None

            except Exception as e:
                print(f"    Erreur requête: {e}")
                if attempt < max_retries - 1:
                    time.sleep(10)

        print(f"    [ECHEC] Impossible d'accéder à {url} après {max_retries} essais")
        return None


class TennisScraper:
    """Scraper principal pour les matchs de tennis OddsPortal."""

    def __init__(self, session: OddsPortalSession):
        self.session = session

    # -------------------------------------------------------------------------
    # Récupération des matchs du jour
    # -------------------------------------------------------------------------

    def get_today_matches(self) -> list[dict]:
        """Récupère la liste des matchs de tennis du jour."""
        print(">>> Récupération des matchs tennis du jour...")

        r = self.session.get(f"{BASE_URL}/tennis/")
        if not r:
            return []

        # Tentative 1 : données Next.js embarquées
        matches = self._parse_nextjs_matches(r.text)
        if matches:
            print(f"    {len(matches)} matchs trouvés via __NEXT_DATA__")
            return matches

        # Tentative 2 : parsing HTML classique
        matches = self._parse_html_matches(r.text)
        if matches:
            print(f"    {len(matches)} matchs trouvés via HTML")
            return matches

        print("    Aucun match trouvé dans la page tennis")
        return []

    def _parse_nextjs_matches(self, html: str) -> list[dict]:
        """Extrait les matchs depuis le bloc __NEXT_DATA__ (Next.js)."""
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if not m:
            return []

        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []

        matches = []
        # Parcours générique des structures Next.js pour trouver les événements
        self._find_events(data, matches)
        return matches

    def _find_events(self, obj, results: list, depth: int = 0):
        """Cherche récursivement les événements tennis dans le JSON Next.js."""
        if depth > 10 or not isinstance(obj, (dict, list)):
            return

        if isinstance(obj, list):
            for item in obj:
                self._find_events(item, results, depth + 1)
            return

        # Un événement OddsPortal possède typiquement ces champs
        if all(k in obj for k in ("id", "slug")) and obj.get("sport-id", obj.get("sportId", 0)) == SPORT_ID:
            results.append(
                {
                    "id": obj.get("id"),
                    "slug": obj.get("slug", ""),
                    "url": BASE_URL + obj.get("slug", ""),
                    "home": obj.get("home", {}).get("name", "") if isinstance(obj.get("home"), dict) else obj.get("home", ""),
                    "away": obj.get("away", {}).get("name", "") if isinstance(obj.get("away"), dict) else obj.get("away", ""),
                    "tournament": obj.get("tournament-name", obj.get("tournamentName", "")),
                    "start_time": obj.get("startTime", obj.get("start-time", "")),
                }
            )
            return

        for v in obj.values():
            self._find_events(v, results, depth + 1)

    def _parse_html_matches(self, html: str) -> list[dict]:
        """Extrait les liens de matchs depuis le HTML (fallback)."""
        soup = BeautifulSoup(html, "lxml")
        seen = set()
        matches = []

        # OddsPortal : les liens de matchs tennis ont ≥4 segments /tennis/pays/tournoi/match/
        for a in soup.find_all("a", href=True):
            href = a["href"]
            parts = [p for p in href.split("/") if p]
            if (
                len(parts) >= 4
                and parts[0] == "tennis"
                and href not in seen
            ):
                seen.add(href)
                text = a.get_text(strip=True)
                matches.append(
                    {
                        "id": None,
                        "slug": href,
                        "url": BASE_URL + href,
                        "home": "",
                        "away": text,
                        "tournament": "/".join(parts[1:3]),
                        "start_time": "",
                    }
                )

        return matches

    # -------------------------------------------------------------------------
    # Récupération des cotes pour un match
    # -------------------------------------------------------------------------

    def get_match_odds(self, match: dict) -> Optional[dict]:
        """
        Récupère les cotes de tous les bookmakers pour un match.
        Retourne un dict {bookmaker_name: [odd1, odd2, ...]} ou None.
        """
        # Stratégie 1 : API v2 si on a l'event ID
        if match.get("id"):
            odds = self._get_odds_via_api(match["id"])
            if odds:
                return odds

        # Stratégie 2 : parsing de la page HTML du match
        r = self.session.get(match["url"])
        if not r:
            return None

        # Chercher les cotes dans __NEXT_DATA__
        next_m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL)
        if next_m:
            try:
                data = json.loads(next_m.group(1))
                odds = self._extract_odds_from_nextjs(data)
                if odds:
                    return odds
            except json.JSONDecodeError:
                pass

        # Stratégie 3 : chercher dans les scripts inline (données sérialisées)
        odds = self._extract_odds_from_inline_scripts(r.text)
        if odds:
            return odds

        # Stratégie 4 : parser le tableau HTML des cotes
        return self._parse_odds_table(r.text)

    def _get_odds_via_api(self, event_id: str) -> Optional[dict]:
        """
        Appel direct à l'API OddsPortal v2.
        Endpoint : /api/v2/sports/{sport}/events/{eventId}/odds/{oddsType}/
        oddsType 1 = Match Winner (1X2 ou 12)
        """
        url = f"{BASE_URL}/api/v2/sports/{SPORT_ID}/events/{event_id}/odds/1/"
        r = self.session.get(url, is_api=True)
        if not r:
            return None

        try:
            data = r.json()
            return self._parse_api_odds_response(data)
        except Exception:
            return None

    def _parse_api_odds_response(self, data: dict) -> Optional[dict]:
        """Parse la réponse JSON de l'API OddsPortal v2."""
        odds_by_bookie = {}
        try:
            # Structure typique : data.d.oddsdata.back.E_...
            odds_data = data.get("d", data).get("oddsdata", data.get("oddsdata", {}))
            back = odds_data.get("back", odds_data)

            for key, entry in back.items():
                bookie_name = entry.get("bookieName", entry.get("bookie", key))
                odds_list = []
                # Les cotes sont dans 'odds' : {0: [odd, ...], 1: [odd, ...]}
                raw_odds = entry.get("odds", {})
                for idx in sorted(raw_odds.keys(), key=int):
                    o = raw_odds[str(idx)]
                    if isinstance(o, list) and o:
                        odds_list.append(float(o[0]))
                    elif isinstance(o, (int, float)):
                        odds_list.append(float(o))

                if odds_list and bookie_name:
                    odds_by_bookie[bookie_name] = odds_list

        except Exception:
            pass

        return odds_by_bookie if odds_by_bookie else None

    def _extract_odds_from_nextjs(self, data: dict) -> Optional[dict]:
        """Extrait les cotes depuis le JSON Next.js d'une page match."""
        try:
            props = data.get("props", {}).get("pageProps", {})
            # Cherche un champ 'odds' ou 'oddsdata' en profondeur
            return self._find_odds_in_dict(props)
        except Exception:
            return None

    def _find_odds_in_dict(self, obj, depth: int = 0) -> Optional[dict]:
        """Cherche récursivement un dict de cotes par bookmaker."""
        if depth > 8 or not isinstance(obj, dict):
            return None

        # Clé directe 'odds' avec des bookmakers dedans
        if "odds" in obj and isinstance(obj["odds"], dict):
            candidate = obj["odds"]
            # Vérifie que les valeurs ressemblent à des cotes
            sample = next(iter(candidate.values()), None)
            if isinstance(sample, dict) and "bookieName" in sample:
                return self._parse_api_odds_response({"oddsdata": {"back": candidate}})
            if isinstance(sample, list):
                return candidate

        for v in obj.values():
            result = self._find_odds_in_dict(v, depth + 1)
            if result:
                return result
        return None

    def _extract_odds_from_inline_scripts(self, html: str) -> Optional[dict]:
        """Cherche des données de cotes dans les balises <script> inline."""
        # OddsPortal peut injecter des données via window.oddsData ou similaire
        patterns = [
            r'window\.oddsData\s*=\s*(\{.*?\});',
            r'var\s+oddsData\s*=\s*(\{.*?\});',
            r'"oddsdata"\s*:\s*(\{.*?"bookieName".*?\})',
        ]
        for pattern in patterns:
            m = re.search(pattern, html, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(1))
                    result = self._parse_api_odds_response(data)
                    if result:
                        return result
                except Exception:
                    continue
        return None

    def _parse_odds_table(self, html: str) -> Optional[dict]:
        """
        Parse le tableau HTML des cotes (fallback final).
        OddsPortal affiche un tableau avec lignes par bookmaker.
        """
        soup = BeautifulSoup(html, "lxml")
        odds_by_bookie = {}

        # Sélecteurs pour les tableaux de cotes OddsPortal
        selectors = [
            ("table", {"id": re.compile(r"odds", re.I)}),
            ("div", {"class": re.compile(r"odds-table|bookmaker", re.I)}),
            ("tr", {"data-bookie-id": True}),
        ]

        # Cherche un tableau avec des lignes bookmaker
        for tag, attrs in selectors:
            rows = soup.find_all(tag, attrs)
            if rows:
                for row in rows:
                    bookie_cell = row.find(
                        ["td", "div"],
                        class_=re.compile(r"bookie|bookmaker|name", re.I),
                    )
                    if not bookie_cell:
                        continue
                    bookie_name = bookie_cell.get_text(strip=True)
                    if not bookie_name:
                        continue

                    # Extraire les valeurs numériques de cotes
                    odd_cells = row.find_all(
                        ["td", "a"],
                        class_=re.compile(r"odds-nowrap|outcome|odd", re.I),
                    )
                    odd_values = []
                    for cell in odd_cells:
                        txt = cell.get_text(strip=True)
                        try:
                            odd_values.append(float(txt))
                        except ValueError:
                            pass

                    if odd_values:
                        odds_by_bookie[bookie_name] = odd_values

                if odds_by_bookie:
                    return odds_by_bookie

        return None


# =============================================================================
# Analyse des anomalies
# =============================================================================

def find_ggbet_anomalies(
    matches_with_odds: list[dict],
    target_odd: float = 1.70,
    threshold: float = 0.10,
) -> list[dict]:
    """
    Détecte les anomalies GGBET :
    - Cote GGBET proche de target_odd (±0.05)
    - Écart >= threshold (10%) avec la moyenne des autres bookmakers
    """
    anomalies = []

    for match in matches_with_odds:
        match_name = match.get("name", "?")
        odds_by_bookie = match.get("odds", {})

        if not odds_by_bookie:
            continue

        # Identifier GGBET dans les bookmakers disponibles
        ggbet_key = None
        for k in odds_by_bookie:
            if k in GGBET_NAMES or "gg" in k.lower():
                ggbet_key = k
                break

        if not ggbet_key:
            continue

        ggbet_odds = odds_by_bookie[ggbet_key]
        other_books = {k: v for k, v in odds_by_bookie.items() if k != ggbet_key}

        if not other_books:
            continue

        for i, gg_odd in enumerate(ggbet_odds):
            try:
                gg_val = float(gg_odd)
            except (TypeError, ValueError):
                continue

            # Calculer la moyenne des autres bookmakers pour ce même résultat
            other_vals = []
            for vals in other_books.values():
                if isinstance(vals, list) and len(vals) > i:
                    try:
                        other_vals.append(float(vals[i]))
                    except (TypeError, ValueError):
                        pass

            if len(other_vals) < 2:
                continue

            avg_other = sum(other_vals) / len(other_vals)
            if avg_other == 0:
                continue

            diff_pct = abs(gg_val - avg_other) / avg_other

            anomalies.append(
                {
                    "match": match_name,
                    "outcome_idx": i,
                    "outcome_label": ["Joueur 1", "Joueur 2", "Nul"][i] if i < 3 else f"Résultat {i+1}",
                    "ggbet_odd": gg_val,
                    "avg_other": round(avg_other, 3),
                    "diff_pct": round(diff_pct * 100, 1),
                    "is_target": abs(gg_val - target_odd) <= 0.05,
                    "is_anomaly": diff_pct >= threshold,
                    "n_bookmakers": len(other_vals),
                    "other_odds": {
                        k: v[i]
                        for k, v in other_books.items()
                        if isinstance(v, list) and len(v) > i
                    },
                }
            )

    return anomalies


def print_report(
    matches_with_odds: list[dict],
    anomalies: list[dict],
    target_odd: float,
    threshold: float,
):
    """Affiche un rapport clair des anomalies détectées."""
    date_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    print("\n" + "=" * 70)
    print(f"  RAPPORT ANOMALIES COTES TENNIS - {date_str}")
    print(f"  Cote cible GGBET : {target_odd} | Seuil anomalie : {threshold*100:.0f}%")
    print("=" * 70)

    total_matches = len(matches_with_odds)
    matches_with_data = sum(1 for m in matches_with_odds if m.get("odds"))
    print(f"\nMatchs trouvés    : {total_matches}")
    print(f"Avec cotes        : {matches_with_data}")
    print(f"Anomalies GGBET   : {len([a for a in anomalies if a['is_anomaly']])}")
    print(f"Cote {target_odd} GGBET  : {len([a for a in anomalies if a['is_target']])}")

    # Anomalies critiques : cote GGBET = target_odd ET écart significatif
    critical = [a for a in anomalies if a["is_target"] and a["is_anomaly"]]
    if critical:
        print(f"\n{'='*70}")
        print(f"  *** {len(critical)} CAS CRITIQUES : GGBET à {target_odd} avec écart ≥{threshold*100:.0f}% ***")
        print(f"{'='*70}")
        for a in critical:
            _print_anomaly(a)

    # Autres anomalies GGBET (écart significatif même sans cote cible)
    other_anomalies = [a for a in anomalies if a["is_anomaly"] and not a["is_target"]]
    if other_anomalies:
        print(f"\n{'─'*70}")
        print(f"  Autres anomalies GGBET (écart ≥{threshold*100:.0f}%) :")
        print(f"{'─'*70}")
        for a in other_anomalies:
            _print_anomaly(a)

    # GGBET à 1.70 sans anomalie (pour info)
    target_no_anomaly = [a for a in anomalies if a["is_target"] and not a["is_anomaly"]]
    if target_no_anomaly:
        print(f"\n{'─'*70}")
        print(f"  GGBET à {target_odd} (écart < {threshold*100:.0f}%, pas d'anomalie) :")
        print(f"{'─'*70}")
        for a in target_no_anomaly:
            print(f"  Match : {a['match']} | {a['outcome_label']}")
            print(f"    GGBET : {a['ggbet_odd']} | Moyenne autres : {a['avg_other']} | Écart : {a['diff_pct']}%")

    if not anomalies:
        print("\n  Aucune anomalie détectée aujourd'hui.")

    print("\n" + "=" * 70)


def _print_anomaly(a: dict):
    print(f"\n  MATCH     : {a['match']}")
    print(f"  Résultat  : {a['outcome_label']}")
    print(f"  GGBET     : {a['ggbet_odd']}")
    print(f"  Moy. autres ({a['n_bookmakers']} books) : {a['avg_other']}")
    print(f"  Écart     : {a['diff_pct']}%")
    if a["other_odds"]:
        sorted_books = sorted(a["other_odds"].items(), key=lambda x: str(x[1]))
        books_str = " | ".join(f"{k}: {v}" for k, v in sorted_books[:8])
        print(f"  Détail    : {books_str}")


# =============================================================================
# Orchestration principale
# =============================================================================

def run(target_odd: float = 1.70, threshold: float = 0.10):
    session = OddsPortalSession()
    scraper = TennisScraper(session)

    # 1. Récupération des matchs du jour
    matches = scraper.get_today_matches()

    if not matches:
        print("\nAucun match trouvé. OddsPortal est peut-être indisponible ou rate-limited.")
        print("Conseil : relancez le script dans quelques minutes.")
        return

    # 2. Cotes pour chaque match
    print(f"\n>>> Récupération des cotes pour {len(matches)} matchs...")
    matches_with_odds = []

    for i, match in enumerate(matches, 1):
        name = match.get("home") and match.get("away") \
            and f"{match['home']} vs {match['away']}" \
            or match.get("slug", "?").split("/")[-2]

        print(f"  [{i}/{len(matches)}] {name}")
        odds = scraper.get_match_odds(match)

        matches_with_odds.append(
            {
                "name": name,
                "url": match["url"],
                "tournament": match.get("tournament", ""),
                "odds": odds,
            }
        )

        if not odds:
            print(f"    [SKIP] Cotes non disponibles pour ce match")

    # 3. Détection des anomalies
    print("\n>>> Analyse des anomalies...")
    anomalies = find_ggbet_anomalies(matches_with_odds, target_odd, threshold)

    # 4. Rapport
    print_report(matches_with_odds, anomalies, target_odd, threshold)

    # 5. Export JSON
    output = {
        "date": datetime.now().isoformat(),
        "target_odd": target_odd,
        "threshold_pct": threshold * 100,
        "total_matches": len(matches),
        "matches_with_odds": len([m for m in matches_with_odds if m["odds"]]),
        "anomalies": anomalies,
        "matches": [
            {k: v for k, v in m.items() if k != "odds"}
            for m in matches_with_odds
        ],
    }
    out_file = f"anomalies_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\nRésultats exportés : {out_file}")


# =============================================================================

def run_demo(target_odd: float = 1.70, threshold: float = 0.10):
    """Mode démo : simule des données pour tester la logique d'analyse."""
    print("\n[MODE DEMO] Simulation de données OddsPortal...")

    # Données simulées représentant des matchs tennis réels du jour
    fake_matches = [
        {
            "name": "Carlos Alcaraz vs Tommy Paul",
            "url": BASE_URL + "/tennis/usa/atp-miami/carlos-alcaraz-tommy-paul/",
            "tournament": "ATP Miami",
            "odds": {
                "Bet365": [1.30, 3.50],
                "Pinnacle": [1.29, 3.55],
                "Unibet": [1.31, 3.45],
                "1xBet": [1.33, 3.40],
                "William Hill": [1.30, 3.50],
                "GG.bet": [1.70, 2.10],   # ← ANOMALIE : cote GGBET cible 1.70 vs avg ~1.31
                "Betway": [1.28, 3.60],
                "Bwin": [1.32, 3.48],
            },
        },
        {
            "name": "Jannik Sinner vs Daniil Medvedev",
            "url": BASE_URL + "/tennis/usa/atp-miami/jannik-sinner-daniil-medvedev/",
            "tournament": "ATP Miami",
            "odds": {
                "Bet365": [1.55, 2.40],
                "Pinnacle": [1.57, 2.38],
                "Unibet": [1.54, 2.42],
                "1xBet": [1.56, 2.39],
                "GG.bet": [1.70, 2.20],   # ← ANOMALIE : cote 1.70 vs avg ~1.55
                "William Hill": [1.55, 2.41],
                "Bwin": [1.53, 2.44],
            },
        },
        {
            "name": "Aryna Sabalenka vs Coco Gauff",
            "url": BASE_URL + "/tennis/usa/wta-miami/aryna-sabalenka-coco-gauff/",
            "tournament": "WTA Miami",
            "odds": {
                "Bet365": [1.68, 2.15],
                "Pinnacle": [1.70, 2.12],
                "Unibet": [1.67, 2.17],
                "1xBet": [1.71, 2.11],
                "GG.bet": [1.70, 2.13],   # ← GGBET à 1.70 mais dans la moyenne (pas d'anomalie)
                "William Hill": [1.69, 2.14],
                "Betway": [1.68, 2.16],
            },
        },
        {
            "name": "Novak Djokovic vs Francisco Cerundolo",
            "url": BASE_URL + "/tennis/usa/atp-miami/novak-djokovic-francisco-cerundolo/",
            "tournament": "ATP Miami",
            "odds": {
                "Bet365": [1.25, 4.00],
                "Pinnacle": [1.24, 4.10],
                "Unibet": [1.26, 3.95],
                "1xBet": [1.27, 3.90],
                "GG.bet": [1.45, 3.50],   # ← ANOMALIE : cote anormale vs moyenne ~1.25
                "William Hill": [1.25, 4.00],
                "Bwin": [1.24, 4.05],
            },
        },
        {
            "name": "Elena Rybakina vs Mirra Andreeva",
            "url": BASE_URL + "/tennis/usa/wta-miami/elena-rybakina-mirra-andreeva/",
            "tournament": "WTA Miami",
            "odds": {
                "Bet365": [1.40, 2.85],
                "Pinnacle": [1.42, 2.80],
                "Unibet": [1.39, 2.88],
                "1xBet": [1.41, 2.83],
                "GG.bet": [1.38, 2.90],   # pas d'anomalie significative
                "William Hill": [1.40, 2.86],
            },
        },
    ]

    anomalies = find_ggbet_anomalies(fake_matches, target_odd, threshold)
    print_report(fake_matches, anomalies, target_odd, threshold)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Détecte les anomalies de cotes tennis sur OddsPortal (focus GGBET)"
    )
    parser.add_argument(
        "--target-odd",
        type=float,
        default=1.70,
        help="Cote GGBET cible à surveiller (défaut: 1.70)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.10,
        help="Seuil d'anomalie en fraction (défaut: 0.10 = 10%%)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Mode démo : utilise des données simulées (pas de scraping réel)",
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=0,
        help="Attente initiale en secondes avant de démarrer (utile si rate-limited)",
    )
    args = parser.parse_args()

    if args.demo:
        run_demo(target_odd=args.target_odd, threshold=args.threshold)
    else:
        if args.wait > 0:
            print(f"Attente initiale de {args.wait}s (--wait)...")
            time.sleep(args.wait)
        run(target_odd=args.target_odd, threshold=args.threshold)
