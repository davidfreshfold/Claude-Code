# Walk-Shed Housing Stock Analysis

Profiles every tax lot within a 5, 10 and 15 minute walk of a laundromat site, using NYC Open Data and the U.S. Census. Built for Fresh Fold acquisition screening; works for any NYC address.

## Setup (one time)

1. Put `walkshed_housing.py`, `requirements.txt` and `Fresh_Fold_Acquisition_Target_Rankings.xlsx` in one folder.
2. Get a free Census API key at https://api.census.gov/data/key_signup.html (arrives by email in a few minutes).
3. Optional: get a free NYC Open Data app token at https://data.cityofnewyork.us/profile/edit/developer_settings. It avoids rate limits on repeat runs.

## Running it in Claude Code

Open Claude Code in that folder and paste:

> Set up a Python virtual environment, install requirements.txt, then run
> `python walkshed_housing.py --address "2220 Bath Avenue" --borough BK --rankings Fresh_Fold_Acquisition_Target_Rankings.xlsx`
> with CENSUS_API_KEY set to [your key]. When it finishes, open walkshed_output/walkshed_report.md and summarize what the housing stock means for laundromat demand at this site. Flag any data source that failed.

To screen another target, change the address. For example, `--address "1948 Bath Avenue"`, or `--address "935 McDonald Avenue"` to benchmark your own store.

## Running it directly

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export CENSUS_API_KEY=your_key
python walkshed_housing.py --address "2220 Bath Avenue" --rankings Fresh_Fold_Acquisition_Target_Rankings.xlsx
```

Options:

| Flag | Default | Purpose |
|---|---|---|
| `--address` | 2220 Bath Avenue | Street address as PLUTO spells it (AVE, ST and similar are expanded automatically) |
| `--borough` | BK | MN, BX, BK, QN, SI |
| `--bbl` / `--lat --lon` | | Use instead of an address |
| `--minutes` | 5,10,15 | Walk-time rings |
| `--speed-mph` | 3.0 | Walking speed |
| `--rankings` | | Adds ranked competitors to the map and the competitor counts |
| `--skip-census` | | Run with NYC data only |
| `--outdir` | walkshed_output | Where results go |

## What it produces

- **walkshed_summary.xlsx**
  - Summary (by ring)
  - Units by building type
  - Building size by era of construction, one sheet per ring
  - Dependency model (assumptions and results)
  - Largest buildings, with owners and HPD status
  - New-building filings
  - Laundry-related DOB jobs
  - Affordable housing
  - Competitors
  - ACS tables
  - 2020 Census block counts
  - Sources and notes
- **walkshed_lots.csv**: every lot with its walk time, building class, units, year built, HPD and DOB flags, census geography and estimated dependent units.
- **walkshed_map.html**: walk-shed rings, residential lots colored by laundromat dependency and sized by units, and competitor pins.
- **walkshed_report.md**: the summary table plus notes on anything that failed.

## Method

- **Walk shed.** Network walking distance on the NYC Street Centerline. Highways, ramps and vehicle-only segments are removed. Each lot is timed from the site to its nearest street segment, then to the lot.
- **Housing stock.** PLUTO residential units by building class, unit count and year built. HPD adds legal Class A units and registration status.
- **Laundromat dependency.** A share of units by building size and era that likely lack in-unit or in-building laundry. The values are starting assumptions in the `DEPENDENCY` table near the top of the script. Calibrate them before relying on the unit counts.
- **Census.** ACS block-group and 2020 block counts are apportioned to each ring by PLUTO residential units (dasymetric), not by land area. ACS medians are a unit-weighted average of block-group medians.
- **Pipeline and signals.**
  - DOB BIS and DOB NOW new-building filings show supply coming online.
  - Job descriptions that mention laundry, washers or dryers flag buildings adding in-unit or in-building laundry, as well as other laundromats.

## Known gaps

- Rent stabilization status is not on NYC Open Data. DHCR publishes building lists separately.
- Most self-service laundromats are not DCWP-licensed, so use `--rankings` for competitors.
- No public dataset records in-unit washers, which is why the dependency shares are modeled assumptions.
