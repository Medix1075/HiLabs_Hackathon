"""Score every candidate address for every (NPI, TIN) pair and keep the best one or few.

For one pair, each candidate site gets:

  1. Source evidence. Each source's weight x recency. Within a family (NPPES and the NPI API
     share the same underlying records) only the best counts; families combine as independent
     witnesses:   p = 1 - prod_f (1 - best_f)
  2. Group alignment. CMS rows (by org_pac_id) and directory rows (by TIN) that belong to the
     group this TIN maps to are boosted; rows for the NPI's other groups are discounted.
  3. TIN consensus. The share of the TIN's other NPIs (its roster in the rate file) that also have
     this site:   p = 1 - (1 - p) x (1 - weight x share)
     A site shared by most of the roster is added even for members with no address of their own.
  4. Penalties for out-of-state sites and PO boxes.

The best site is kept, plus up to two more that are nearly as good (multi-site practitioners).
Every kept address carries its confidence, the families that agree on it, and the evidence.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date

from .address import Address, standardize


def region_for(zip5: str, regions: dict) -> str:
    if len(zip5) < 3 or not zip5[:3].isdigit():
        return ""
    z = int(zip5[:3])
    for rng, name in regions.items():
        lo, _, hi = str(rng).partition("-")
        if int(lo) <= z <= int(hi or lo):
            return name
    return f"ZIP3 {zip5[:3]}"


class Resolver:
    def __init__(self, cfg: dict, as_of: date, providers: dict, dac_facts: dict, zip_county: dict | None = None):
        self.cfg, self.as_of, self.providers, self.dac = cfg, as_of, providers, dac_facts
        self.zip_county = zip_county or {}
        sc = cfg["sources"]
        self.weights = sc["weights"]
        self.family = {s: f for f, members in sc["families"].items() for s in members}
        self.sites: dict[int, dict[str, list]] = defaultdict(lambda: defaultdict(list))  # npi -> site -> evidence
        self.addr: dict[str, Address] = {}                                               # site -> best display
        self.addr_rank: dict[str, float] = {}

    # -------------------------------------------------------------- evidence
    def _recency(self, d: date | None) -> float:
        rc = self.cfg["sources"]["recency"]
        if d is None:
            return rc["floor"]
        age = max(0.0, (self.as_of - d).days / 365.25)
        return max(rc["floor"], 0.5 ** (age / rc["half_life_years"]))

    def add_candidates(self, cands: list[dict]) -> int:
        n = 0
        for c in cands:
            if not c.get("line1"):
                continue
            a = standardize(c["line1"], c.get("line2", ""), c.get("city", ""), c.get("state", ""), c.get("zip", ""))
            if not a.line1:
                continue
            w = self.weights.get(c["source"], 0.0)
            ev = {"source": c["source"], "family": self.family.get(c["source"], c["source"]),
                  "p": w * self._recency(c.get("date")), "detail": c.get("detail", "")}
            self.sites[c["npi"]][a.site_key].append(ev)
            if w >= self.addr_rank.get(a.site_key, -1):  # display the address as the most trusted source wrote it
                self.addr[a.site_key], self.addr_rank[a.site_key] = a, w
            n += 1
        return n

    # -------------------------------------------------------------- the TIN view
    def _direct_strength(self, npi: int) -> dict[str, float]:
        """site -> how strongly the NPI's own sources place it there (noisy-OR over families, no TIN effects)."""
        out = {}
        for site, evs in self.sites.get(npi, {}).items():
            best: dict[str, float] = {}
            for e in evs:
                if e["family"] in ("nppes", "dac", "directory") and e["source"] != "nppes_mailing":
                    best[e["family"]] = max(best.get(e["family"], 0.0), e["p"])
            if best:
                q = 1.0
                for v in best.values():
                    q *= 1 - v
                out[site] = 1 - q
        return out

    def build_tin_view(self, pairs: list[tuple[int, str]]) -> None:
        """Per TIN: its roster, how strongly each site is backed by the roster, its organisation sites,
        and the CMS group (org_pac_id) most of its NPIs belong to."""
        self.roster: dict[str, set] = defaultdict(set)
        for npi, tin in pairs:
            self.roster[tin].add(npi)
        self.strength = {n: self._direct_strength(n) for n in {n for n, _ in pairs}}
        self.tin_sites: dict[str, Counter] = {}
        self.tin_mass: dict[str, float] = {}
        self.tin_with_any: dict[str, int] = {}
        self.org_sites: dict[str, set] = defaultdict(set)
        self.dominant_pac: dict[str, tuple[str, float]] = {}
        ag = self.cfg["group_alignment"]
        for tin, members in self.roster.items():
            mass, cnt, with_any = 0.0, Counter(), 0
            for m in members:
                st = self.strength.get(m) or {}
                if st:
                    with_any += 1
                    top = max(st.values())
                    mass += top
                    for site, v in st.items():
                        cnt[site] += v
                if str(self.providers.get(m, {}).get("entity_type")) == "2":
                    self.org_sites[tin].update(s for s, evs in self.sites.get(m, {}).items()
                                               if any(e["source"] in ("nppes_practice", "nppes_secondary",
                                                                      "npi_api_practice", "npi_api_secondary")
                                                      for e in evs))
            self.tin_sites[tin], self.tin_mass[tin], self.tin_with_any[tin] = cnt, mass, with_any
            pacs = Counter(p for m in members for p in self.dac.get(m, {}).get("pacs", ()))
            in_dac = sum(1 for m in members if self.dac.get(m, {}).get("pacs"))
            if pacs and in_dac >= 2:
                pac, k = pacs.most_common(1)[0]
                if k / in_dac >= ag["min_share"]:
                    self.dominant_pac[tin] = (pac, k / in_dac)

    # -------------------------------------------------------------- one pair
    def _consensus(self, npi: int, tin: str, site: str) -> float:
        """Share of the TIN's other NPIs at this site, each NPI weighted by how strongly its own
        sources place it there (a fresh CMS address counts more than a 2012 NPPES one)."""
        tc = self.cfg["tin_consensus"]
        own = self.strength.get(npi) or {}
        others = self.tin_with_any.get(tin, 0) - (1 if own else 0)
        if others + 1 < tc["min_roster"] or others <= 0:
            return 0.0
        mass = self.tin_mass[tin] - (max(own.values()) if own else 0.0)
        if mass <= 0:
            return 0.0
        return max(0.0, min(1.0, (self.tin_sites[tin][site] - own.get(site, 0.0)) / mass))

    def _adjust(self, ev: dict, tin: str) -> float:
        p, ag = ev["p"], self.cfg["group_alignment"]
        if ev["source"] == "dac" and tin in self.dominant_pac:
            pac, share = self.dominant_pac[tin]
            if json.loads(ev["detail"] or "{}").get("org_pac_id") == pac:
                return 1 - (1 - p) * (1 - ag["boost"] * share)
            return p * (1 - ag["discount"] * share)
        if ev["source"] in ("directory", "directory_tin") and ev["detail"]:
            dtin = json.loads(ev["detail"]).get("tin")
            if dtin:
                same = re.sub(r"\D", "", str(dtin)) == re.sub(r"\D", "", str(tin))  # "95-1234567" vs "951234567"
                return 1 - (1 - p) * (1 - ag["boost"]) if same else p * (1 - ag["discount"])
        return p

    def resolve_pair(self, npi: int, tin: str) -> tuple[list[dict], list[dict]]:
        cfg = self.cfg
        tc, pen, sel, ver = cfg["tin_consensus"], cfg["penalties"], cfg["select"], cfg["verified"]
        prov = self.providers.get(npi, {})
        if prov.get("deactivated") and sel["drop_deactivated"]:
            return [self._empty(npi, tin, "NPI deactivated in NPPES")], []
        evidence: dict[str, list] = defaultdict(list)
        for site, evs in self.sites.get(npi, {}).items():
            evidence[site].extend(evs)
        if str(prov.get("entity_type")) != "2":
            for site in self.org_sites.get(tin, ()):
                p = self.weights["tin_org_site"]
                evidence[site].append({"source": "tin_org_site", "family": "tin_roster", "p": p,
                                       "detail": "practice address of an organisation NPI under this TIN"})
        others = self.tin_with_any.get(tin, 0)
        if others + 1 >= tc["min_roster"]:
            for site, k in self.tin_sites.get(tin, Counter()).most_common(5):
                if site not in evidence and self._consensus(npi, tin, site) >= tc["add_site_min_share"]:
                    evidence[site] = []
        eligible = others + 1 >= tc["min_roster"]
        tin_has_home = eligible and any(self._consensus(npi, tin, s) >= tc["add_site_min_share"]
                                        for s, _ in self.tin_sites.get(tin, Counter()).most_common(3))
        scored = []
        for site, evs in evidence.items():
            best: dict[str, float] = {}
            srcs = set()
            for e in evs:
                p = self._adjust(e, tin)
                best[e["family"]] = max(best.get(e["family"], 0.0), p)
                srcs.add(e["source"])
            p_src = 1.0
            for v in best.values():
                p_src *= 1 - v
            p_src = 1 - p_src
            share = self._consensus(npi, tin, site)
            conf = 1 - (1 - p_src) * (1 - tc["weight"] * share)
            a = self.addr[site]
            notes = [f"{f} {v:.2f}" for f, v in sorted(best.items(), key=lambda kv: -kv[1])]
            if share:
                notes.append(f"TIN consensus {share:.0%}")
            elif tin_has_home:
                conf *= 1 - tc["absent_penalty"]
                notes.append("no other NPI of this TIN is here")
            if cfg["scope_state"] and a.state and a.state != cfg["scope_state"]:
                conf *= pen["out_of_state"]
                notes.append(f"outside {cfg['scope_state']}")
            if a.po_box:
                conf *= pen["po_box"]
                notes.append("PO box")
            families = {f for f, v in best.items() if v > 0}
            if share >= tc["add_site_min_share"]:
                families.add("tin_consensus")
            if not evs:
                notes.append("tin_consensus_only")
            verified = (len(families) >= ver["min_families"] or (ver["directory_alone"] and "directory" in families)
                        or conf >= ver["min_confidence"])
            scored.append({"npi": npi, "tin": tin, "site_key": site, "address": a.display(), "line1": a.line1,
                           "unit": a.unit, "city": a.city, "state": a.state, "zip5": a.zip5,
                           "confidence": round(conf, 4), "source_p": round(p_src, 4), "tin_consensus": round(share, 4),
                           "families": "|".join(sorted(families)), "sources": "|".join(sorted(srcs)),
                           "verified": bool(verified), "evidence": "; ".join(notes)})
        if not scored:
            return [self._empty(npi, tin, "no address in any source")], []
        scored.sort(key=lambda r: (-r["confidence"], r["site_key"]))
        top = scored[0]["confidence"]
        kept = [scored[0]] + [r for r in scored[1:sel["max_addresses_per_pair"]]
                              if r["confidence"] >= sel["secondary_min_confidence"]
                              and r["confidence"] >= sel["secondary_min_ratio"] * top]
        for i, r in enumerate(kept, start=1):
            r["rank"] = i
            r.update(self.geo(r["zip5"]))
        return kept, scored

    def _empty(self, npi, tin, why) -> dict:
        return {"npi": npi, "tin": tin, "rank": 1, "site_key": "", "address": "", "line1": "", "unit": "", "city": "",
                "state": "", "zip5": "", "confidence": 0.0, "source_p": 0.0, "tin_consensus": 0.0, "families": "",
                "sources": "", "verified": False, "evidence": why, "county": "", "region": ""}

    def geo(self, zip5: str) -> dict:
        county = self.zip_county.get(zip5, "")
        return {"county": county, "region": f"{county} County" if county else region_for(zip5, self.cfg["regions"])}
