"""Download the public reference files: NPPES (monthly), CMS Doctors & Clinicians, NUCC taxonomy.

Links are discovered from each publisher's page at run time (they change monthly), never
hardcoded. Each download streams to disk through a .part file. If a site can't be reached the
command says so and the pipeline runs on whatever sources you do have.

`extract_nppes` cuts the 9 GB NPPES file down to one state: every NPI whose practice or mailing
address is in the state, or that has a secondary practice location there. That is the
"NPPES California extract" the brief promises, built from the public file.
"""
from __future__ import annotations

import csv
import os
import re
import zipfile
from pathlib import Path
from urllib.parse import urljoin

import requests

from .sources import NPPES_COLS, PL_COLS, open_table, resolve_columns

PAGES = {
    "nppes": ("https://download.cms.gov/nppes/NPI_Files.html", r'href="([^"]*NPPES_Data_Dissemination_[A-Za-z]+_\d{4}[^"]*\.zip)"'),
    "nucc": ("https://www.nucc.org/index.php/code-sets-mainmenu-41/provider-taxonomy-mainmenu-40/csv-mainmenu-57",
             r'href="([^"]*nucc_taxonomy_\d+\.csv)"'),
}
DAC_METASTORE = "https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items/mj5m-pzi6"
UA = {"User-Agent": "hilabs-c5-pipeline/1.0"}


def _download(url: str, dest: Path, log=print) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=120, headers=UA) as r:
        r.raise_for_status()
        with open(part, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
    os.replace(part, dest)
    log(f"[fetch] {dest} ({dest.stat().st_size:,} bytes)")
    return dest


def discover(what: str) -> str:
    if what == "dac":
        meta = requests.get(DAC_METASTORE, timeout=60, headers=UA).json()
        return next(d["downloadURL"] for d in meta.get("distribution", []) if d.get("downloadURL"))
    page, rx = PAGES[what]
    html = requests.get(page, timeout=60, headers=UA).text
    links = [m for m in re.findall(rx, html) if "weekly" not in m.lower()]
    if not links:
        raise RuntimeError(f"no {what} download link found on {page}")
    return urljoin(page, links[0])


def fetch(whats: list[str], dest: Path, log=print) -> dict:
    got = {}
    for what in whats:
        try:
            url = discover(what)
            got[what] = str(_download(url, dest / Path(url.split("?")[0]).name, log))
        except Exception as exc:  # blocked or moved: report and carry on
            log(f"[fetch] {what}: not downloaded ({exc.__class__.__name__}: {exc})")
    return got


def extract_nppes(nppes: str | Path, state: str, out_dir: Path, log=print) -> tuple[Path, Path | None]:
    """Write <out>/nppes_<state>.csv (main file rows) and <out>/nppes_pl_<state>.csv (secondary locations)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    state = state.upper()
    pl_out, pl_npis = None, set()
    if zipfile.is_zipfile(nppes) and any("pl_pfile" in n.lower() for n in zipfile.ZipFile(nppes).namelist()):
        headers, rows = open_table(nppes, r"pl_pfile")
        cols = resolve_columns(headers, PL_COLS)
        si, ni = cols["state"], cols["npi"]
        pl_out = out_dir / f"nppes_pl_{state.lower()}.csv"
        with open(pl_out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(headers)
            for r in rows:
                if si < len(r) and r[si].strip().upper() == state:
                    w.writerow(r)
                    pl_npis.add(r[ni])
        log(f"[nppes] {len(pl_npis):,} NPIs with a secondary location in {state} -> {pl_out}")
    headers, rows = open_table(nppes, r"npidata_pfile")
    cols = resolve_columns(headers, NPPES_COLS)
    ps, ms, ni = cols["p_state"], cols["m_state"], cols["npi"]
    main_out = out_dir / f"nppes_{state.lower()}.csv"
    n = 0
    with open(main_out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(headers)
        for r in rows:
            if (ps < len(r) and r[ps].strip().upper() == state) or (ms < len(r) and r[ms].strip().upper() == state) \
                    or r[ni] in pl_npis:
                w.writerow(r)
                n += 1
    log(f"[nppes] {n:,} NPIs in {state} -> {main_out}")
    return main_out, pl_out


def read_zip_county(path: str | Path) -> dict[str, str]:
    """Any CSV/XLSX with a ZIP column and a county-name column (HUD ZIP-COUNTY, Census ZCTA files...)."""
    headers, rows = open_table(path)
    keys = [re.sub(r"[^a-z0-9]", "", h.lower()) for h in headers]
    zi = next(i for i, k in enumerate(keys) if k in ("zip", "zipcode", "zcta", "zcta5", "postalcode"))
    ci = next(i for i, k in enumerate(keys) if k in ("county", "countyname", "countynm", "uspszipprefcity") or
              k.startswith("countyname"))
    out = {}
    for r in rows:
        z = re.sub(r"\D", "", r[zi])[:5].zfill(5)
        if z and ci < len(r) and r[ci].strip() and z not in out:
            out[z] = re.sub(r"\s+County$", "", r[ci].strip(), flags=re.IGNORECASE).title()
    return out

