"""PPO probability for every NPI + TIN pair (optional, `ptd_c6 run --ppo-model`).

C4 confirms a pair only by its own directory listing, so a pair whose NPI the Health Net finder
does not list is a candidate whatever else we know. This model gives each pair a probability of
being PPO, so candidates can be ranked and weighted instead of all being treated alike.

Labels are positive-unlabelled. The finder lists PPO providers only; a listing is a positive, but
"not found" is not a negative (the finder misses many real PPO providers, organisations and
behavioural health above all). So the model learns g(x) = P(listed | x) on every searched pair and
turns it into P(PPO | x) = g(x) / c, where c = P(listed | PPO) is estimated as the mean held-out g
over the listed pairs (Elkan and Noto, 2008). Pairs capitated in every reference are set to 0.

Features (all about the pair's surroundings, never its own search result, which is its label):
  ref_found_share   share of the *other* searched NPIs in the pair's provider references that are listed
  ref_searched      how many of them were searched (log)
  tin_found_share   the same for the other NPIs billing under the pair's TIN
  tin_searched      (log)
  ref_size          NPIs in the pair's largest reference (log), and TINs per NPI in it
  n_refs            references the pair sits in (log)
  capitated         one of its references has capitated prices (an HMO contract)
  npi_type_2        organisation NPI
  code_fit          share of the pair's billed codes the HiLabs workbook expects for its specialty
  specialty family and region (one-hot)
Rate amounts are left out on purpose: C7 scores cost on them, so using them here would be circular.
C5's "address verified" flag is left out too: an address is mostly verified by the same finder listing
that is the label, so it leaks the answer (held-out AUC 0.98 with it, almost all from that one flag).

Model: L2-regularised logistic regression (Newton steps, numpy only), features standardised,
5-fold cross-validation grouped by TIN for the held-out predictions and c.
"""
from __future__ import annotations

import csv
import math
import re
import zlib
from collections import defaultdict
from pathlib import Path

import numpy as np

FOUND = {"FOUND_PPO_BY_NPI", "FOUND_PPO_NPI_OTHER_TIN"}
FAMILIES = [
    ("behavioural", r"psych|counsel|therapist|social work|behavio|marriage|mental|addiction|substance"),
    ("hospital_based", r"emergency|radiolog|anesthes|patholog|hospitalist|neonat|critical care"),
    ("facility", r"facility|hospital|center|clinic|laborator|ambulatory|pharmac|skilled|urgent|home health|"
                 r"supplier|dme|261Q|282N|291U|3336|332B"),
    ("primary_care", r"primary care|family|internal medicine|general practice|pediatric"),
    ("therapy", r"physical therap|occupational|speech|chiropr|podiat|audiolog"),
    ("surgical", r"surg|orthop|urolog|otolaryng|ophthalm|obstet|gynec"),
    ("medical", r"cardio|dermat|gastro|neuro|oncol|endocr|nephro|pulmon|rheumat|allerg|infect|hematol"),
]


def family(specialty: str) -> str:
    s = str(specialty or "").lower()
    for name, rx in FAMILIES:
        if re.search(rx, s, re.IGNORECASE):
            return name
    return "other"


def read_check(path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        head = fh.readline()
        fh.seek(0)
        return list(csv.DictReader(fh, delimiter="\t" if head.count("\t") > head.count(",") else ","))


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def fit_logistic(X: np.ndarray, y: np.ndarray, w: np.ndarray, l2: float, iters: int = 50) -> np.ndarray:
    """Weighted logistic regression with an L2 penalty (not on the intercept, column 0)."""
    beta = np.zeros(X.shape[1])
    pen = np.full(X.shape[1], l2)
    pen[0] = 0.0
    for _ in range(iters):
        p = _sigmoid(X @ beta)
        grad = X.T @ (w * (y - p)) - pen * beta
        H = X.T @ (X * (w * p * (1 - p))[:, None]) + np.diag(pen) + 1e-9 * np.eye(X.shape[1])
        step = np.linalg.solve(H, grad)
        beta += step
        if np.abs(step).max() < 1e-7:
            break
    return beta


def auc(y: np.ndarray, s: np.ndarray, w: np.ndarray | None = None) -> float | None:
    """Area under the ROC curve (ties count half)."""
    pos, neg = s[y == 1], s[y == 0]
    if not len(pos) or not len(neg):
        return None
    order = np.argsort(np.concatenate([pos, neg]), kind="mergesort")
    ranks = np.empty(len(order))
    allv = np.concatenate([pos, neg])[order]
    i = 0
    while i < len(allv):  # average ranks over ties
        j = i
        while j + 1 < len(allv) and allv[j + 1] == allv[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return float((ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


class Features:
    """Builds the feature rows for NPI + TIN pairs from the rate store and the PPO check table."""

    def __init__(self, con, check_rows: list[dict], spec: dict, sp, cfg: dict):
        self.cfg = cfg
        self.members: dict[str, set] = defaultdict(set)
        self.ref_tins: dict[str, set] = defaultdict(set)
        self.pair_refs: dict[tuple, set] = defaultdict(set)
        self.tin_npis: dict[str, set] = defaultdict(set)
        for ref, npi, tin in con.execute("SELECT ref_id, npi, tin FROM c4_ref_members WHERE npi IS NOT NULL"):
            self.members[ref].add(int(npi))
            self.ref_tins[ref].add(tin)
            self.pair_refs[(int(npi), tin)].add(ref)
            self.tin_npis[tin].add(int(npi))
        self.capitated = {r for r, b in con.execute("SELECT ref_id, status_basis FROM c4_refs") if "capitation" in (b or "")}
        self.found, self.searched, self.label = set(), set(), {}
        for r in check_rows:
            try:
                npi = int(str(r.get("npi", "")).split(".")[0])
            except ValueError:
                continue
            if r.get("site_checked") != "yes" or r.get("npi_search_result") not in ("found", "not_found"):
                continue
            self.searched.add(npi)
            if r.get("verdict") in FOUND:
                self.found.add(npi)
            self.label[(npi, r.get("tin", ""))] = r.get("verdict", "")
        self.prior = len(self.found) / max(len(self.searched), 1)
        self.spec = spec
        self.sp = sp
        self.region = {}
        if con.execute("SELECT 1 FROM sqlite_master WHERE name = 'c5_addresses'").fetchone():
            for npi, tin, reg in con.execute("SELECT npi, tin, region FROM c5_addresses WHERE rank = 1"):
                self.region[(int(npi), tin)] = reg or ""
        self.codes: dict[tuple, set] = defaultdict(set)
        for npi, tin, code in con.execute("SELECT DISTINCT p.npi, p.tin, s.code FROM c4_pairs p JOIN c4_sig_rates s "
                                          "ON s.file_id = p.file_id AND s.sig_id = p.sig_id"):
            self.codes[(int(npi), tin)].add(code)
        regions = defaultdict(int)
        for v in self.region.values():
            regions[v] += 1
        self.regions = sorted(k for k, v in regions.items() if k and v >= cfg.get("min_region_pairs", 20))
        self.families = [f for f, _ in FAMILIES] + ["other"]
        self.names = (["ref_found_share", "ref_searched", "tin_found_share", "tin_searched", "ref_size",
                       "tins_per_npi", "n_refs", "capitated", "npi_type_2", "code_fit"]
                      + [f"family={f}" for f in self.families[:-1]] + [f"region={r}" for r in self.regions])

    def _share(self, others: set) -> tuple[float, int]:
        s = others & self.searched
        f = len(others & self.found)
        a = self.cfg.get("smoothing", 2.0)
        return (f + a * self.prior) / (len(s) + a), len(s)

    def row(self, npi: int, tin: str) -> list[float]:
        refs = self.pair_refs.get((npi, tin), set())
        ref_others = set().union(*(self.members[r] for r in refs)) - {npi} if refs else set()
        rs, rn = self._share(ref_others)
        ts, tn = self._share(self.tin_npis.get(tin, set()) - {npi})
        big = max(refs, key=lambda r: (len(self.members[r]), r)) if refs else None
        size = len(self.members[big]) if big else 1
        tpn = len(self.ref_tins[big]) / size if big else 1.0
        s = self.spec.get(npi, {})
        fam = family(s.get("specialty", ""))
        exp = [self.sp.code_expected(s.get("spec_cd") or "", c) for c in self.codes.get((npi, tin), ())] \
            if self.sp is not None and s.get("spec_cd") else []
        exp = [e for e in exp if e is not None]
        fit = sum(exp) / len(exp) if exp else 0.5
        reg = self.region.get((npi, tin), "")
        return ([rs, math.log1p(rn), ts, math.log1p(tn), math.log1p(size), tpn, math.log1p(len(refs)),
                 float(bool(refs & self.capitated)), float(str(s.get("npi_type")) == "2"), fit]
                + [float(fam == f) for f in self.families[:-1]] + [float(reg == r) for r in self.regions])


def _fold(tin: str, k: int) -> int:
    return zlib.crc32(str(tin).encode()) % k


def train_and_score(feats: Features, pairs: list[tuple], cfg: dict, log=print) -> tuple[dict, dict]:
    """Returns ({pair: result}, report). Trains on the searched pairs, scores every pair in `pairs`."""
    l2, k = cfg.get("l2", 1.0), cfg.get("folds", 5)
    other_tin_w = cfg.get("other_tin_weight", 0.5)
    train = [p for p in feats.label if p in feats.pair_refs]
    every = sorted(set(pairs) | set(train))
    X_raw = np.array([feats.row(n, t) for n, t in every], dtype=float)
    mu, sd = X_raw.mean(axis=0), X_raw.std(axis=0)
    sd[sd == 0] = 1.0
    X = np.hstack([np.ones((len(every), 1)), (X_raw - mu) / sd])
    idx = {p: i for i, p in enumerate(every)}
    ti = np.array([idx[p] for p in train], dtype=int)
    verdict = [feats.label[p] for p in train]
    s = np.array([1.0 if v in FOUND else 0.0 for v in verdict])
    w = np.array([other_tin_w if v == "FOUND_PPO_NPI_OTHER_TIN" else 1.0 for v in verdict])
    folds = np.array([_fold(t, k) for _, t in train])
    oof = np.zeros(len(train))
    for f in range(k):
        tr, te = folds != f, folds == f
        if te.any() and tr.any() and s[tr].min() != s[tr].max():
            oof[te] = _sigmoid(X[ti[te]] @ fit_logistic(X[ti[tr]], s[tr], w[tr], l2))
    beta = fit_logistic(X[ti], s, w, l2)
    same_tin = np.array([v == "FOUND_PPO_BY_NPI" for v in verdict])
    c = float(oof[same_tin].mean()) if same_tin.any() else 1.0
    c = min(max(c, cfg.get("min_c", 0.2)), 1.0)
    g_all = _sigmoid(X @ beta)
    contrib = X[:, 1:] * beta[1:]
    out = {}
    for p in pairs:
        i = idx[p]
        top = np.argsort(-np.abs(contrib[i]))[:3]
        reasons = "; ".join(f"{feats.names[j]}={X_raw[i, j]:.3g} {'+' if contrib[i, j] > 0 else '-'}{abs(contrib[i, j]):.2f}"
                            for j in top)
        out[p] = {"g": float(g_all[i]), "model_p": float(min(1.0, g_all[i] / c)), "reasons": reasons}
    # held-out reliability of g against "listed": predicted vs observed per decile
    bins = []
    if len(oof):
        edges = np.quantile(oof, np.linspace(0, 1, 6))
        for a, b in zip(edges[:-1], edges[1:]):
            m = (oof >= a) & (oof <= b)
            if m.any():
                bins.append({"g_from": round(float(a), 3), "g_to": round(float(b), 3), "pairs": int(m.sum()),
                             "mean_g": round(float(oof[m].mean()), 3), "listed_share": round(float(s[m].mean()), 3)})
    report = {"train_pairs": len(train), "listed": int(s.sum()), "listed_same_tin": int(same_tin.sum()),
              "c_listed_given_ppo": round(c, 4), "cv_auc_listed_vs_rest": auc(s, oof),
              "coefficients": {n: round(float(b), 4) for n, b in zip(feats.names, beta[1:])},
              "intercept": round(float(beta[0]), 4), "reliability": bins, "prior_listed_share": round(feats.prior, 4)}
    log(f"[c6]   PPO model: {len(train):,} searched pairs ({int(s.sum()):,} listed), held-out AUC "
        f"{report['cv_auc_listed_vs_rest'] and round(report['cv_auc_listed_vs_rest'], 3)}, "
        f"c = P(listed | PPO) = {c:.3f}")
    return out, report


def final_probability(status: str, model_p: float) -> float:
    """Observed evidence first: confirmed pairs are 1, capitated non-PPO pairs 0; the rest the model's."""
    if status == "CONFIRMED_PPO":
        return 1.0
    if status == "CONFIRMED_NON_PPO":
        return 0.0
    return round(model_p, 4)


def write_report(path: Path, report: dict, dist: dict) -> None:
    L = ["# PPO probability model", "",
         "Positive-unlabelled logistic regression (see ptd_c6/ppo_model.py). g = P(listed on the finder | features), "
         "P(PPO) = g / c with c = P(listed | PPO) estimated on held-out listed pairs.", "",
         f"- Searched pairs used for training: {report['train_pairs']:,} ({report['listed']:,} listed, "
         f"{report['listed_same_tin']:,} under the same TIN)",
         f"- Held-out AUC, listed vs not (5 folds grouped by TIN): {report['cv_auc_listed_vs_rest']}",
         f"- c = P(listed | PPO): {report['c_listed_given_ppo']}", "",
         "## Held-out reliability (g vs observed listing share)", "", "| g from | g to | pairs | mean g | listed share |",
         "|---|---|---|---|---|"]
    L += [f"| {b['g_from']} | {b['g_to']} | {b['pairs']} | {b['mean_g']} | {b['listed_share']} |"
          for b in report["reliability"]]
    L += ["", "## Coefficients (standardised features)", "", "| feature | coefficient |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in sorted(report["coefficients"].items(), key=lambda kv: -abs(kv[1]))]
    L += ["", "## PPO probability by network status (pairs with rates)", "", "| status | pairs | mean P | P >= 0.5 | P >= 0.8 |",
          "|---|---|---|---|---|"]
    L += [f"| {k} | {v['pairs']} | {v['mean']} | {v['ge_05']} | {v['ge_08']} |" for k, v in dist.items()]
    path.write_text("\n".join(L) + "\n")
