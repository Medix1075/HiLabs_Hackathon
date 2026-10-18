"""Outside dataset analysis: factor support (method 1) and transfer model (method 2)."""
import csv, json, math, os, sqlite3, sys, collections, re, zlib
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pipeline"))
import yaml
from ptd_c5.resolve import region_for
from build import family, REG

EXT = os.environ.get("PPO_EXT", os.path.dirname(os.path.abspath(__file__)) + "/work/")
DB = os.environ.get("PPO_DB", "pipeline/out/rates.sqlite")
CHECK = os.environ.get("PPO_CHECK", "pipeline/out/c4/c4_ppo_check.csv")
MODEL = os.environ.get("PPO_MODEL_CSV", "pipeline/out/c6/c6_ppo_probability.csv")

def rd(p): return list(csv.DictReader(open(p)))

def wrate(rows, key, w="w"):
    s = sum(float(r[w]) for r in rows); return sum(float(r[w]) * int(r[key]) for r in rows) / s if s else float("nan")

def ci(p, n):
    if n == 0: return (float("nan"),) * 2
    z = 1.96; d = 1 + z*z/n; c = (p + z*z/(2*n)) / d; h = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / d
    return c - h, c + h

def fit(X, y, w, l2=1.0, it=60):
    b = np.zeros(X.shape[1])
    for _ in range(it):
        p = 1/(1+np.exp(-X@b)); g = X.T@(w*(p-y)) + l2*np.r_[0, b[1:]]
        H = (X*(w*p*(1-p))[:, None]).T@X + l2*np.diag(np.r_[0, np.ones(len(b)-1)])
        b -= np.linalg.solve(H, g)
    return b

def auc(y, s):
    y = np.asarray(y); s = np.asarray(s); o = np.argsort(s); r = np.empty(len(s)); r[o] = np.arange(1, len(s)+1)
    # ties: average ranks
    _, inv, cnt = np.unique(s, return_inverse=True, return_counts=True)
    sums = np.bincount(inv, r); r = (sums/cnt)[inv]
    n1 = y.sum(); n0 = len(y) - n1
    return (r[y == 1].sum() - n1*(n1+1)/2) / (n1*n0) if n1 and n0 else float("nan")

def ours_features():
    c = sqlite3.connect(DB)
    et = {str(n): str(t or "") for n, t in c.execute("SELECT npi, entity_type FROM c5_providers")}
    spec = {}
    for n, s in c.execute("SELECT npi, specialty FROM c6_rates WHERE specialty != '' GROUP BY npi"):
        spec[str(n)] = s
    for n, d, t in c.execute("SELECT npi, dac_specialty, primary_taxonomy FROM c5_providers"):
        spec.setdefault(str(n), (d or "") + " " + (t or ""))
    reg = {}
    for n, r in c.execute("SELECT npi, region FROM c5_addresses WHERE rank = 1"):
        reg.setdefault(str(n), r or "")
    tins, refs, members = collections.defaultdict(set), collections.defaultdict(set), collections.defaultdict(set)
    tin_npis = collections.defaultdict(set)
    for ref, n, t in c.execute("SELECT ref_id, npi, tin FROM c4_ref_members WHERE npi IS NOT NULL"):
        n = str(n); tins[n].add(t); refs[n].add(ref); members[ref].add(n); tin_npis[t].add(n)
    cap = {r for r, b in c.execute("SELECT ref_id, status_basis FROM c4_refs") if "capitation" in (b or "")}
    return et, spec, reg, tins, refs, members, tin_npis, cap

def main():
    ours = {r["npi"]: r for r in rd(EXT + "ours.csv")}
    smp_tot = {l.split("\t")[0]: int(l.split("\t")[2]) for l in open(EXT + "sample_npis.txt") if l.strip()}
    S = [r for r in rd(EXT + "sample.csv") if r["npi"] not in ours and r["listed_commercial"] == "1" and r["error"] == "0"]
    for r in S:
        r["w"] = smp_tot.get(r["npi"], 1) / max(int(r["n_gr_records"]), 1)
    tw = sum(r["w"] for r in S); 
    for r in S: r["w"] = r["w"] * len(S) / tw
    out = {"sample": {}, "ours": {}, "transfer": {}}
    # ---------------- method 1: factor tables on the outside sample
    def table(rows, fn, label="ppo"):
        g = collections.defaultdict(list)
        for r in rows: g[fn(r)].append(r)
        res = []
        for k, rr in sorted(g.items(), key=lambda x: -len(x[1])):
            n = len(rr); p = wrate(rr, label); lo, hi = ci(sum(int(x[label]) for x in rr)/n, n)
            res.append({"level": k, "n": n, "ppo_share": round(p, 3), "ci_lo": round(lo, 3), "ci_hi": round(hi, 3),
                        "hmo_share": round(wrate(rr, "hmo"), 3)})
        return res
    out["sample"]["n"] = len(S)
    out["sample"]["overall"] = {"ppo": round(wrate(S, "ppo"), 3), "hmo": round(wrate(S, "hmo"), 3),
                                "both": round(sum(r["w"]*(r["ppo"]=="1" and r["hmo"]=="1") for r in S)/len(S), 3),
                                "ppo_only": round(sum(r["w"]*(r["ppo"]=="1" and r["hmo"]=="0") for r in S)/len(S), 3),
                                "hmo_only": round(sum(r["w"]*(r["ppo"]=="0" and r["hmo"]=="1") for r in S)/len(S), 3),
                                "neither": round(sum(r["w"]*(r["ppo"]=="0" and r["hmo"]=="0") for r in S)/len(S), 3)}
    out["sample"]["npi_type"] = table(S, lambda r: "Organisation (Type 2)" if r["npi_type_2"] == "1" else "Individual (Type 1)")
    out["sample"]["family"] = table(S, lambda r: r["family"] or "unknown")
    out["sample"]["region"] = table(S, lambda r: r["region"] or "unknown")
    out["sample"]["capitated_ppg"] = table(S, lambda r: "In a capitated medical group" if r["capitated_ppg"] == "1" else "Not")
    out["sample"]["n_tins"] = table(S, lambda r: "2+ TINs" if int(r["n_tins"]) >= 2 else "1 TIN")
    # multivariable on sample
    fams = ["behavioural", "hospital_based", "facility", "primary_care", "therapy", "surgical", "medical"]
    regs = sorted({r["region"] for r in S if r["region"]} )
    regs = [x for x in regs if sum(1 for r in S if r["region"] == x) >= 20]
    names = ["npi_type_2", "two_plus_tins", "capitated"] + [f"family={f}" for f in fams] + [f"region={x}" for x in regs]
    def xrow(t2, ntin, capd, fam, rg):
        return [1.0, float(t2), float(ntin >= 2), float(capd)] + [float(fam == f) for f in fams] + [float(rg == x) for x in regs]
    X = np.array([xrow(r["npi_type_2"] == "1", int(r["n_tins"]), r["capitated_ppg"] == "1", r["family"], r["region"]) for r in S])
    y = np.array([int(r["ppo"]) for r in S]); w = np.array([r["w"] for r in S])
    mu, sd = X[:, 1:].mean(0), X[:, 1:].std(0) + 1e-9
    Xs = X.copy(); Xs[:, 1:] = (X[:, 1:] - mu) / sd
    b = fit(Xs, y, w)
    out["sample"]["coef"] = sorted([(names[i], round(float(b[i+1]), 3)) for i in range(len(names))], key=lambda x: -abs(x[1]))
    # 5-fold CV AUC on sample (grouped by first TIN)
    folds = np.array([hash((r["tins"].split("|")[0] or r["npi"])) % 5 for r in S]); pr = np.zeros(len(S))
    for k in range(5):
        tr = folds != k; bk = fit(Xs[tr], y[tr], w[tr]); pr[~tr] = 1/(1+np.exp(-Xs[~tr]@bk))
    out["sample"]["cv_auc"] = round(auc(y, pr), 3)
    # ---------------- ours: real labels for every rate-file NPI
    et, spec, reg, tins, refs, members, tin_npis, cap = ours_features()
    O = [r for r in ours.values() if r["error"] == "0"]
    lab = {r["npi"]: int(r["ppo"]) for r in O}
    known = {r["npi"] for r in O if r["listed_any"] == "1"}
    chk = {}
    for r in rd(CHECK):
        if r["site_checked"] == "yes": chk[r["npi"]] = r["verdict"]
    out["ours"]["n"] = len(O)
    out["ours"]["labels"] = collections.Counter(("PPO" if r["ppo"] == "1" else "not PPO") + " / " +
                                                ("HMO" if r["hmo"] == "1" else "no HMO") + " / " +
                                                ("listed somewhere" if r["listed_any"] == "1" else "not in directory") for r in O)
    out["ours"]["browser_vs_backend"] = {f"{a} | {b}": v for (a, b), v in collections.Counter((chk[n], "PPO" if lab[n] else "not PPO") for n in lab if n in chk).items()}
    # display (findability) among PPO
    out["ours"]["display_given_ppo"] = {}
    for t in ("1", "2"):
        pp = [n for n in lab if lab[n] and et.get(n) == t and n in chk]
        found = [n for n in pp if chk[n] in ("FOUND_PPO_BY_NPI", "FOUND_PPO_NPI_OTHER_TIN")]
        out["ours"]["display_given_ppo"][t] = (len(found), len(pp))
    def otable(fn, rows=None):
        g = collections.defaultdict(list)
        for n in (rows or lab):
            g[fn(n)].append(lab[n])
        res = []
        for k, v in sorted(g.items(), key=lambda x: -len(x[1])):
            p = sum(v)/len(v); lo, hi = ci(p, len(v)); res.append({"level": k, "n": len(v), "ppo_share": round(p, 3), "ci_lo": round(lo, 3), "ci_hi": round(hi, 3)})
        return res
    def share_bucket(others):
        o = [lab[x] for x in others if x in lab]
        if not o: return "no other NPIs"
        s = sum(o)/len(o)
        return "0-25% PPO" if s < .25 else "25-50%" if s < .5 else "50-75%" if s < .75 else "75-100% PPO"
    out["ours"]["npi_type"] = otable(lambda n: {"1": "Individual (Type 1)", "2": "Organisation (Type 2)"}.get(et.get(n), "unknown"))
    out["ours"]["family"] = otable(lambda n: family(spec.get(n, "")) if spec.get(n, "").strip() else "unknown")
    out["ours"]["capitated"] = otable(lambda n: "capitated contract" if refs[n] & cap else "no capitation")
    out["ours"]["n_tins"] = otable(lambda n: "2+ TINs" if len(tins[n]) >= 2 else "1 TIN")
    out["ours"]["contract_share"] = otable(lambda n: share_bucket(set().union(*(members[r] for r in refs[n])) - {n}))
    out["ours"]["tin_share"] = otable(lambda n: share_bucket(set().union(*(tin_npis[t] for t in tins[n])) - {n}))
    out["ours"]["contract_size"] = otable(lambda n: (lambda s: "1-5 NPIs" if s <= 5 else "6-50" if s <= 50 else "51+")(max(len(members[r]) for r in refs[n])))
    # ---------------- method 2: transfer the sample model to ours (same directory-derived features on both sides)
    npis = [n for n in lab if n in known]
    ox = np.array([xrow(ours[n]["npi_type_2"] == "1", int(ours[n]["n_tins"]), ours[n]["capitated_ppg"] == "1",
                        ours[n]["family"], ours[n]["region"]) for n in npis])
    oxs = ox.copy(); oxs[:, 1:] = (ox[:, 1:] - mu) / sd
    po = 1/(1+np.exp(-oxs@b)); oy = np.array([lab[n] for n in npis])
    out["transfer"]["n_eval"] = len(npis)
    out["transfer"]["auc_sample_model"] = round(auc(oy, po), 3)
    out["transfer"]["mean_pred"] = round(float(po.mean()), 3); out["transfer"]["actual"] = round(float(oy.mean()), 3)
    # same simple features trained on ours (5 folds by TIN)
    def tin_of(n): return sorted(tins[n])[0] if tins[n] else n
    f2 = np.array([zlib.crc32(tin_of(n).encode()) % 5 for n in npis])
    def cv(Xm):
        pr = np.zeros(len(npis))
        for k in range(5):
            tr = f2 != k; bk = fit(Xm[tr], oy[tr], np.ones(tr.sum())); pr[~tr] = 1/(1+np.exp(-Xm[~tr]@bk))
        return pr
    # contract and TIN neighbour shares (leave-one-out, real labels) alone and with the simple features
    def loo(others):
        o = [lab[x] for x in others if x in lab]
        return ((sum(o) + 2*0.86) / (len(o) + 2), math.log1p(len(o)))
    ref_f = np.array([loo(set().union(*(members[r] for r in refs[n])) - {n}) for n in npis])
    tin_f = np.array([loo(set().union(*(tin_npis[t] for t in tins[n])) - {n}) for n in npis])
    size_f = np.array([[math.log1p(max(len(members[r]) for r in refs[n]))] for n in npis])
    out["transfer"]["auc_contract_share_alone"] = round(auc(oy, ref_f[:, 0]), 3)
    out["transfer"]["auc_tin_share_alone"] = round(auc(oy, tin_f[:, 0]), 3)
    full = np.c_[oxs, (ref_f - ref_f.mean(0)) / (ref_f.std(0) + 1e-9), (tin_f - tin_f.mean(0)) / (tin_f.std(0) + 1e-9),
                 (size_f - size_f.mean(0)) / (size_f.std(0) + 1e-9)]
    del full  # directory-derived simple features include the PPO record itself (leak), so only rate-file features below
    pipe = np.c_[np.ones(len(npis)), ref_f, tin_f, size_f,
                 [[float(bool(refs[n] & cap)), float(et.get(n) == "2"), float(et.get(n, "") == "")] for n in npis]]
    pipe[:, 1:] = (pipe[:, 1:] - pipe[:, 1:].mean(0)) / (pipe[:, 1:].std(0) + 1e-9)
    out["transfer"]["auc_ratefile_features_trained_on_ours"] = round(auc(oy, cv(pipe)), 3)
    # in-domain model and rule status against the real labels
    mp, st = {}, {}
    for r in rd(MODEL):
        mp[r["npi"]] = max(mp.get(r["npi"], 0), float(r["model_p"] or 0))
        st[r["npi"]] = max(st.get(r["npi"], 0), {"CONFIRMED_PPO": 3, "PPO_CANDIDATE": 2, "UNRESOLVED": 1}.get(r["network_status"], 0))
    both = [n for n in npis if n in mp]
    yy = np.array([lab[n] for n in both])
    out["transfer"]["n_compare"] = len(both)
    out["transfer"]["auc_inhouse_model"] = round(auc(yy, np.array([mp[n] for n in both])), 3)
    out["transfer"]["auc_rule_status"] = round(auc(yy, np.array([st[n] for n in both])), 3)
    out["transfer"]["auc_sample_model_same_npis"] = round(auc(yy, np.array([po[npis.index(n)] for n in both])), 3)
    out["transfer"]["auc_inhouse_vs_listed"] = None
    json.dump(out, open(EXT + "results.json", "w"), indent=1, default=str)
    # dataset files
    with open(EXT + "outside_sample_dataset.csv", "w", newline="") as f:
        cols = ["npi", "w", "ppo", "hmo", "epo_hsp_eoa", "listed_commercial", "n_commercial_networks", "npi_type_2", "ptype",
                "family", "specialty", "taxonomy", "region", "n_tins", "capitated_ppg", "in_ppg"]
        wr = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); wr.writeheader()
        for r in S: wr.writerow({**r, "w": round(r["w"], 4)})
    with open(EXT + "rate_file_npis_labelled.csv", "w", newline="") as f:
        wr = csv.writer(f); wr.writerow(["npi", "ppo_backend", "hmo_backend", "listed_any", "browser_verdict", "npi_type", "family", "region",
                                         "n_tins", "capitated_contract", "sample_model_p", "inhouse_model_p"])
        for i, n in enumerate(npis):
            r = ours[n]
            wr.writerow([n, r["ppo"], r["hmo"], r["listed_any"], chk.get(n, ""), et.get(n, ""), family(spec.get(n, "")), reg.get(n, ""),
                         len(tins[n]), int(bool(refs[n] & cap)), round(float(po[i]), 4), mp.get(n, "")])
    print(json.dumps({k: v for k, v in out.items()}, indent=1, default=str)[:6000])

if __name__ == "__main__":
    main()
