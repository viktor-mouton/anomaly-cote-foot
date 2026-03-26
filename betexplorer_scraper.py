#!/usr/bin/env python3
"""
Détecteur d'anomalies de cotes Tennis - Betexplorer.com
========================================================
Scrape betexplorer.com pour trouver des cotes anormales
sur les matchs de tennis du jour (comparaison multi-bookmakers).

Usage:
    python3 betexplorer_scraper.py
    python3 betexplorer_scraper.py --threshold 0.08
    python3 betexplorer_scraper.py --max-matches 50
"""

import re
import time
import json
import random
import argparse
from datetime import datetime
from typing import Optional
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

BASE_URL = "https://www.betexplorer.com"
TODAY = datetime.now().strftime("%Y-%m-%d")


class BetexplorerSession:
    def __init__(self):
        self.session = cffi_requests.Session(impersonate="chrome120")
        self.session.headers.update({
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
        })
        self.request_count = 0

    def get(self, url: str, is_ajax: bool = False, **kwargs) -> Optional[cffi_requests.Response]:
        """GET avec gestion des erreurs et délai poli."""
        if self.request_count > 0:
            time.sleep(random.uniform(1.5, 3.0))

        headers = {}
        if is_ajax:
            headers = {
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": BASE_URL + "/tennis/",
            }

        try:
            r = self.session.get(url, headers=headers, timeout=20, verify=False, **kwargs)
            self.request_count += 1
            return r
        except Exception as e:
            print(f"    Erreur: {e}")
            return None


def get_today_tennis_matches(session: BetexplorerSession) -> list[dict]:
    """Récupère les matchs tennis du jour depuis betexplorer."""
    print(f">>> Récupération des matchs tennis du {TODAY}...")

    r = session.get(f"{BASE_URL}/tennis/")
    if not r or r.status_code != 200:
        print(f"    Erreur: {r.status_code if r else 'timeout'}")
        return []

    soup = BeautifulSoup(r.text, "lxml")
    matches = []
    seen = set()

    # Chercher tous les liens de matchs tennis
    # Format betexplorer: /tennis/{competition}/{tournament}/{player1-player2}/{matchId}/
    for link in soup.find_all("a", href=re.compile(r"^/tennis/[^/]+/[^/]+/[^/]+/[A-Za-z0-9]{8}/$")):
        href = link["href"]
        parts = [p for p in href.split("/") if p]

        if len(parts) < 5 or parts[0] != "tennis":
            continue

        match_id = parts[-1]
        if match_id in seen:
            continue
        seen.add(match_id)

        competition = parts[1]   # ex: "atp-singles"
        tournament = parts[2]    # ex: "miami"
        slug = parts[3]          # ex: "alcaraz-draper"
        name = link.get_text(strip=True)

        # Chercher le score adjacent (si présent = match terminé)
        parent = link.find_parent("tr") or link.find_parent("td") or link.find_parent("div")
        score_text = ""
        if parent:
            score_match = re.search(r"\b(\d+:\d+)\b", parent.get_text())
            if score_match:
                score_text = score_match.group(1)

        # Chercher l'heure du match
        time_el = (parent or link).find_parent(
            lambda t: t.get("data-dt") or t.get("data-time")
        ) if parent else None

        matches.append({
            "id": match_id,
            "name": name or slug.replace("-", " "),
            "competition": competition,
            "tournament": tournament,
            "slug": slug,
            "url": BASE_URL + href,
            "score": score_text,
            "is_live_or_finished": bool(score_text),
        })

    print(f"    {len(matches)} matchs trouvés au total")

    # Filtrer les matchs upcoming (sans score) = ceux d'aujourd'hui/demain
    upcoming = [m for m in matches if not m["is_live_or_finished"]]
    print(f"    {len(upcoming)} matchs à venir (sans score)")

    return upcoming


def get_match_odds(session: BetexplorerSession, match: dict) -> Optional[dict]:
    """
    Récupère les cotes pour un match depuis betexplorer.
    Retourne {bookmaker: [odds]} ou None.
    """
    match_id = match["id"]

    # Endpoint principal: /match-odds-old/{matchId}/0/ha/1/en/
    # ha = Home/Away (tennis: joueur 1 / joueur 2)
    url = f"{BASE_URL}/match-odds-old/{match_id}/0/ha/1/en/"

    r = session.get(url, is_ajax=True)
    if not r or r.status_code != 200:
        return None

    try:
        data = r.json()
        html_odds = data.get("odds", "")
    except Exception:
        return None

    if not html_odds:
        return None

    return _parse_odds_html(html_odds, match_id)


def _parse_odds_html(html: str, match_id: str) -> Optional[dict]:
    """Parse le HTML de la réponse d'odds betexplorer."""
    soup = BeautifulSoup(html, "lxml")
    bookmaker_odds = {}

    rows = soup.find_all("tr", attrs={"data-bid": True})
    for row in rows:
        # Nom du bookmaker dans la première cellule visible
        name_cell = row.find("td", class_=lambda c: c and "h-text-left" in c)
        if not name_cell:
            continue
        bookie_name = name_cell.get_text(strip=True)
        if not bookie_name or bookie_name in ("", "Average odds", "Opening odds", "Add to My Selections"):
            continue
        bookie_name = re.sub(r"\s+", " ", bookie_name).strip()

        # Les cotes sont dans les attributs data-odd des td d'odds
        odds_cells = row.find_all("td", class_=lambda c: c and "table-main__detail-odds" in c)
        odds = []
        for cell in odds_cells:
            val = cell.get("data-odd")
            if val:
                try:
                    odds.append(float(val))
                except ValueError:
                    odds.append(None)
            else:
                odds.append(None)

        if odds and any(o is not None for o in odds):
            bookmaker_odds[bookie_name] = odds

    return bookmaker_odds if bookmaker_odds else None


def find_anomalies(matches_with_odds: list[dict], threshold: float = 0.08) -> list[dict]:
    """
    Détecte les anomalies : bookmaker avec une cote qui s'écarte
    de >= threshold (8%) de la moyenne des autres bookmakers.
    """
    anomalies = []

    for match in matches_with_odds:
        name = match["name"]
        odds_by_bookie = match.get("odds", {})

        if not odds_by_bookie or len(odds_by_bookie) < 3:
            continue

        # Pour chaque résultat possible (joueur 1, joueur 2)
        max_outcomes = max(len(v) for v in odds_by_bookie.values() if v)

        for outcome_idx in range(min(max_outcomes, 2)):
            # Collecter les cotes disponibles pour ce résultat
            bookie_values = {}
            for bookie, odds_list in odds_by_bookie.items():
                if isinstance(odds_list, list) and len(odds_list) > outcome_idx:
                    v = odds_list[outcome_idx]
                    if v is not None and isinstance(v, float) and v > 1.0:
                        bookie_values[bookie] = v

            if len(bookie_values) < 3:
                continue

            all_values = list(bookie_values.values())
            avg_all = sum(all_values) / len(all_values)

            for bookie, odd_val in bookie_values.items():
                # Calculer la moyenne sans ce bookmaker
                others = [v for b, v in bookie_values.items() if b != bookie]
                if not others:
                    continue
                avg_others = sum(others) / len(others)
                if avg_others == 0:
                    continue

                diff_pct = abs(odd_val - avg_others) / avg_others

                if diff_pct >= threshold:
                    anomalies.append({
                        "match": name,
                        "competition": match.get("competition", ""),
                        "tournament": match.get("tournament", ""),
                        "url": match.get("url", ""),
                        "outcome_idx": outcome_idx,
                        "outcome_label": f"Joueur {outcome_idx + 1}",
                        "bookmaker": bookie,
                        "odd_value": odd_val,
                        "avg_others": round(avg_others, 3),
                        "diff_pct": round(diff_pct * 100, 1),
                        "n_bookmakers": len(bookie_values),
                        "all_odds": dict(sorted(bookie_values.items(), key=lambda x: x[1])),
                    })

    # Trier par écart décroissant
    return sorted(anomalies, key=lambda x: x["diff_pct"], reverse=True)


def print_report(matches_with_odds: list[dict], anomalies: list[dict], threshold: float):
    """Affiche le rapport d'anomalies."""
    date_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    print("\n" + "=" * 72)
    print(f"  ANOMALIES DE COTES TENNIS - {date_str}")
    print(f"  Seuil : {threshold * 100:.0f}% d'écart vs moyenne autres bookmakers")
    print("=" * 72)

    total = len(matches_with_odds)
    with_odds = sum(1 for m in matches_with_odds if m.get("odds"))
    print(f"\nMatchs analysés     : {total}")
    print(f"Avec cotes          : {with_odds}")
    print(f"Anomalies détectées : {len(anomalies)}")

    if not anomalies:
        print("\n  Aucune anomalie détectée.")
        print("=" * 72)
        return

    # Grouper par bookmaker
    by_bookie = {}
    for a in anomalies:
        bk = a["bookmaker"]
        if bk not in by_bookie:
            by_bookie[bk] = []
        by_bookie[bk].append(a)

    print(f"\nBookmakers impliqués : {', '.join(sorted(by_bookie.keys()))}")

    # Afficher les top anomalies
    print(f"\n{'─' * 72}")
    print(f"  TOP ANOMALIES (écart le plus élevé en premier) :")
    print(f"{'─' * 72}")

    for a in anomalies[:25]:
        direction = "↑ SURCOTÉE" if a["odd_value"] > a["avg_others"] else "↓ SOUS-COTÉE"
        print(f"\n  [{a['diff_pct']}%] {direction}")
        print(f"  Match      : {a['match']}")
        print(f"  Compétition: {a['competition']} / {a['tournament']}")
        print(f"  Résultat   : {a['outcome_label']}")
        print(f"  Bookmaker  : {a['bookmaker']} → {a['odd_value']}")
        print(f"  Moy. autres ({a['n_bookmakers']-1} books) : {a['avg_others']}")
        top8 = dict(list(a["all_odds"].items())[:8])
        odds_str = " | ".join(f"{k}: {v}" for k, v in top8.items())
        print(f"  Cotes      : {odds_str}")
        print(f"  URL        : {a['url']}")

    print("\n" + "=" * 72)


def run(max_matches: int = 100, threshold: float = 0.08):
    session = BetexplorerSession()

    # 1. Matchs du jour
    matches = get_today_tennis_matches(session)
    if not matches:
        print("Aucun match trouvé.")
        return

    # Limiter le nombre de matchs à analyser
    matches = matches[:max_matches]
    print(f"\n>>> Analyse de {len(matches)} matchs (max={max_matches})...")

    # 2. Cotes pour chaque match
    matches_with_odds = []
    for i, match in enumerate(matches, 1):
        print(f"  [{i}/{len(matches)}] {match['name']} ({match['competition']})", end=" ")
        odds = get_match_odds(session, match)
        match["odds"] = odds
        if odds:
            n = sum(1 for v in odds.values() if v and any(x is not None for x in v))
            print(f"→ {n} bookmakers")
        else:
            print("→ pas de cotes")
        matches_with_odds.append(match)

    # 3. Détection anomalies
    print("\n>>> Analyse des anomalies...")
    anomalies = find_anomalies(matches_with_odds, threshold)

    # 4. Rapport
    print_report(matches_with_odds, anomalies, threshold)

    # 5. Export JSON
    out_data = {
        "date": TODAY,
        "threshold_pct": threshold * 100,
        "total_matches": len(matches),
        "matches_with_odds": sum(1 for m in matches_with_odds if m.get("odds")),
        "anomalies_count": len(anomalies),
        "anomalies": anomalies,
    }
    out_file = f"tennis_anomalies_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(out_data, f, ensure_ascii=False, indent=2)
    print(f"\nRésultats exportés : {out_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Détecte les anomalies de cotes tennis sur betexplorer.com"
    )
    parser.add_argument("--threshold", type=float, default=0.08,
                        help="Seuil d'anomalie (défaut: 0.08 = 8%%)")
    parser.add_argument("--max-matches", type=int, default=100,
                        help="Nombre max de matchs à analyser (défaut: 100)")
    args = parser.parse_args()
    run(max_matches=args.max_matches, threshold=args.threshold)
