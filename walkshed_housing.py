#!/usr/bin/env python3
"""
Walk-shed housing stock analysis for a laundromat site (Fresh Fold).

Builds a true pedestrian walk shed from NYC's street centerline, then profiles
every tax lot inside it using NYC Open Data and the U.S. Census:

  NYC Open Data (no key needed, optional SOCRATA_APP_TOKEN for speed)
    - PLUTO (64uk-42ks): every tax lot, building class, units, year built, floors, owner
    - Street Centerline / CSCL (inkn-q76z): walk network
    - HPD Buildings Subject to HPD Jurisdiction (kj4p-ruqc): legal Class A units, program
    - HPD Multiple Dwelling Registrations (tesw-yqqr): registered multiple dwellings
    - DOB Job Application Filings, BIS (ic3t-wcy2): new buildings, laundry-related jobs
    - DOB NOW Build Job Filings (w9ak-ipjd): new buildings, laundry-related jobs
    - Affordable Housing Production by Building (hg8x-zxpr): new affordable units
    - DCWP Issued Licenses (w7w3-xahh): licensed laundries
  U.S. Census API (free key required: https://api.census.gov/data/key_signup.html)
    - ACS 5-year, block group: tenure, units in structure, year built, income,
      household size, vehicles, rent, rent burden, Hispanic origin
    - 2020 Decennial DHC, census block: population, housing units, occupancy, tenure

Census figures are apportioned to the walk shed using PLUTO residential units
(a dasymetric method), not land area, so a block group that is half parkland
does not get half its people assigned to the walk shed.

Usage
    pip install pandas numpy requests shapely pyproj networkx openpyxl folium
    export CENSUS_API_KEY=your_key          # optional but recommended
    python walkshed_housing.py --address "2220 Bath Avenue" --minutes 5,10,15

    Optional: --rankings Fresh_Fold_Acquisition_Target_Rankings.xlsx
              (plots ranked competitors inside the walk shed)

Outputs (in --outdir, default ./walkshed_output)
    walkshed_summary.xlsx   all tables
    walkshed_lots.csv       one row per tax lot with walk time and every attribute
    walkshed_map.html       interactive map: walk-shed rings, lots, competitors
    walkshed_report.md      plain-language summary
"""

import argparse
import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime

import networkx as nx
import shapely
import numpy as np
import pandas as pd
import requests
from pyproj import Transformer
from shapely.geometry import LineString, MultiLineString, Point, mapping
from shapely.ops import substring, unary_union
from shapely.strtree import STRtree

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
SOCRATA = "https://data.cityofnewyork.us/resource"
CENSUS = "https://api.census.gov/data"
STATE_FIPS = "36"
BORO = {  # PLUTO borough code -> (borocode, county FIPS, DOB name, HPD boroid)
    "MN": ("1", "061", "MANHATTAN", "1"),
    "BX": ("2", "005", "BRONX", "2"),
    "BK": ("3", "047", "BROOKLYN", "3"),
    "QN": ("4", "081", "QUEENS", "4"),
    "SI": ("5", "085", "STATEN ISLAND", "5"),
}
FT_PER_M = 3.28084
TO_FT = Transformer.from_crs("EPSG:4326", "EPSG:2263", always_xy=True)   # NY State Plane LI (ft)
TO_LL = Transformer.from_crs("EPSG:2263", "EPSG:4326", always_xy=True)

# Street centerline types a pedestrian can use. rw_type: 1 street, 3 bridge, 4 tunnel,
# 5 boardwalk, 6 path/trail, 7 step street, 8 driveway, 10 alley, 13 U-turn.
# Excluded: 2 highway, 9 ramp, 11 unknown, 12 non-physical, 14 ferry.
WALKABLE_RW_TYPES = {"1", "3", "4", "5", "6", "7", "8", "10", "13"}

# Laundromat dependency: share of units in each building type that likely have
# NO in-unit washer and NO in-building laundry room, so they need a laundromat.
# These are starting assumptions, not data. Calibrate them (e.g. by surveying
# customers at 935 McDonald) and edit here. Mirrors the rankings model logic:
# small buildings and pre-1960 stock are the most laundromat dependent.
SIZE_BUCKETS = [(1, 1, "1 unit"), (2, 2, "2 units"), (3, 4, "3-4 units"), (5, 9, "5-9 units"),
                (10, 19, "10-19 units"), (20, 49, "20-49 units"), (50, 10**9, "50+ units")]
ERA_BUCKETS = [(0, 1939, "Pre-1940"), (1940, 1959, "1940-1959"), (1960, 1979, "1960-1979"),
               (1980, 1999, "1980-1999"), (2000, 2009, "2000-2009"), (2010, 2100, "2010+")]
DEPENDENCY = {  # size bucket -> (pre-1960, 1960-1999, 2000+)
    "1 unit":      (0.30, 0.20, 0.10),
    "2 units":     (0.50, 0.35, 0.15),
    "3-4 units":   (0.75, 0.55, 0.20),
    "5-9 units":   (0.80, 0.60, 0.25),
    "10-19 units": (0.70, 0.55, 0.25),
    "20-49 units": (0.55, 0.45, 0.20),
    "50+ units":   (0.40, 0.35, 0.15),
}
UNKNOWN_ERA_DEP_INDEX = 0  # treat unknown year built as pre-1960 (true for most NYC zero-year lots)

LAUNDRY_TERMS = r"LAUNDR|WASHER|WASHING MACHINE|DRYER"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


SESSION = requests.Session()
if os.getenv("SOCRATA_APP_TOKEN"):
    SESSION.headers["X-App-Token"] = os.environ["SOCRATA_APP_TOKEN"]


def http_get(url, params=None, tries=4):
    for i in range(tries):
        try:
            r = SESSION.get(url, params=params, timeout=120)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** i)
                continue
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        except requests.RequestException as e:
            if i == tries - 1:
                raise
            time.sleep(2 ** i)
    raise RuntimeError(f"Failed after {tries} tries: {url}")


def soql(dataset, where=None, select=None, order=None, page=50000, max_rows=2_000_000):
    """Fetch all rows from a Socrata dataset with paging."""
    rows, offset = [], 0
    while True:
        params = {"$limit": page, "$offset": offset}
        if where:
            params["$where"] = where
        if select:
            params["$select"] = select
        params["$order"] = order or ":id"
        batch = http_get(f"{SOCRATA}/{dataset}.json", params).json()
        rows.extend(batch)
        if len(batch) < page or len(rows) >= max_rows:
            break
        offset += page
    return pd.DataFrame(rows)


def chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def q(v):
    return "'" + str(v).replace("'", "''") + "'"


def to_num(df, cols):
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def size_bucket(u):
    if not u or u <= 0 or pd.isna(u):
        return None
    for lo, hi, lab in SIZE_BUCKETS:
        if lo <= u <= hi:
            return lab
    return None


def era_bucket(y):
    if pd.isna(y) or y <= 0:
        return "Unknown"
    for lo, hi, lab in ERA_BUCKETS:
        if lo <= y <= hi:
            return lab
    return "Unknown"


def dep_share(size_lab, year):
    if not isinstance(size_lab, str):
        return 0.0
    pre, mid, new = DEPENDENCY[size_lab]
    if pd.isna(year) or year <= 0:
        return (pre, mid, new)[UNKNOWN_ERA_DEP_INDEX]
    if year < 1960:
        return pre
    if year < 2000:
        return mid
    return new


def typology(bldgclass, unitsres, numfloors):
    bc = (bldgclass or "").upper()
    k = bc[:1]
    if k == "A":
        return "1-family house"
    if k == "B":
        return "2-family house"
    if bc == "C0":
        return "3-family house"
    if k == "C":
        return "Walk-up apartments"
    if k == "D":
        return "Elevator apartments"
    if k == "S":
        return "Mixed use, residential over store"
    if k == "R":
        return "Condo / co-op unit lots"
    if unitsres and unitsres > 0:
        return "Other with residential units"
    return "Non-residential"


def is_elevator(bldgclass, numfloors):
    bc = (bldgclass or "").upper()
    return bc.startswith("D") or bc in ("R4", "RR", "R9") or (numfloors or 0) >= 7


# --------------------------------------------------------------------------
# 1. Locate the site
# --------------------------------------------------------------------------
def normalize_address(a):
    a = a.upper().strip()
    a = re.sub(r",.*$", "", a)
    for short, full in [(r"\bAVE\b\.?", "AVENUE"), (r"\bST\b\.?", "STREET"), (r"\bPL\b\.?", "PLACE"),
                        (r"\bRD\b\.?", "ROAD"), (r"\bBLVD\b\.?", "BOULEVARD"), (r"\bPKWY\b\.?", "PARKWAY")]:
        a = re.sub(short, full, a)
    return re.sub(r"\s+", " ", a)


def locate_site(args):
    if args.lat and args.lon:
        return {"lat": args.lat, "lon": args.lon, "address": args.address or "Custom point", "bbl": None, "borough": args.borough}
    where = f"borough={q(args.borough)} and "
    if args.bbl:
        where += f"bbl={int(args.bbl)}"
    else:
        where += f"address={q(normalize_address(args.address))}"
    df = soql("64uk-42ks", where=where, select="bbl,address,latitude,longitude,bldgclass,unitsres,yearbuilt,ownername,zipcode")
    if df.empty:
        sys.exit(f"Could not find '{args.address}' in PLUTO for borough {args.borough}. "
                 f"Try --bbl or --lat/--lon.")
    r = df.iloc[0]
    log(f"Site: {r['address']} (BBL {int(float(r['bbl']))}), {r.get('bldgclass')}, built {r.get('yearbuilt')}")
    return {"lat": float(r["latitude"]), "lon": float(r["longitude"]), "address": r["address"],
            "bbl": int(float(r["bbl"])), "borough": args.borough, "zip": r.get("zipcode")}


# --------------------------------------------------------------------------
# 2. Walk network and walk shed
# --------------------------------------------------------------------------
def build_network(site, radius_m):
    log(f"Fetching street centerline within {radius_m:,.0f} m")
    df = soql("inkn-q76z",
              where=f"within_circle(the_geom,{site['lat']},{site['lon']},{radius_m:.0f})",
              select="the_geom,rw_type,nonped,full_street_name,physicalid")
    df = df[df["rw_type"].astype(str).isin(WALKABLE_RW_TYPES)]
    df = df[df["nonped"].isna() | (df["nonped"].astype(str).str.strip() == "")]
    G = nx.Graph()
    lines, names = [], []

    def node_key(x, y):
        return (round(x, 0), round(y, 0))  # snap endpoints to 1 ft

    for _, r in df.iterrows():
        geom = r["the_geom"]
        parts = geom["coordinates"] if geom["type"] == "MultiLineString" else [geom["coordinates"]]
        for coords in parts:
            pts = [TO_FT.transform(x, y) for x, y in coords]
            if len(pts) < 2:
                continue
            ls = LineString(pts)
            a, b = node_key(*pts[0]), node_key(*pts[-1])
            if a == b:
                continue
            if G.has_edge(a, b) and G[a][b]["length"] <= ls.length:
                continue
            G.add_edge(a, b, length=ls.length, geom=ls, name=r.get("full_street_name"))
    # keep the largest connected component
    if G.number_of_nodes() == 0:
        sys.exit("No street segments returned; check the address or network access.")
    comp = max(nx.connected_components(G), key=len)
    G = G.subgraph(comp).copy()
    for a, b, d in G.edges(data=True):
        lines.append(d["geom"])
        names.append((a, b))
    log(f"Walk network: {G.number_of_nodes():,} nodes, {G.number_of_edges():,} segments")
    return G, lines, names


def snap(point, tree, lines, names):
    """Nearest edge to a point: returns (edge_key, distance_along, offset_ft)."""
    idx = tree.nearest(point)
    ln = lines[idx]
    return names[idx], ln.project(point), ln.distance(point), ln


def walk_distances(G, tree, lines, names, origin_xy):
    o = Point(origin_xy)
    (a, b), along, off, ln = snap(o, tree, lines, names)
    # orient the edge geometry so along is measured from node a
    if Point(ln.coords[0]).distance(Point(a)) > Point(ln.coords[-1]).distance(Point(a)):
        along = ln.length - along
    H = G.copy()
    H.add_node("ORIGIN")
    H.add_edge("ORIGIN", a, length=along + off)
    H.add_edge("ORIGIN", b, length=(ln.length - along) + off)
    dist = nx.single_source_dijkstra_path_length(H, "ORIGIN", weight="length")
    dist.pop("ORIGIN", None)
    return dist


def point_walk_ft(pt, dist, tree, lines, names):
    (a, b), along, off, ln = snap(pt, tree, lines, names)
    if Point(ln.coords[0]).distance(Point(a)) > Point(ln.coords[-1]).distance(Point(a)):
        along = ln.length - along
    da, db = dist.get(a, math.inf), dist.get(b, math.inf)
    return min(da + along, db + (ln.length - along)) + off


def shed_polygon(G, dist, max_ft, buffer_ft=160):
    """Network buffer: every street length reachable within max_ft, buffered."""
    segs = []
    for a, b, d in G.edges(data=True):
        ln = d["geom"]
        if Point(ln.coords[0]).distance(Point(a)) > Point(ln.coords[-1]).distance(Point(a)):
            ln = LineString(list(ln.coords)[::-1])
        da, db = dist.get(a, math.inf), dist.get(b, math.inf)
        L = ln.length
        if da + L <= max_ft or db + L <= max_ft:
            segs.append(ln)
            continue
        if da < max_ft:
            segs.append(substring(ln, 0, min(L, max_ft - da)))
        if db < max_ft:
            segs.append(substring(ln, max(0, L - (max_ft - db)), L))
    if not segs:
        return None
    return unary_union([s.buffer(buffer_ft, cap_style=1) for s in segs if s.length > 0])


# --------------------------------------------------------------------------
# 3. PLUTO lots
# --------------------------------------------------------------------------
PLUTO_COLS = ("bbl,borough,block,lot,address,zipcode,bldgclass,landuse,ownertype,ownername,lotarea,bldgarea,"
              "resarea,comarea,retailarea,numbldgs,numfloors,unitsres,unitstotal,yearbuilt,yearalter1,yearalter2,"
              "zonedist1,version,latitude,longitude,xcoord,ycoord,bct2020,bctcb2020,cd,council,assesstot")


def fetch_pluto(site, radius_m):
    dlat = radius_m / 111_320
    dlon = radius_m / (111_320 * math.cos(math.radians(site["lat"])))
    where = (f"borough={q(site['borough'])} and latitude between {site['lat']-dlat} and {site['lat']+dlat} "
             f"and longitude between {site['lon']-dlon} and {site['lon']+dlon}")
    log("Fetching PLUTO lots around the site")
    df = soql("64uk-42ks", where=where, select=PLUTO_COLS)
    return df


def fetch_pluto_tracts(borough, tracts):
    """All lots in the given 2020 tracts, so census apportionment uses complete block-group totals."""
    out = []
    for ch in chunks(sorted(tracts), 40):
        where = f"borough={q(borough)} and bct2020 in ({','.join(q(t) for t in ch)})"
        out.append(soql("64uk-42ks", where=where, select="bbl,unitsres,bct2020,bctcb2020"))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def prep_pluto(df):
    df = to_num(df.copy(), ["bbl", "block", "lot", "lotarea", "bldgarea", "resarea", "comarea", "retailarea",
                            "numbldgs", "numfloors", "unitsres", "unitstotal", "yearbuilt", "yearalter1",
                            "yearalter2", "latitude", "longitude", "xcoord", "ycoord", "assesstot"])
    df["bbl"] = df["bbl"].astype("Int64")
    df["unitsres"] = df["unitsres"].fillna(0)
    for c in ("bldgclass", "address", "ownername"):
        if c in df.columns:
            df[c] = df[c].fillna("").astype(str)
    return df


def geoid_parts(bctcb2020, county):
    """PLUTO bctcb2020 = borocode(1)+tract(6)+block(4) -> tract GEOID, BG GEOID, block GEOID."""
    s = str(bctcb2020 or "")
    if len(s) != 11:
        return None, None, None
    tract, block = s[1:7], s[7:11]
    base = STATE_FIPS + county + tract
    return base, base + block[0], base + block


# --------------------------------------------------------------------------
# 4. HPD, DOB, affordable housing, licenses
# --------------------------------------------------------------------------
def fetch_hpd(boroid, blocks):
    log("Fetching HPD buildings and registrations")
    b, r = [], []
    for ch in chunks(sorted(blocks), 150):
        blist = ",".join(q(x) for x in ch)
        b.append(soql("kj4p-ruqc", where=f"boroid={q(boroid)} and block in ({blist})",
                      select="buildingid,block,lot,bin,legalclassa,legalclassb,managementprogram,"
                             "dobbuildingclass,legalstories,lifecycle,recordstatus"))
        r.append(soql("tesw-yqqr", where=f"boroid={q(boroid)} and block in ({blist})",
                      select="registrationid,buildingid,block,lot,lastregistrationdate,registrationenddate"))
    bld = pd.concat(b, ignore_index=True) if b else pd.DataFrame()
    reg = pd.concat(r, ignore_index=True) if r else pd.DataFrame()
    return bld, reg


def fetch_dob(dob_boro, blocks):
    log("Fetching DOB job filings (BIS and DOB NOW)")
    bis, now = [], []
    for ch in chunks(sorted(blocks), 120):
        blist = ",".join(q(f"{int(x):05d}") for x in ch) + "," + ",".join(q(str(int(x))) for x in ch)
        bis.append(soql("ic3t-wcy2", where=f"borough={q(dob_boro)} and block in ({blist})",
                        select="job__,doc__,block,lot,bin__,job_type,job_status_descrp,latest_action_date,"
                               "pre__filing_date,existing_dwelling_units,proposed_dwelling_units,"
                               "existing_occupancy,proposed_occupancy,job_description"))
        now.append(soql("w9ak-ipjd", where=f"borough={q(dob_boro)} and block in ({blist})",
                        select="job_filing_number,filing_status,block,lot,bin,job_type,filing_date,"
                               "existing_dwelling_units,proposed_dwelling_units,job_description"))
    bis = pd.concat(bis, ignore_index=True) if bis else pd.DataFrame()
    now = pd.concat(now, ignore_index=True) if now else pd.DataFrame()
    frames = []
    if not bis.empty:
        d = pd.DataFrame({
            "source": "DOB BIS", "job": bis["job__"], "block": pd.to_numeric(bis["block"], errors="coerce"),
            "lot": pd.to_numeric(bis["lot"], errors="coerce"), "job_type": bis["job_type"],
            "status": bis.get("job_status_descrp"),
            "date": pd.to_datetime(bis.get("pre__filing_date"), errors="coerce", format="%m/%d/%Y"),
            "existing_units": pd.to_numeric(bis.get("existing_dwelling_units"), errors="coerce"),
            "proposed_units": pd.to_numeric(bis.get("proposed_dwelling_units"), errors="coerce"),
            "description": bis.get("job_description")})
        d = d.drop_duplicates("job")  # BIS has one row per document
        frames.append(d)
    if not now.empty:
        d = pd.DataFrame({
            "source": "DOB NOW", "job": now["job_filing_number"].str.split("-").str[0],
            "block": pd.to_numeric(now["block"], errors="coerce"), "lot": pd.to_numeric(now["lot"], errors="coerce"),
            "job_type": now["job_type"], "status": now.get("filing_status"),
            "date": pd.to_datetime(now.get("filing_date"), errors="coerce"),
            "existing_units": pd.to_numeric(now.get("existing_dwelling_units"), errors="coerce"),
            "proposed_units": pd.to_numeric(now.get("proposed_dwelling_units"), errors="coerce"),
            "description": now.get("job_description")})
        d = d.drop_duplicates("job")
        frames.append(d)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def fetch_points(dataset, site, radius_m, select, extra_where=None, latf="latitude", lonf="longitude"):
    dlat = radius_m / 111_320
    dlon = radius_m / (111_320 * math.cos(math.radians(site["lat"])))
    where = (f"{latf} between {site['lat']-dlat} and {site['lat']+dlat} and "
             f"{lonf} between {site['lon']-dlon} and {site['lon']+dlon}")
    if extra_where:
        where += f" and {extra_where}"
    return soql(dataset, where=where, select=select)


# --------------------------------------------------------------------------
# 5. Census
# --------------------------------------------------------------------------
ACS_TABLES = {
    "B01003": "Total population",
    "B03002": "Hispanic or Latino origin by race",
    "B11001": "Household type",
    "B19001": "Household income",
    "B19013": "Median household income",
    "B25003": "Tenure",
    "B25008": "Population in occupied units by tenure",
    "B25024": "Units in structure",
    "B25032": "Tenure by units in structure",
    "B25034": "Year structure built",
    "B25036": "Tenure by year structure built",
    "B25044": "Tenure by vehicles available",
    "B25064": "Median gross rent",
    "B25070": "Gross rent as % of household income",
}
MEDIAN_TABLES = {"B19013", "B25064"}


def census_get(url, params, key):
    p = dict(params)
    if key:
        p["key"] = key
    r = SESSION.get(url, params=p, timeout=120)
    if r.status_code != 200 or not r.text.strip().startswith("["):
        raise RuntimeError(f"Census API {r.status_code}: {r.text[:200]}")
    j = r.json()
    return pd.DataFrame(j[1:], columns=j[0])


def latest_acs_year(key):
    for yr in range(date.today().year - 1, 2018, -1):
        try:
            census_get(f"{CENSUS}/{yr}/acs/acs5", {"get": "NAME", "for": f"state:{STATE_FIPS}"}, key)
            return yr
        except Exception:
            continue
    raise RuntimeError("No ACS 5-year vintage reachable")


def acs_labels(yr, table, key):
    r = SESSION.get(f"{CENSUS}/{yr}/acs/acs5/groups/{table}.json", timeout=60)
    v = r.json()["variables"]
    return {k: re.sub(r"^Estimate!!", "", d["label"]).replace("!!", " | ").replace(":", "")
            for k, d in v.items() if k.endswith("E") and not k.startswith("NAME")}


def fetch_acs(key, county, tracts):
    yr = latest_acs_year(key)
    log(f"Fetching ACS {yr - 4}-{yr} 5-year block-group tables")
    out, labels = {}, {}
    for t in ACS_TABLES:
        frames = []
        for ch in chunks(sorted(tracts), 50):
            df = census_get(f"{CENSUS}/{yr}/acs/acs5",
                            {"get": f"group({t})", "for": "block group:*",
                             "in": f"state:{STATE_FIPS} county:{county} tract:{','.join(ch)}"}, key)
            frames.append(df)
        df = pd.concat(frames, ignore_index=True)
        df["bg"] = df["state"] + df["county"] + df["tract"] + df["block group"]
        ecols = [c for c in df.columns if re.match(rf"^{t}_\d+E$", c)]
        df = df[["bg"] + ecols].copy()
        for c in ecols:
            df[c] = pd.to_numeric(df[c], errors="coerce")
            df.loc[df[c] < -999, c] = np.nan  # census sentinel values
        out[t] = df.set_index("bg")
        try:
            labels[t] = acs_labels(yr, t, key)
        except Exception:
            labels[t] = {c: c for c in ecols}
    return yr, out, labels


def fetch_decennial_blocks(key, county, tracts):
    """2020 block counts. DHC has tenure; the PL 94-171 file is the fallback (no tenure)."""
    log("Fetching 2020 Census block counts")
    dhc = {"P1_001N": "Population", "H1_001N": "Housing units", "H3_002N": "Occupied units",
           "H3_003N": "Vacant units", "H4_002N": "Owned with mortgage", "H4_003N": "Owned free and clear",
           "H4_004N": "Renter occupied"}
    pl = {"P1_001N": "Population", "H1_001N": "Housing units", "H1_002N": "Occupied units",
          "H1_003N": "Vacant units"}
    frames = []
    for tr in sorted(tracts):
        geo = {"for": "block:*", "in": f"state:{STATE_FIPS} county:{county} tract:{tr}"}
        try:
            df = census_get(f"{CENSUS}/2020/dec/dhc", {"get": ",".join(dhc), **geo}, key).rename(columns=dhc)
        except Exception:
            df = census_get(f"{CENSUS}/2020/dec/pl", {"get": ",".join(pl), **geo}, key).rename(columns=pl)
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    df["block_geoid"] = df["state"] + df["county"] + df["tract"] + df["block"]
    cols = [c for c in dict.fromkeys(list(dhc.values()) + list(pl.values())) if c in df.columns]
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.set_index("block_geoid")[cols]


# --------------------------------------------------------------------------
# 6. Summaries
# --------------------------------------------------------------------------
def band_labels(rings):
    lo = [0] + rings[:-1]
    return [f"{a}-{b} min" for a, b in zip(lo, rings)]


def ring_label(m, rings):
    for lab, r in zip(band_labels(rings), rings):
        if m <= r:
            return lab
    return None


def cumulative(df, rings, fn):
    """Apply fn to each cumulative ring (<=5, <=10, <=15)."""
    out = {}
    for r in rings:
        out[f"Within {r} min"] = fn(df[df["walk_min"] <= r])
    return pd.DataFrame(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--address", default="2220 Bath Avenue")
    ap.add_argument("--borough", default="BK", choices=list(BORO))
    ap.add_argument("--bbl", type=int)
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--minutes", default="5,10,15", help="walk-time rings in minutes")
    ap.add_argument("--speed-mph", type=float, default=3.0, help="walking speed (3.0 mph is standard)")
    ap.add_argument("--census-key", default=os.getenv("CENSUS_API_KEY"))
    ap.add_argument("--skip-census", action="store_true")
    ap.add_argument("--rankings", help="path to Fresh_Fold_Acquisition_Target_Rankings.xlsx")
    ap.add_argument("--outdir", default="walkshed_output")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    rings = sorted(int(x) for x in args.minutes.split(","))
    ft_per_min = args.speed_mph * 5280 / 60
    max_ft = rings[-1] * ft_per_min
    radius_m = max_ft / FT_PER_M * 1.15 + 250  # straight-line radius that safely contains the walk shed
    borocode, county, dob_boro, hpd_boro = BORO[args.borough]
    sources = []
    notes = []

    # ---- site and network
    site = locate_site(args)
    sx, sy = TO_FT.transform(site["lon"], site["lat"])
    G, lines, names = build_network(site, radius_m)
    tree = STRtree(lines)
    dist = walk_distances(G, tree, lines, names, (sx, sy))
    sources.append(("Street Centerline (CSCL)", "inkn-q76z", "NYC DoITT", "Pedestrian walk network"))

    # ---- lots
    area_lots = prep_pluto(fetch_pluto(site, radius_m))
    lots = area_lots.copy()
    sources.append(("PLUTO", "64uk-42ks", f"NYC DCP, version {lots.get('version', pd.Series(['see dataset'])).iloc[0] if 'version' in lots else 'current'}",
                    "Tax lots, building class, units, year built"))
    lots = lots.dropna(subset=["xcoord", "ycoord"])
    lots["walk_ft"] = [point_walk_ft(Point(x, y), dist, tree, lines, names)
                       for x, y in zip(lots["xcoord"], lots["ycoord"])]
    lots["walk_min"] = lots["walk_ft"] / ft_per_min
    lots = lots[lots["walk_min"] <= rings[-1]].copy()
    lots["ring"] = lots["walk_min"].apply(lambda m: ring_label(m, rings))
    lots["typology"] = [typology(b, u, f) for b, u, f in zip(lots["bldgclass"], lots["unitsres"], lots["numfloors"])]
    lots["size_bucket"] = lots["unitsres"].apply(size_bucket)
    lots["era"] = lots["yearbuilt"].apply(era_bucket)
    lots["pre_1960"] = (lots["yearbuilt"] > 0) & (lots["yearbuilt"] < 1960)
    lots["elevator"] = [is_elevator(b, f) for b, f in zip(lots["bldgclass"], lots["numfloors"])]
    lots["dependency_share"] = [dep_share(s, y) for s, y in zip(lots["size_bucket"], lots["yearbuilt"])]
    lots["dependent_units_est"] = lots["unitsres"] * lots["dependency_share"]
    parts = lots["bctcb2020"].apply(lambda s: geoid_parts(s, county))
    lots["tract_geoid"] = parts.str[0]
    lots["bg_geoid"] = parts.str[1]
    lots["block_geoid"] = parts.str[2]
    log(f"{len(lots):,} lots and {int(lots['unitsres'].sum()):,} residential units within {rings[-1]} minutes")

    # ---- HPD
    blocks = set(lots["block"].dropna().astype(int))
    try:
        hpd_b, hpd_r = fetch_hpd(hpd_boro, [str(b) for b in blocks])
        sources.append(("HPD Buildings Subject to HPD Jurisdiction", "kj4p-ruqc", "NYC HPD", "Legal Class A units, program"))
        sources.append(("HPD Multiple Dwelling Registrations", "tesw-yqqr", "NYC HPD", "Registered multiple dwellings"))
        if not hpd_b.empty:
            hpd_b = to_num(hpd_b, ["block", "lot", "legalclassa", "legalclassb", "legalstories"])
            hpd_b = hpd_b[hpd_b["recordstatus"].fillna("Active") == "Active"]
            agg = hpd_b.groupby(["block", "lot"]).agg(
                hpd_buildings=("buildingid", "nunique"), hpd_class_a_units=("legalclassa", "sum"),
                hpd_program=("managementprogram", lambda s: ", ".join(sorted(set(s.dropna()))))).reset_index()
            lots = lots.merge(agg, on=["block", "lot"], how="left")
        if not hpd_r.empty:
            hpd_r = to_num(hpd_r, ["block", "lot"])
            hpd_r["registrationenddate"] = pd.to_datetime(hpd_r["registrationenddate"], errors="coerce")
            ragg = hpd_r.groupby(["block", "lot"]).agg(hpd_reg_end=("registrationenddate", "max")).reset_index()
            lots = lots.merge(ragg, on=["block", "lot"], how="left")
            lots["hpd_registered_current"] = lots["hpd_reg_end"] >= pd.Timestamp.today().normalize()
    except Exception as e:
        notes.append(f"HPD data skipped: {e}")
        log(notes[-1])

    # ---- DOB
    dob = pd.DataFrame()
    try:
        dob = fetch_dob(dob_boro, blocks)
        sources.append(("DOB Job Application Filings (BIS)", "ic3t-wcy2", "NYC DOB", "Legacy job filings"))
        sources.append(("DOB NOW: Build Job Application Filings", "w9ak-ipjd", "NYC DOB", "Current job filings"))
        if not dob.empty:
            dob = dob.merge(lots[["block", "lot", "address", "walk_min", "unitsres", "yearbuilt", "bldgclass"]],
                            on=["block", "lot"], how="inner")
    except Exception as e:
        notes.append(f"DOB data skipped: {e}")
        log(notes[-1])

    nb = pd.DataFrame()
    laundry_jobs = pd.DataFrame()
    if not dob.empty:
        is_nb = dob["job_type"].astype(str).str.upper().isin(["NB", "NEW BUILDING"])
        nb = dob[is_nb].sort_values("date", ascending=False)
        laundry_jobs = dob[dob["description"].astype(str).str.upper().str.contains(LAUNDRY_TERMS, regex=True, na=False)]
        laundry_jobs = laundry_jobs.sort_values("date", ascending=False)
        lots = lots.merge(laundry_jobs.groupby(["block", "lot"]).size().rename("dob_laundry_jobs").reset_index(),
                          on=["block", "lot"], how="left")
        lots["dob_laundry_jobs"] = lots["dob_laundry_jobs"].fillna(0).astype(int)

    # ---- affordable housing
    aff = pd.DataFrame()
    try:
        aff = fetch_points("hg8x-zxpr", site, radius_m,
                           "project_name,building_completion_date,house_number,street_name,bbl,latitude,longitude,"
                           "reporting_construction_type,total_units,counted_rental_units")
        sources.append(("Affordable Housing Production by Building", "hg8x-zxpr", "NYC HPD", "New/preserved affordable units"))
        if not aff.empty:
            aff = to_num(aff, ["latitude", "longitude", "total_units", "counted_rental_units"])
            aff = aff.dropna(subset=["latitude", "longitude"])
            aff["walk_min"] = [point_walk_ft(Point(*TO_FT.transform(lo, la)), dist, tree, lines, names) / ft_per_min
                               for la, lo in zip(aff["latitude"], aff["longitude"])]
            aff = aff[aff["walk_min"] <= rings[-1]].sort_values("walk_min")
    except Exception as e:
        notes.append(f"Affordable housing data skipped: {e}")

    # ---- competitors: DCWP licenses and Fresh Fold rankings
    comps = []
    try:
        lic = fetch_points("w7w3-xahh", site, radius_m,
                           "business_name,dba_trade_name,business_category,license_status,address_building,"
                           "address_street_name,latitude,longitude",
                           extra_where="upper(business_category) like '%LAUND%'")
        sources.append(("DCWP Issued Licenses", "w7w3-xahh", "NYC DCWP", "Licensed laundries (most self-service laundromats are not licensed)"))
        lic = to_num(lic, ["latitude", "longitude"])
        if lic.empty or "latitude" not in lic.columns:
            lic = pd.DataFrame(columns=["latitude", "longitude"])
        for _, r in lic.dropna(subset=["latitude", "longitude"]).iterrows():
            comps.append({"source": f"DCWP license ({r['business_category']}, {r.get('license_status')})",
                          "name": r.get("dba_trade_name") or r.get("business_name"),
                          "address": f"{r.get('address_building','')} {r.get('address_street_name','')}".strip(),
                          "lat": r["latitude"], "lon": r["longitude"]})
    except Exception as e:
        notes.append(f"DCWP licenses skipped: {e}")
    if args.rankings and os.path.exists(args.rankings):
        try:
            rk = pd.read_excel(args.rankings, sheet_name="Store Rankings")
            rk.columns = [str(c).replace("\n", " ") for c in rk.columns]
            area = area_lots.copy()
            area["addr_norm"] = area["address"].astype(str).str.upper()
            amap = area.dropna(subset=["latitude"]).drop_duplicates("addr_norm").set_index("addr_norm")
            for _, r in rk.iterrows():
                key = normalize_address(str(r["Address"]))
                if key in amap.index:
                    a = amap.loc[key]
                    comps.append({"source": "Fresh Fold rankings", "name": r["Store Name"], "address": r["Address"],
                                  "lat": a["latitude"], "lon": a["longitude"], "rank_overall": r.get("Rank Overall"),
                                  "acq_score": r.get("Acquisition Score"), "google_rating": r.get("Google Rating"),
                                  "weekly_hours": r.get("Weekly Hours"), "dnc": r.get("Do Not Contact")})
            sources.append(("Fresh Fold Acquisition Target Rankings", args.rankings, "Fresh Fold", "Competitor locations and scores"))
        except Exception as e:
            notes.append(f"Rankings file skipped: {e}")
    comps = pd.DataFrame(comps)
    if not comps.empty:
        comps["walk_min"] = [point_walk_ft(Point(*TO_FT.transform(lo, la)), dist, tree, lines, names) / ft_per_min
                             for la, lo in zip(comps["lat"], comps["lon"])]
        comps = comps[comps["walk_min"] <= rings[-1]]
        is_site = comps["address"].astype(str).apply(normalize_address) == normalize_address(site["address"])
        comps = comps[~is_site].sort_values("walk_min")

    # ---- census
    acs_tables, census_blocks, acs_year = {}, pd.DataFrame(), None
    if args.skip_census:
        notes.append("Census skipped by --skip-census.")
    elif not args.census_key:
        notes.append("Census skipped: set CENSUS_API_KEY (free at https://api.census.gov/data/key_signup.html).")
        log(notes[-1])
    else:
        try:
            tracts7 = set(lots["bct2020"].dropna().astype(str))          # borocode + tract
            tracts6 = sorted({t[1:] for t in tracts7 if len(t) == 7})
            full = prep_pluto(fetch_pluto_tracts(args.borough, tracts7))
            fp = full["bctcb2020"].apply(lambda s: geoid_parts(s, county))
            full["bg_geoid"], full["block_geoid"] = fp.str[1], fp.str[2]
            bg_units = full.groupby("bg_geoid")["unitsres"].sum()
            blk_units = full.groupby("block_geoid")["unitsres"].sum()
            acs_year, acs_raw, acs_lab = fetch_acs(args.census_key, county, tracts6)
            sources.append((f"ACS 5-year {acs_year-4}-{acs_year}", "api.census.gov", "U.S. Census Bureau", "Block-group demographics and housing"))
            for t, df in acs_raw.items():
                rows = {}
                for r in rings:
                    sub = lots[lots["walk_min"] <= r]
                    w = (sub.groupby("bg_geoid")["unitsres"].sum() / bg_units).reindex(df.index).fillna(0).clip(0, 1)
                    if t in MEDIAN_TABLES:  # units-weighted average of block-group medians
                        wu = sub.groupby("bg_geoid")["unitsres"].sum().reindex(df.index).fillna(0)
                        rows[f"Within {r} min"] = (df.mul(wu, axis=0).sum() / max(wu[df.iloc[:, 0].notna()].sum(), 1))
                    else:
                        rows[f"Within {r} min"] = df.mul(w, axis=0).sum()
                res = pd.DataFrame(rows)
                res.insert(0, "Variable", [acs_lab[t].get(c, c) for c in res.index])
                acs_tables[t] = res
            census_blocks = fetch_decennial_blocks(args.census_key, county, tracts6)
            sources.append(("2020 Decennial Census (DHC / PL 94-171)", "api.census.gov", "U.S. Census Bureau", "Block population, units, tenure"))
            dec_rows = {}
            for r in rings:
                sub = lots[lots["walk_min"] <= r]
                w = (sub.groupby("block_geoid")["unitsres"].sum() / blk_units).reindex(census_blocks.index).fillna(0).clip(0, 1)
                dec_rows[f"Within {r} min"] = census_blocks.mul(w, axis=0).sum()
            census_blocks = pd.DataFrame(dec_rows)
        except Exception as e:
            notes.append(f"Census step failed: {e}")
            log(notes[-1])

    # ---------------------------------------------------------------- tables
    res = lots[lots["unitsres"] > 0]

    def headline(d):
        r_ = d[d["unitsres"] > 0]
        u = r_["unitsres"].sum()
        return pd.Series({
            "Tax lots": len(d),
            "Residential lots": len(r_),
            "Residential units": u,
            "Units in 1-2 family houses": r_.loc[r_["unitsres"] <= 2, "unitsres"].sum(),
            "Units in 3-19 unit buildings": r_.loc[r_["unitsres"].between(3, 19), "unitsres"].sum(),
            "Units in 20+ unit buildings": r_.loc[r_["unitsres"] >= 20, "unitsres"].sum(),
            "Share of units in buildings under 20 units": r_.loc[r_["unitsres"] < 20, "unitsres"].sum() / u if u else 0,
            "Share of units built pre-1960": r_.loc[r_["pre_1960"], "unitsres"].sum() / u if u else 0,
            "Share of units built pre-1940": r_.loc[(r_["yearbuilt"] > 0) & (r_["yearbuilt"] < 1940), "unitsres"].sum() / u if u else 0,
            "Units in elevator buildings": r_.loc[r_["elevator"], "unitsres"].sum(),
            "Units built 2000 or later": r_.loc[r_["yearbuilt"] >= 2000, "unitsres"].sum(),
            "Median year built (unit-weighted)": weighted_median(r_["yearbuilt"].where(r_["yearbuilt"] > 0), r_["unitsres"]),
            "Est. laundromat-dependent units": r_["dependent_units_est"].sum(),
            "Est. dependency share": r_["dependent_units_est"].sum() / u if u else 0,
            "HPD Class A units (active)": d.get("hpd_class_a_units", pd.Series(dtype=float)).sum(),
            "HPD-registered lots (current)": int(d.get("hpd_registered_current", pd.Series(dtype=bool)).fillna(False).sum()),
            "Lots with DOB laundry-related jobs (incl. laundromats)": int((d.get("dob_laundry_jobs", pd.Series(dtype=int)) > 0).sum()),
        })

    summary = cumulative(lots, rings, headline)

    def pivot_units(d, idx, cols, order_cols=None):
        p = d.pivot_table(index=idx, columns=cols, values="unitsres", aggfunc="sum", fill_value=0)
        if order_cols:
            p = p.reindex(columns=[c for c in order_cols if c in p.columns])
        p["Total"] = p.sum(axis=1)
        p.loc["Total"] = p.sum()
        return p

    size_order = [b[2] for b in SIZE_BUCKETS]
    era_order = [b[2] for b in ERA_BUCKETS] + ["Unknown"]
    typ_by_ring = pivot_units(res, "typology", "ring", band_labels(rings))
    size_by_era = {r: pivot_units(res[res["walk_min"] <= r], "size_bucket", "era", era_order).reindex(size_order + ["Total"])
                   for r in rings}
    dep_by_ring = res.pivot_table(index="size_bucket", columns="ring", values="dependent_units_est",
                                  aggfunc="sum", fill_value=0).reindex(index=size_order, columns=band_labels(rings))
    dep_by_ring.index.name = "Est. laundromat-dependent units by walk band"
    dep_model = pd.DataFrame(DEPENDENCY, index=["Pre-1960", "1960-1999", "2000+"]).T
    dep_model.index.name = "Building size"

    big = (res.sort_values("unitsres", ascending=False)
              .loc[:, [c for c in ["address", "walk_min", "bldgclass", "typology", "unitsres", "yearbuilt", "numfloors",
                                   "ownername", "hpd_class_a_units", "hpd_program", "hpd_registered_current",
                                   "dob_laundry_jobs", "dependency_share", "bbl"] if c in res.columns]].head(150))

    # ---------------------------------------------------------------- shed polygons
    polys = {r: shed_polygon(G, dist, r * ft_per_min) for r in rings}
    area_sqmi = {r: (p.area / 27_878_400 if p is not None else 0) for r, p in polys.items()}
    summary.loc["Walk-shed area (sq mi)"] = [area_sqmi[r] for r in rings]
    summary.loc["Residential units per acre of walk shed"] = [
        summary.loc["Residential units", f"Within {r} min"] / (area_sqmi[r] * 640) if area_sqmi[r] else 0 for r in rings]
    if not comps.empty:
        summary.loc["Laundry competitors (known)"] = [int((comps["walk_min"] <= r).sum()) for r in rings]
        summary.loc["Dependent units per competitor"] = [
            summary.loc["Est. laundromat-dependent units", f"Within {r} min"] / max(1, (comps["walk_min"] <= r).sum())
            for r in rings]
    if not census_blocks.empty and "Population" in census_blocks.index:
        summary.loc["2020 Census population (apportioned)"] = census_blocks.loc["Population"].values
    if "B25003" in acs_tables:
        t = acs_tables["B25003"]
        tot, rent = t.iloc[0, 1:], t.iloc[2, 1:]
        summary.loc[f"ACS households (apportioned)"] = tot.values
        summary.loc[f"ACS renter share"] = (rent / tot.replace(0, np.nan)).values

    # ---------------------------------------------------------------- write Excel
    xlsx = os.path.join(args.outdir, "walkshed_summary.xlsx")
    with pd.ExcelWriter(xlsx, engine="openpyxl") as xw:
        meta = pd.DataFrame({
            "Item": ["Site", "BBL", "Walk speed", "Rings", "Run date", "Method"],
            "Value": [site["address"], site.get("bbl"), f"{args.speed_mph} mph ({ft_per_min:.0f} ft/min)",
                      ", ".join(f"{r} min" for r in rings), date.today().isoformat(),
                      "Network walk time on NYC street centerline from site to each tax lot centroid. "
                      "Census figures apportioned by PLUTO residential units."]})
        meta.to_excel(xw, sheet_name="Summary", index=False)
        summary.to_excel(xw, sheet_name="Summary", startrow=len(meta) + 2)
        typ_by_ring.to_excel(xw, sheet_name="Units by Type")
        for r in rings:
            size_by_era[r].to_excel(xw, sheet_name=f"Size x Era {r}min")
        dep_model.to_excel(xw, sheet_name="Dependency Model")
        dep_by_ring.to_excel(xw, sheet_name="Dependency Model", startrow=len(dep_model) + 3)
        big.to_excel(xw, sheet_name="Largest Buildings", index=False)
        if not nb.empty:
            nb.drop(columns=["block", "lot"]).to_excel(xw, sheet_name="New Buildings (DOB)", index=False)
        if not laundry_jobs.empty:
            laundry_jobs.drop(columns=["block", "lot"]).to_excel(xw, sheet_name="Laundry Jobs (DOB)", index=False)
        if not aff.empty:
            aff.to_excel(xw, sheet_name="Affordable Housing", index=False)
        if not comps.empty:
            comps.to_excel(xw, sheet_name="Competitors", index=False)
        row = 0
        for t, df in acs_tables.items():
            pd.DataFrame({"": [f"{t}: {ACS_TABLES[t]} (ACS {acs_year-4}-{acs_year}, apportioned)"]}).to_excel(
                xw, sheet_name="ACS", startrow=row, index=False, header=False)
            df.to_excel(xw, sheet_name="ACS", startrow=row + 1)
            row += len(df) + 4
        if not census_blocks.empty:
            census_blocks.to_excel(xw, sheet_name="Census 2020")
        pd.DataFrame(sources, columns=["Dataset", "ID", "Publisher / vintage", "Used for"]).to_excel(
            xw, sheet_name="Sources & Notes", index=False)
        pd.DataFrame({"Notes": notes + [
            "Dependency shares are assumptions (Dependency Model sheet); calibrate before relying on unit counts.",
            "Rent stabilization status is not on NYC Open Data; DHCR building lists are published separately.",
            "PLUTO condo lots report units on the billing lot; condo unit lots (R-classes) are counted once.",
            "DCWP licenses capture only licensed laundries; most self-service laundromats are unlicensed. "
            "Use --rankings to add the Fresh Fold competitor database.",
            "ACS medians are a unit-weighted average of block-group medians, an approximation.",
        ]}).to_excel(xw, sheet_name="Sources & Notes", startrow=len(sources) + 3, index=False)
        for ws in xw.book.worksheets:
            for col in ws.columns:
                width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
                ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 60)

    lots.drop(columns=[c for c in ["xcoord", "ycoord"] if c in lots]).to_csv(
        os.path.join(args.outdir, "walkshed_lots.csv"), index=False)

    # ---------------------------------------------------------------- map
    try:
        import folium
        m = folium.Map(location=[site["lat"], site["lon"]], zoom_start=15, tiles="OpenStreetMap")
        palette = ["#1a9850", "#fdae61", "#d73027", "#762a83", "#2166ac"]
        for i, r in reversed(list(enumerate(rings))):
            p = polys[r]
            if p is None:
                continue
            gj = mapping(shapely.transform(p, lambda c: np.column_stack(TO_LL.transform(c[:, 0], c[:, 1]))))
            folium.GeoJson(gj, name=f"{r}-minute walk",
                           style_function=lambda _, c=palette[i % len(palette)]: {"color": c, "weight": 2, "fillOpacity": 0.06}).add_to(m)
        lyr = folium.FeatureGroup(name="Residential lots (color = dependency, size = units)")
        for _, r in res.iterrows():
            share = r["dependency_share"]
            color = "#d73027" if share >= 0.7 else "#fc8d59" if share >= 0.5 else "#91bfdb" if share >= 0.3 else "#4575b4"
            folium.CircleMarker([r["latitude"], r["longitude"]], radius=max(2, min(12, math.sqrt(r["unitsres"]) * 1.5)),
                                color=color, fill=True, fill_opacity=0.7, weight=0,
                                tooltip=f"{r['address']} | {int(r['unitsres'])} units | built {int(r['yearbuilt']) if r['yearbuilt'] > 0 else 'n/a'} "
                                        f"| {r['bldgclass']} | {r['walk_min']:.1f} min").add_to(lyr)
        lyr.add_to(m)
        if not comps.empty:
            cl = folium.FeatureGroup(name="Laundry competitors")
            for _, c in comps.iterrows():
                folium.Marker([c["lat"], c["lon"]], icon=folium.Icon(color="black", icon="tint"),
                              tooltip=f"{c['name']} ({c['source']}) {c['walk_min']:.1f} min").add_to(cl)
            cl.add_to(m)
        folium.Marker([site["lat"], site["lon"]], icon=folium.Icon(color="blue", icon="star"),
                      tooltip=f"SITE: {site['address']}").add_to(m)
        folium.LayerControl().add_to(m)
        m.save(os.path.join(args.outdir, "walkshed_map.html"))
    except ImportError:
        notes.append("folium not installed; map skipped.")

    # ---------------------------------------------------------------- report
    def fmt(v, kind="n"):
        if pd.isna(v):
            return "n/a"
        return f"{v:.0%}" if kind == "p" else f"{v:,.1f}" if kind == "f" else f"{v:,.0f}"

    lines_md = [f"# Walk-shed housing stock: {site['address']}", "",
                f"Walk speed {args.speed_mph} mph on the NYC street centerline. Run {date.today().isoformat()}.", "",
                "| Metric | " + " | ".join(summary.columns) + " |", "|---|" + "---|" * len(summary.columns)]
    pct_rows = {"Share of units in buildings under 20 units", "Share of units built pre-1960",
                "Share of units built pre-1940", "Est. dependency share", "ACS renter share"}
    float_rows = { "Residential units per acre of walk shed", "Median year built (unit-weighted)"}
    for idx, row in summary.iterrows():
        kind = "p" if idx in pct_rows else "f" if idx in float_rows else "n"
        if idx == "Walk-shed area (sq mi)":
            lines_md.append(f"| {idx} | " + " | ".join(f"{v:.2f}" for v in row) + " |")
        elif idx == "Median year built (unit-weighted)":
            lines_md.append(f"| {idx} | " + " | ".join("n/a" if pd.isna(v) else f"{v:.0f}" for v in row) + " |")
        else:
            lines_md.append(f"| {idx} | " + " | ".join(fmt(v, kind) for v in row) + " |")
    if not nb.empty:
        recent = nb[nb["date"] >= pd.Timestamp.today() - pd.DateOffset(years=10)]
        lines_md += ["", f"New-building filings in the last 10 years: {len(recent)} "
                         f"({fmt(recent['proposed_units'].sum())} proposed units). New construction usually has "
                         "in-unit or in-building laundry, so it adds little laundromat demand."]
    if notes:
        lines_md += ["", "## Notes"] + [f"- {n}" for n in notes]
    with open(os.path.join(args.outdir, "walkshed_report.md"), "w") as f:
        f.write("\n".join(lines_md) + "\n")

    log(f"Done. Outputs in {os.path.abspath(args.outdir)}")
    print("\n".join(lines_md))


def weighted_median(values, weights):
    d = pd.DataFrame({"v": values, "w": weights}).dropna()
    d = d[d["w"] > 0].sort_values("v")
    if d.empty:
        return np.nan
    c = d["w"].cumsum()
    return d.loc[c >= d["w"].sum() / 2, "v"].iloc[0]


if __name__ == "__main__":
    main()
