#!/usr/bin/env python3
"""
GOAT Machine score builder.

Reads the HH80 pool plus every data source and emits ONE file,
data/goat-machine-scores.json: for each of the 80 pool players, the 17
category scores already normalized 0-100 (pool leader = 100), plus name,
headshot URL and HH80 rank. The page does only weight math client-side.

Re-run whenever the HH80 / Defensive 79 / Peak GOATs lists or the
nba-player-data sources update:

    python scripts/build-goat-scores.py

Sources fetched from the public nba-player-data repo (falls back to a
local clone at ../nba-player-data if offline). Everything else is read
from this repo: data/pool.json, data/defense79.json, data/peakgoats.json,
data/era-difficulty.json, data/offcourt.json, data/aba.json, and the GOAT
picks payload embedded in index.html.
"""

import json, os, re, sys, urllib.request
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = "https://raw.githubusercontent.com/jsierrahoopshype/nba-player-data/main/"
LOCAL_FALLBACK = os.path.join(ROOT, "..", "nba-player-data")

# ---- name alias map: pool name -> name used in nba-player-data sources ----
ALIASES = {
    "Nate Archibald": "Tiny Archibald",
}
# headshot slug overrides where player-headshots.json has no entry
HEADSHOT_OVERRIDES = {
    "Nate Archibald": "76054-nate-archibald",
    # These two have no file in nba-headshots yet; the slugs below are what the
    # ESPN backfill script writes, so they light up as soon as that repo updates
    # (until then the page shows an initials fallback).
    "Jason Kidd": "467-jason-kidd",
    "Alex English": "76673-alex-english",
}

CATEGORY_ORDER = [
    "careerAverages", "accumulatedStats", "efficiency", "peak",
    "playoffPerformance", "finalsPerformance", "teamSuccess",
    "accolades", "longevity", "durability", "defense",
    "loyalty", "eraDifficulty", "peersRespect",
    "offCourtImpact", "fibaSuccess", "abaCredit",
]


def load_source(fname):
    local = os.path.join(LOCAL_FALLBACK, fname)
    try:
        with urllib.request.urlopen(RAW + fname, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        if os.path.exists(local):
            print(f"  [offline] using local copy for {fname}")
            return json.load(open(local))
        raise SystemExit(f"Cannot load {fname}: {e}")


def load_repo(fname):
    return json.load(open(os.path.join(ROOT, "data", fname)))


def num(v):
    """Blank strings in pre-modern rows mean 'not recorded' -> None."""
    if v is None or str(v).strip() == "":
        return None
    return float(v)


def norm_leader(values):
    """Normalization rule for every category: proportional to the pool
    leader. Leader = 100, everyone else = (value / leader) * 100, one
    decimal. No rank-based spacing."""
    leader = max(values.values()) if values else 0
    if leader <= 0:
        return {k: 0.0 for k in values}
    return {k: round(v / leader * 100, 1) for k, v in values.items()}


def main():
    print("Loading repo data...")
    pool = load_repo("pool.json")
    pool_names = [p["player"] for p in pool]
    hh80_rank = {p["player"]: p["rank"] for p in pool}
    d79 = load_repo("defense79.json")
    peak_list = load_repo("peakgoats.json")
    era = {int(k): v for k, v in load_repo("era-difficulty.json").items()}
    offcourt = load_repo("offcourt.json")
    peers_curated = load_repo("peers.json")
    aba = load_repo("aba.json")
    estimates = load_repo("estimated-stats.json")   # per-game STL/BLK for untracked seasons
    advanced = load_repo("advanced-metrics.json")   # career PER / WS48 / BPM (bbref)

    # GOAT picks payload lives inside index.html (the GOAT Debate page)
    html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    m = re.search(r'<script id="payload" type="application/json">(.*?)</script>', html, re.S)
    picks = json.loads(m.group(1))["records"]

    print("Loading nba-player-data sources...")
    rs = load_source("rsStats.json")
    po = load_source("poStats.json")
    awards = load_source("awards.json")
    votes = load_source("awardVotes.json")
    headshots = load_source("player-headshots.json")

    def src_name(p):
        return ALIASES.get(p, p)

    # ---- per-player source aggregation --------------------------------
    rs_rows = defaultdict(list)
    for r in rs:
        rs_rows[r["PLAYER"]].append(r)
    po_rows = defaultdict(list)
    for r in po:
        po_rows[r["PLAYER"]].append(r)
    aw_rows = defaultdict(list)
    for r in awards:
        aw_rows[r["PLAYER / COACH"]].append(r)
    vote_rows = defaultdict(list)
    for r in votes:
        vote_rows[r["PLAYER"]].append(r)

    # mismatch log ------------------------------------------------------
    print("\n--- name mismatch log (pool player -> source coverage) ---")
    misses = []
    for p in pool_names:
        s = src_name(p)
        gaps = []
        if s not in rs_rows: gaps.append("rsStats")
        if s not in po_rows: gaps.append("poStats")
        if s not in aw_rows: gaps.append("awards")
        if s not in vote_rows: gaps.append("awardVotes")
        if p not in headshots and s not in headshots and p not in HEADSHOT_OVERRIDES:
            gaps.append("headshots")
        if gaps:
            misses.append((p, gaps))
            print(f"  {p}: missing in {', '.join(gaps)}")
    if not misses:
        print("  none - every pool player matched in every source")

    # season max GP per year (self-adjusts for 1999/2012/2020/2021 and
    # the short early seasons) -----------------------------------------
    season_max_gp = defaultdict(float)
    season_totals = defaultdict(lambda: defaultdict(float))  # year -> player -> gp (for multi-stint sums)
    for r in rs:
        y = int(r["YEAR"])
        season_totals[y][r["PLAYER"]] += num(r["GP"]) or 0
    for y, players in season_totals.items():
        season_max_gp[y] = max(players.values())

    # helper: career aggregation over rs/po rows ------------------------
    def agg(rows, est=None):
        t = defaultdict(float)
        gp_all = 0.0
        gp_stl = gp_blk = 0.0  # GP only in seasons where the stat was recorded
        stl = blk = None
        for r in rows:
            gp = num(r["GP"]) or 0
            gp_all += gp
            for k in ("PTS", "REB", "AST", "FGA", "FTA", "MIN"):
                v = num(r[k])
                if v is not None:
                    t[k] += v
            s, b = num(r["STL"]), num(r["BLK"])
            # untracked season + estimate available -> per-game est x GP
            if s is None and est and est.get("stl") is not None:
                s = est["stl"] * gp
            if b is None and est and est.get("blk") is not None:
                b = est["blk"] * gp
            if s is not None:
                stl = (stl or 0) + s
                gp_stl += gp
            if b is not None:
                blk = (blk or 0) + b
                gp_blk += gp
        return t, gp_all, stl, gp_stl, blk, gp_blk

    # composite of per-game PTS/REB/AST/STL/BLK, category-1 weights.
    # Players with no recorded STL/BLK at all (pre-1974 careers) get those
    # weights redistributed across PTS/REB/AST instead of scoring zeros.
    def composite(values_per_player):
        # values_per_player: name -> dict stat -> per-game value or None
        W = {"PTS": .35, "REB": .20, "AST": .20, "STL": .125, "BLK": .125}
        normed = {}
        for stat in W:
            vals = {n: v[stat] for n, v in values_per_player.items() if v[stat] is not None}
            normed[stat] = norm_leader(vals)
        out = {}
        for n, v in values_per_player.items():
            score, wsum = 0.0, 0.0
            for stat, w in W.items():
                if v[stat] is not None:
                    score += w * normed[stat].get(n, 0)
                    wsum += w
            out[n] = score / wsum if wsum else 0.0  # redistribution = renormalize weights
        return out

    # ---- gather per-player raw material -------------------------------
    P = {}
    for p in pool_names:
        s = src_name(p)
        rrows = rs_rows.get(s, [])
        prow = po_rows.get(s, [])
        est = estimates.get(p)
        t, gp, stl, gp_stl, blk, gp_blk = agg(rrows, est)
        # playoff samples are shakier: estimates count at half strength there
        po_est = ({k: v * 0.5 for k, v in est.items() if isinstance(v, (int, float))}
                  if est else None)
        pt, pgp, pstl, pgp_stl, pblk, pgp_blk = agg(prow, po_est)
        P[p] = dict(rs_t=t, rs_gp=gp, rs_stl=stl, rs_gp_stl=gp_stl, rs_blk=blk, rs_gp_blk=gp_blk,
                    po_t=pt, po_gp=pgp, po_stl=pstl, po_gp_stl=pgp_stl, po_blk=pblk, po_gp_blk=pgp_blk,
                    rs_rows=rrows, po_rows=prow,
                    awards=aw_rows.get(s, []), votes=vote_rows.get(s, []))

    scores = {p: {} for p in pool_names}

    # 1. Career averages ------------------------------------------------
    def pergame(d, which):
        t, gp = d[f"{which}_t"], d[f"{which}_gp"]
        out = {"PTS": t["PTS"] / gp if gp else None,
               "REB": t["REB"] / gp if gp else None,
               "AST": t["AST"] / gp if gp else None}
        out["STL"] = (d[f"{which}_stl"] / d[f"{which}_gp_stl"]) if d[f"{which}_stl"] is not None and d[f"{which}_gp_stl"] else None
        out["BLK"] = (d[f"{which}_blk"] / d[f"{which}_gp_blk"]) if d[f"{which}_blk"] is not None and d[f"{which}_gp_blk"] else None
        return out

    # ppg + rpg + apg + spg + bpg (estimates fill untracked years), nudged
    # A BIT by career TS%: factor runs 0.93 (pool-worst shooter) to 1.07
    # (pool-best), linear in between.
    ts_all = {}
    for p, d in P.items():
        t = d["rs_t"]
        denom = 2 * (t["FGA"] + 0.44 * t["FTA"])
        ts_all[p] = t["PTS"] / denom if denom else 0
    ts_lo, ts_hi = min(ts_all.values()), max(ts_all.values())
    def ts_factor(p):
        return 0.93 + 0.14 * (ts_all[p] - ts_lo) / (ts_hi - ts_lo)
    def pg_sum(d, which):
        v = pergame(d, which)
        return sum(x for x in v.values() if x is not None)
    cat1 = {p: pg_sum(d, "rs") * ts_factor(p) for p, d in P.items()}
    for p, v in norm_leader(cat1).items():
        scores[p]["careerAverages"] = v

    # 2. Accumulated stats: career PTS + REB + AST + STL + BLK, one raw
    # sum (steals/blocks for untracked seasons come from the estimates file)
    def totsum(d, which):
        t = d[f"{which}_t"]
        return ((t["PTS"] or 0) + (t["REB"] or 0) + (t["AST"] or 0)
                + (d[f"{which}_stl"] or 0) + (d[f"{which}_blk"] or 0))

    cat2 = {p: totsum(d, "rs") for p, d in P.items()}
    for p, v in norm_leader(cat2).items():
        scores[p]["accumulatedStats"] = v

    # 3. Efficiency: equal-weight blend of career PER, WS/48, BPM
    # (basketball-reference, data/advanced-metrics.json) and career TS%
    # computed here. Each metric normalized pool-leader = 100; players with
    # no career BPM (pre-1974 careers) are averaged over the other three.
    ts = {}
    for p, d in P.items():
        t = d["rs_t"]
        denom = 2 * (t["FGA"] + 0.44 * t["FTA"])
        ts[p] = t["PTS"] / denom if denom else 0
    ts_n = norm_leader(ts)
    adv_n = {}
    for metric in ("per", "ws48", "bpm"):
        vals = {p: advanced[p][metric] for p in pool_names
                if advanced.get(p, {}).get(metric) is not None}
        adv_n[metric] = norm_leader({p: max(v, 0) for p, v in vals.items()})
    eff = {}
    for p in pool_names:
        parts = [ts_n[p]] + [adv_n[m][p] for m in ("per", "ws48", "bpm") if p in adv_n[m]]
        eff[p] = sum(parts) / len(parts)
    for p, v in norm_leader(eff).items():
        scores[p]["efficiency"] = v

    # 4. Peak -----------------------------------------------------------
    # Primary: Peak GOATs list rank, linear (No. 1 = 100). Fallback for
    # pool players not on the list: best 5-consecutive-season per-game
    # composite + MVP-vote bonus, scaled to sit strictly below the
    # lowest-ranked list member so the list stays authoritative.
    N = len(peak_list)
    peak_val = {e["player"]: (N + 1 - e["rank"]) / N * 100 for e in peak_list}
    floor = min(peak_val.values())

    # per-season composite pool for the fallback: every pool player's
    # individual seasons, normalized within that season pool
    season_stats = defaultdict(dict)  # player -> year -> per-game dict
    for p, d in P.items():
        per_year = defaultdict(lambda: defaultdict(float))
        gp_y = defaultdict(float)
        for r in d["rs_rows"]:
            y = int(r["YEAR"])
            gp_y[y] += num(r["GP"]) or 0
            for k in ("PTS", "REB", "AST"):
                per_year[y][k] += num(r[k]) or 0
            for k in ("STL", "BLK"):
                v = num(r[k])
                if v is not None:
                    per_year[y][k] += v
                    per_year[y][k + "_rec"] = 1
        for y, tt in per_year.items():
            g = gp_y[y] or 1
            season_stats[p][y] = {
                "PTS": tt["PTS"] / g, "REB": tt["REB"] / g, "AST": tt["AST"] / g,
                "STL": tt["STL"] / g if tt.get("STL_rec") else None,
                "BLK": tt["BLK"] / g if tt.get("BLK_rec") else None,
            }
    all_seasons = {f"{p}|{y}": st for p, ys in season_stats.items() for y, st in ys.items()}
    season_comp = composite(all_seasons)

    mvp_rank = defaultdict(dict)  # player -> year -> MVP vote rank
    for p, d in P.items():
        for v in d["votes"]:
            if v["AWARD"] == "MVP":
                mvp_rank[p][int(v["YEAR"])] = int(v["RNK"])

    fallback_raw = {}
    for p in pool_names:
        if p in peak_val:
            continue
        years = sorted(season_stats[p])
        best = 0.0
        for i in range(len(years)):
            span = [y for y in years if years[i] <= y < years[i] + 5]  # 5 consecutive calendar seasons
            comp = sum(season_comp[f"{p}|{y}"] for y in span) / len(span)
            bonus = 0.0
            for y in span:
                r = mvp_rank[p].get(y)
                if r:
                    bonus += max(0, 11 - r)  # MVP-vote finish bonus: 1st=10 ... 10th=1
            best = max(best, comp + bonus)
        fallback_raw[p] = best
    fmax = max(fallback_raw.values()) if fallback_raw else 1
    for p, v in fallback_raw.items():
        peak_val[p] = v / fmax * floor * 0.95  # strictly below the last list member
    for p, v in norm_leader(peak_val).items():
        scores[p]["peak"] = v

    # 5. Playoff performance -------------------------------------------
    # 45% per-game playoff production (weighted composite, nudged by
    # playoff TS%: 0.93x-1.07x) + 30% accumulated playoff production
    # (career playoff PTS+REB+AST+STL+BLK) + 25% playoff team success
    # (title 4 / Finals loss 2 / conf-finals loss 1, one per season).
    # Old-era steal/block estimates count at half strength in playoffs.
    po_comp = composite({p: pergame(d, "po") for p, d in P.items()})
    po_comp_n = norm_leader(po_comp)
    po_ts = {}
    for p, d in P.items():
        t = d["po_t"]
        denom = 2 * (t["FGA"] + 0.44 * t["FTA"])
        po_ts[p] = t["PTS"] / denom if denom else 0
    pts_vals = [v for v in po_ts.values() if v > 0]
    plo, phi = min(pts_vals), max(pts_vals)
    def po_ts_factor(p):
        return 0.93 + 0.14 * (max(po_ts[p], plo) - plo) / (phi - plo)
    comp_adj = norm_leader({p: po_comp_n[p] * po_ts_factor(p) for p in pool_names})
    po_acc = norm_leader({p: (d["po_t"]["PTS"] or 0) + (d["po_t"]["REB"] or 0)
                          + (d["po_t"]["AST"] or 0) + (d["po_stl"] or 0)
                          + (d["po_blk"] or 0) for p, d in P.items()})
    PO_SEASON = {"Champion": 4, "Finalist": 2, "Conf Finalist": 1}
    po_team = norm_leader({p: sum(PO_SEASON.get(r["RESULT"], 0) for r in d["po_rows"])
                           for p, d in P.items()})
    blend = {p: 0.45 * comp_adj[p] + 0.30 * po_acc[p] + 0.25 * po_team[p] for p in pool_names}
    for p, v in norm_leader(blend).items():
        scores[p]["playoffPerformance"] = v

    # 6. Finals performance --------------------------------------------
    # Finals MVPs x5 + championships x2 + Finals appearances x1.
    # v2 upgrade: real Finals-series stat lines from Basketball-Reference
    # (per-game production in the Finals themselves). Not built now.
    fin = {}
    for p, d in P.items():
        fmvp = sum(1 for a in d["awards"] if a["AWARD"] == "Finals MVP")
        chips = sum(1 for r in d["po_rows"] if r["RESULT"] == "Champion")
        apps = sum(1 for r in d["po_rows"] if r["RESULT"] in ("Finalist", "Champion"))
        fin[p] = fmvp * 5 + chips * 2 + apps * 1
    for p, v in norm_leader(fin).items():
        scores[p]["finalsPerformance"] = v

    # 7. Team success in the NBA (deliberate overlap with 6) -----------
    # One value per season, no double dipping: a title is 4 points and
    # that's all that season gives; losing the Finals 2; losing the
    # conference finals 1. Summed over the career. Calibration: Bill
    # Russell first, Celtics dynasty players (Havlicek) right behind.
    SEASON_PTS = {"Champion": 4, "Finalist": 2, "Conf Finalist": 1}
    team = {}
    for p, d in P.items():
        team[p] = sum(SEASON_PTS.get(r["RESULT"], 0) for r in d["po_rows"])
    for p, v in norm_leader(team).items():
        scores[p]["teamSuccess"] = v

    # 8. Accolades ------------------------------------------------------
    # Fixed points per award, importance order set editorially:
    # MVP > Finals MVP > All-NBA 1st > 2nd > 3rd > All-Star > DPOY >
    # All-Defensive 1st > 2nd.
    ACC_PTS = {
        "Most Valuable Player": 10,
        "Finals MVP": 8,
        "All-NBA First Team": 6,
        "All-NBA Second Team": 4,
        "All-NBA Third Team": 3,
        "All-Star": 2,
        "Defensive Player of the Year": 1.5,
        "All-Defensive First Team": 1,
        "All-Defensive Second Team": 0.5,
    }
    acc = {}
    for p, d in P.items():
        acc[p] = sum(ACC_PTS.get(a["AWARD"], 0) for a in d["awards"])
    for p, v in norm_leader(acc).items():
        scores[p]["accolades"] = v

    # 9. Sustained excellence: All-Star selections plus All-NBA First or
    # Second Team selections, one point each.
    LON_AWARDS = ("All-Star", "All-NBA First Team", "All-NBA Second Team")
    lon = {}
    for p, d in P.items():
        lon[p] = sum(1 for a in d["awards"] if a["AWARD"] in LON_AWARDS)
    for p, v in norm_leader(lon).items():
        scores[p]["longevity"] = v

    # 10. Durability ----------------------------------------------------
    # Career GP / career possible games; possible per season = max GP any
    # player logged that season. Only seasons the player appeared in count
    # (a fully missed season, like Jordan 1994, is not held against him).
    dur = {}
    for p, d in P.items():
        poss = played = 0.0
        for y, g in season_totals_for(d).items():
            poss += season_max_gp[y]
            played += g
        dur[p] = played / poss if poss else 0
    for p, v in norm_leader(dur).items():
        scores[p]["durability"] = v

    # 11. Defense -------------------------------------------------------
    ND = len(d79)
    dval = {e["player"]: (ND + 1 - e["rank"]) / ND * 100 for e in d79 if e["player"] in hh80_rank}
    dfloor = min((ND + 1 - e["rank"]) / ND * 100 for e in d79)  # No. 79's score
    fb_raw = {}
    stlblk = {}
    for p, d in P.items():
        s = (d["rs_stl"] or 0) + (d["rs_blk"] or 0)
        stlblk[p] = s
    stlblk_n = norm_leader(stlblk)
    for p, d in P.items():
        if p in dval:
            continue
        ad1 = sum(1 for a in d["awards"] if a["AWARD"] == "All-Defensive First Team")
        ad2 = sum(1 for a in d["awards"] if a["AWARD"] == "All-Defensive Second Team")
        dpoy = sum(1 for a in d["awards"] if a["AWARD"] == "Defensive Player of the Year")
        fb_raw[p] = ad1 * 2 + ad2 * 1 + dpoy * 3 + stlblk_n[p] / 100
    fmax = max(fb_raw.values()) if fb_raw else 1
    for p, v in fb_raw.items():
        dval[p] = (v / fmax) * dfloor * 0.95  # strictly below No. 79
    for p, v in norm_leader(dval).items():
        scores[p]["defense"] = v

    # 12. Loyalty -------------------------------------------------------
    # Share of career RS GP with the primary franchise (TEAM codes in
    # rsStats are already franchise-normalized across moves), +10 flat
    # before normalization for a true one-franchise career.
    loy = {}
    for p, d in P.items():
        by_team = defaultdict(float)
        for r in d["rs_rows"]:
            by_team[r["TEAM"]] += num(r["GP"]) or 0
        total = sum(by_team.values())
        if not total:
            loy[p] = 0
            continue
        share = max(by_team.values()) / total * 100
        if len(by_team) == 1:
            share += 10
        loy[p] = share
    for p, v in norm_leader(loy).items():
        scores[p]["loyalty"] = v

    # 13. Era difficulty ------------------------------------------------
    # Games-weighted average of era-difficulty values across the player's
    # seasons. Seasons before 1951 (era CSV start) use the 1951 value.
    first_era = min(era)
    edif = {}
    for p, d in P.items():
        w = tot = 0.0
        for y, g in season_totals_for(d).items():
            v = era.get(y, era[first_era] if y < first_era else None)
            if v is None:
                continue
            w += v * g
            tot += g
        edif[p] = w / tot if tot else 0
    for p, v in norm_leader(edif).items():
        scores[p]["eraDifficulty"] = v

    # 14. Respect from peers -------------------------------------------
    # Curated 0-100 table (data/peers.json) anchored on the archive's stated
    # GOAT picks and The Athletic's anonymous player polls, then extended
    # editorially so reverence beyond a literal GOAT pick counts too.
    # The raw stated-pick shares still feed the consensus card (pickSharePct).
    stated = [r for r in picks if r.get("pick")]
    total_picks = len(stated)
    pick_count = defaultdict(int)
    for r in stated:
        pick_count[r["pick"]] += 1
    for p, v in norm_leader({p: peers_curated[p] for p in pool_names}).items():
        scores[p]["peersRespect"] = v

    # 15. Off-court impact (editorial, data/offcourt.json) --------------
    for p, v in norm_leader({p: offcourt[p] for p in pool_names}).items():
        scores[p]["offCourtImpact"] = v

    # 16. FIBA success --------------------------------------------------
    # Senior NT results at Olympics + World Cup ONLY. Gold 3, silver 2,
    # bronze 1, tournament MVP +4. Note: "WC Finals MVP"/"EC Finals MVP"
    # in awards.json are CONFERENCE finals MVPs, not World Cup - excluded.
    FIBA_PTS = {"Olympic Gold": 3, "Olympic Silver": 2, "Olympic Bronze": 1,
                "World Cup Gold": 3, "World Cup Silver": 2, "World Cup Bronze": 1,
                "Olympic MVP": 4, "World Cup MVP": 4}
    fiba = {}
    for p, d in P.items():
        fiba[p] = sum(FIBA_PTS.get(a["AWARD"], 0) for a in d["awards"])
    for p, v in norm_leader(fiba).items():
        scores[p]["fibaSuccess"] = v

    # 17. ABA credit (hand-curated data/aba.json) -----------------------
    aba_pts = {p: 0 for p in pool_names}
    for p, d in aba.items():
        aba_pts[p] = d["titles"] * 3 + d["mvps"] * 3 + d["allstars"] * 1 + d["scoring"] * 1
    for p, v in norm_leader(aba_pts).items():
        scores[p]["abaCredit"] = v

    # ---- headshots ----------------------------------------------------
    BASE = "https://jsierrahoopshype.github.io/nba-headshots/players/headshots/"
    headshot_miss = []
    out_players = []
    for p in pool_names:
        slug = HEADSHOT_OVERRIDES.get(p) or headshots.get(p) or headshots.get(src_name(p))
        if not slug:
            headshot_miss.append(p)
        out_players.append({
            "name": p,
            "hh80Rank": hh80_rank[p],
            "headshot": slug,  # page builds face2-160 webp URL + face2 png fallback
            "pickSharePct": round(pick_count.get(p, 0) / total_picks * 100, 1),
            "scores": {k: scores[p][k] for k in CATEGORY_ORDER},
        })

    out = {
        "generated": __import__("datetime").date.today().isoformat(),
        "categoryOrder": CATEGORY_ORDER,
        "headshotBase": BASE,
        "totalStatedPicks": total_picks,
        "players": out_players,
    }
    with open(os.path.join(ROOT, "data", "goat-machine-scores.json"), "w") as f:
        json.dump(out, f, indent=1)

    # ---- reports ------------------------------------------------------
    print("\n--- headshot misses (no file in nba-headshots) ---")
    print(" ", headshot_miss if headshot_miss else "none")

    print("\n--- FIBA calibration check (must be KD, then LeBron, then Pau) ---")
    for n in sorted(fiba, key=fiba.get, reverse=True)[:6]:
        print(f"  {fiba[n]:5.1f}  {n}")

    print("\n--- sanity: all-50s Top 10 ---")
    def total_at_50(pl):
        return sum(pl["scores"].values())
    for pl in sorted(out_players, key=total_at_50, reverse=True)[:10]:
        print(f"  {total_at_50(pl)/17:6.1f}  {pl['name']}")

    nan = [p["name"] for p in out_players if any(v != v or v is None for v in p["scores"].values())]
    print("\nNaN check:", nan if nan else "clean")
    print("\nWrote data/goat-machine-scores.json for", len(out_players), "players.")


def season_totals_for(d):
    """player row list -> {year: GP summed across stints}"""
    out = defaultdict(float)
    for r in d["rs_rows"]:
        out[int(r["YEAR"])] += float(r["GP"] or 0)
    return out


if __name__ == "__main__":
    main()
