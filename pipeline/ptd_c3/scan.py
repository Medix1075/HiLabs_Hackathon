"""One streaming pass over a rate file, counting everything the C3 metrics need.

The file is read with ijson event by event; each provider_references item and each in_network
item is built as a small object, measured, and dropped, so memory grows with the number of
provider groups and distinct codes, never with the file size. Counts are kept per bucket:
"ALL" (the whole file) plus one bucket per provider tag when a tagger is given (see tags.py).
"""
from __future__ import annotations

import math
import re
import statistics
import time
from collections import Counter, defaultdict

import ijson
from ijson.common import ObjectBuilder

from ptd_c2.stream import ByteCapReached, open_stream

from .codes import norm_code
from .tags import ALL, ProviderTagger

PR, IN = "provider_references.item", "in_network.item"
TOP_FIELDS = ("reporting_entity_name", "reporting_entity_type", "last_updated_on", "version",
              "plan_name", "plan_id", "plan_market_type")
DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def npi_is_valid(npi) -> bool:
    """NPI check digit: Luhn over '80840' + the first nine digits (CMS NPI standard)."""
    s = str(npi).strip()
    if not re.fullmatch(r"\d{10}", s):
        return False
    total = 0
    for i, ch in enumerate(reversed("80840" + s[:9])):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            d = d - 9 if d > 9 else d
        total += d
    return (10 - total % 10) % 10 == int(s[9])


class Acc:
    """Counts for one bucket of one file."""

    def __init__(self):
        self.groups = self.groups_tin_only = self.groups_placeholder_tin = self.groups_invalid_npi = 0
        self.npis: set = set()
        self.tins: set = set()
        self.items = 0
        self.codes: set = set()
        self.code_types = Counter()
        self.arrangements = Counter()
        self.rows = 0                  # negotiated_prices entries (one price row each)
        self.neg_types = Counter()
        self.zero = self.outliers = self.outlier_tested = self.dups = 0
        self.expired = self.evergreen = self.catch_all = 0
        self.resolved = 0              # rows tied to at least one provider group with a valid NPI


class FileScan:
    def __init__(self, url: str, cfg: dict, tagger: ProviderTagger | None = None, log=print):
        self.url, self.cfg, self.log = url, cfg, log
        self.tagger = tagger or ProviderTagger()
        self.acc: dict[str, Acc] = defaultdict(Acc)
        self.top: dict = {}
        self.refs: dict = {}            # provider_group_id -> (tags, has_valid_npi)
        self.refs_ready = False         # provider_references fully read before in_network started
        self.order: list[str] = []
        self.pending = Counter()        # refs tuple -> price rows, when in_network comes first
        self.schema_checked = 0
        self.schema_errors = Counter()  # kind -> records
        self.examples: dict[str, list] = defaultdict(list)
        self.location_fetched = self.location_skipped = 0
        self.blank_network_names = 0
        self.dedup: set = set()
        self.dedup_scope = "file"
        self.as_of = cfg["_as_of"].isoformat()
        self.status, self.error, self.stop_reason = "ok", "", "end_of_file"
        self.bytes_read = 0
        self.seconds = 0.0

    # ------------------------------------------------------------------ helpers
    def _err(self, kind: str, n: int = 1, **example):
        self.schema_errors[kind] += n
        if example:
            self._example("schema:" + kind, **example)

    def _example(self, issue: str, **detail):
        ex = self.examples[issue]
        if len(ex) < self.cfg["examples_per_issue"]:
            ex.append({k: (str(v)[:200] if v is not None else "") for k, v in detail.items()})

    def _buckets(self, tags) -> list[Acc]:
        return [self.acc[ALL]] + [self.acc[t] for t in tags]

    # ----------------------------------------------------------- provider groups
    def _group(self, g: dict) -> tuple[frozenset, bool]:
        self.schema_checked += 1
        if not isinstance(g, dict):
            self._err("provider_group_not_object", detail=g)
            return frozenset(), False
        npis = g.get("npi")
        tin = g.get("tin") if isinstance(g.get("tin"), dict) else None
        if not isinstance(npis, list):
            self._err("npi_not_list", detail=npis)
            npis = [] if npis is None else [npis]
        if tin is None:
            self._err("tin_missing", detail=g.get("tin"))
            tin_value = None
        else:
            tin_value = str(tin.get("value") or "").strip()
            if str(tin.get("type") or "").lower() not in self.cfg["schema"]["_tin_types"]:
                self._err("tin_type_invalid", detail=tin.get("type"))
        real = [n for n in npis if str(n).strip() not in ("", "0")]
        valid = [int(n) for n in real if npi_is_valid(n)]
        tin_only = not real
        placeholder = tin_value in self.cfg["junk"]["_placeholder_tins"]
        invalid = len(valid) < len(real)
        if tin_only:
            self._example("junk:tin_only_group", tin=tin_value, npi=npis[:5])
        if placeholder:
            self._example("junk:placeholder_tin", tin=tin_value)
        if invalid:
            self._example("junk:invalid_npi", npi=[n for n in real if not npi_is_valid(n)][:5], tin=tin_value)
        tags = self.tagger.group_tags(valid, tin_value) if self.tagger.enabled else frozenset()
        for a in self._buckets(tags):
            a.groups += 1
            a.groups_tin_only += tin_only
            a.groups_placeholder_tin += placeholder
            a.groups_invalid_npi += invalid
            a.npis.update(valid)
            if tin_value:
                a.tins.add(tin_value)
        return tags, bool(valid)

    def _provider_reference(self, ref: dict) -> None:
        self.schema_checked += 1
        pid = ref.get("provider_group_id")
        if pid is None:
            self._err("provider_group_id_missing", detail=list(ref)[:5])
        if self.cfg["schema"]["network_name_required"]:
            nn = ref.get("network_name")
            names = nn if isinstance(nn, list) else [nn]
            if not any(str(n or "").strip() for n in names):
                self.blank_network_names += 1
                self._err("network_name_blank", provider_group_id=pid)
        groups = ref.get("provider_groups")
        if groups is None and ref.get("location"):
            groups = self._fetch_location(ref["location"])
        elif groups is None:
            self._err("provider_groups_and_location_missing", provider_group_id=pid)
        tags, valid = set(), False
        for g in groups or []:
            t, v = self._group(g)
            tags |= t
            valid = valid or v
        if pid is not None:
            self.refs[pid] = (frozenset(tags), valid)

    def _fetch_location(self, loc: str):
        sc = self.cfg["stream"]
        if self.location_fetched >= sc["max_location_fetches"]:
            self.location_skipped += 1
            return None
        self.location_fetched += 1
        try:
            with open_stream(loc, timeout=sc["timeout_seconds"], retries=sc["retries"],
                             user_agent=sc["user_agent"]) as (fh, _):
                return list(ijson.items(fh, "provider_groups.item", use_float=True))
        except Exception as exc:
            self._err("location_fetch_failed", location=loc, detail=f"{exc.__class__.__name__}: {exc}")
            return None

    # ----------------------------------------------------------- in_network items
    def _item(self, item: dict) -> None:
        cfg, sch = self.cfg, self.cfg["schema"]
        self.schema_checked += 1
        missing = [k for k in sch["item_required"] if item.get(k) in (None, "")]
        code_type = str(item.get("billing_code_type") or "").upper()
        code = norm_code(item.get("billing_code"))
        if missing:
            self._err("item_field_missing", billing_code=code, detail=missing)
        arrangement = str(item.get("negotiation_arrangement") or "").lower()
        if arrangement and arrangement not in sch["_arrangements"]:
            self._err("arrangement_invalid", billing_code=code, detail=arrangement)
        if arrangement == "bundle" and not item.get("bundled_codes") and not item.get("covered_services"):
            self._err("bundle_without_codes", billing_code=code)
        if code_type and code_type not in sch["_billing_code_types"]:
            self._err("billing_code_type_invalid", billing_code=code, detail=code_type)
        fmt = cfg["_code_formats"].get(code_type)
        if fmt and code and not fmt.match(code):
            self._err("billing_code_format", billing_code=code, detail=code_type)
        catch_all = code_type in cfg["junk"]["_catch_all_types"] or any(p.match(code) for p in cfg["_catch_all_patterns"])
        if catch_all:
            self._example("junk:catch_all_code", billing_code=code, billing_code_type=code_type)

        rates_cfg, out_cfg = cfg["rates"], cfg["outliers"]
        touched: dict[int, Acc] = {}
        groups_for_outliers: dict[tuple, list] = defaultdict(list)
        item_dedup = set()
        for nr in item.get("negotiated_rates") or []:
            self.schema_checked += 1
            if not isinstance(nr, dict):
                self._err("negotiated_rate_not_object", billing_code=code)
                continue
            refs = nr.get("provider_references")
            inline = nr.get("provider_groups")
            if refs is None and inline is None:
                self._err("rate_without_providers", billing_code=code)
            refs = [r for r in (refs or [])]
            tags, resolved, deferred = set(), False, False
            if refs:
                if self.refs_ready:
                    missing_refs = [r for r in refs if r not in self.refs]
                    if missing_refs:
                        self._err("dangling_provider_reference", billing_code=code, detail=missing_refs[:5])
                    for r in refs:
                        t, v = self.refs.get(r, (frozenset(), False))
                        tags |= t
                        resolved = resolved or v
                else:
                    deferred = True
            for g in inline or []:
                t, v = self._group(g)
                tags |= t
                resolved = resolved or v
            buckets = self._buckets(tags)
            for a in buckets:
                touched[id(a)] = a
            prices = nr.get("negotiated_prices") or []
            if not prices:
                self._err("negotiated_prices_empty", billing_code=code)
            if deferred:
                self.pending[tuple(sorted(refs, key=str))] += len(prices)
            prov_key = (tuple(sorted(refs, key=str)), repr(inline) if inline else "")
            for p in prices:
                self._price(p, item, code, code_type, arrangement, catch_all, buckets, resolved, deferred,
                            prov_key, item_dedup, groups_for_outliers, rates_cfg)
        for a in touched.values():
            a.items += 1
            if code:
                a.codes.add(code)
            a.code_types[code_type] += 1
            a.arrangements[arrangement] += 1
        self._outliers(groups_for_outliers, code, out_cfg)

    def _price(self, p, item, code, code_type, arrangement, catch_all, buckets, resolved, deferred, prov_key,
               item_dedup, groups_for_outliers, rates_cfg):
        sch = self.cfg["schema"]
        self.schema_checked += 1
        if not isinstance(p, dict):
            self._err("negotiated_price_not_object", billing_code=code)
            return
        missing = [k for k in sch["price_required"] if p.get(k) in (None, "")]
        bclass = str(p.get("billing_class") or "").lower()
        if bclass == "professional" and not p.get("service_code"):
            missing.append("service_code")
        if missing:
            self._err("price_field_missing", billing_code=code, detail=missing)
        ntype = str(p.get("negotiated_type") or "").lower()
        if ntype and ntype not in sch["_negotiated_types"]:
            self._err("negotiated_type_invalid", billing_code=code, detail=ntype)
        if bclass and bclass not in sch["_billing_classes"]:
            self._err("billing_class_invalid", billing_code=code, detail=bclass)
        setting = str(p.get("setting") or "").lower()
        if setting and setting not in sch["_settings"]:
            self._err("setting_invalid", billing_code=code, detail=setting)
        rate = p.get("negotiated_rate")
        if rate is not None and not isinstance(rate, (int, float)):
            try:
                rate = float(rate)
            except (TypeError, ValueError):
                self._err("negotiated_rate_not_number", billing_code=code, detail=rate)
                rate = None
        exp = str(p.get("expiration_date") or "")
        if exp and not DATE_RX.match(exp):
            self._err("expiration_date_format", billing_code=code, detail=exp)
        evergreen = exp in self.cfg["rates"]["evergreen_dates"]
        expired = bool(exp) and DATE_RX.match(exp) is not None and not evergreen and exp < self.as_of
        zero = rate is not None and rate <= 0
        modifiers = tuple(sorted(str(m) for m in (p.get("billing_code_modifier") or [])))
        svc = tuple(sorted(str(s) for s in (p.get("service_code") or [])))

        key = hash((code_type, code, arrangement, prov_key, ntype, rate, bclass, setting, svc, modifiers, exp))
        if self.dedup_scope == "file":
            dup = key in self.dedup
            if not dup:
                self.dedup.add(key)
                if len(self.dedup) >= self.cfg["stream"]["dedup_max_keys"]:
                    self.dedup_scope = "item (file-wide key cap reached)"
                    self.dedup.clear()
        else:
            dup = key in item_dedup
        item_dedup.add(key)

        if zero:
            self._example("zero_rate", billing_code=code, negotiated_type=ntype, rate=rate)
        if expired:
            self._example("expired", billing_code=code, expiration_date=exp)
        if dup:
            self._example("duplicate", billing_code=code, negotiated_type=ntype, rate=rate)
        pct_outlier = ntype in rates_cfg["_percentage"] and rate is not None and rate > rates_cfg["max_percentage"]
        if pct_outlier:
            self._example("outlier", billing_code=code, negotiated_type=ntype, rate=rate, detail="percentage above max")
        for a in buckets:
            a.rows += 1
            a.neg_types[ntype or "(missing)"] += 1
            a.zero += zero
            a.expired += expired
            a.evergreen += evergreen
            a.dups += dup
            a.catch_all += catch_all
            a.resolved += resolved and not deferred
            if pct_outlier:
                a.outliers += 1
                a.outlier_tested += 1
        if rate is not None and rate > 0 and not pct_outlier and (
                ntype in rates_cfg["_dollar"] or ntype in rates_cfg["_per_diem"] or ntype in rates_cfg["_percentage"]):
            groups_for_outliers[(ntype, bclass, setting, modifiers)].append((rate, buckets))

    def _outliers(self, groups: dict, code: str, oc: dict) -> None:
        for (ntype, *_), vals in groups.items():
            if len(vals) < oc["min_group_size"]:
                continue
            logs = [math.log(v) for v, _ in vals]
            q1, _, q3 = statistics.quantiles(logs, n=4, method="inclusive")
            med = statistics.median(logs)
            lo, hi = q1 - oc["iqr_k"] * (q3 - q1), q3 + oc["iqr_k"] * (q3 - q1)
            min_log_ratio = math.log(oc["min_ratio_to_median"])
            for (v, buckets), lv in zip(vals, logs):
                out = (lv < lo or lv > hi) and abs(lv - med) >= min_log_ratio
                if out:
                    self._example("outlier", billing_code=code, negotiated_type=ntype, rate=v,
                                  detail=f"group median {math.exp(med):.2f}")
                for a in buckets:
                    a.outlier_tested += 1
                    a.outliers += out

    # ---------------------------------------------------------------- the pass
    def _parse(self, fh, refs_only: bool = False) -> None:
        builder, target = None, None
        progress = self.cfg["stream"]["progress_every_items"]
        n_items = 0
        for prefix, event, value in ijson.parse(fh, use_float=True):
            if builder is not None:
                builder.event(event, value)
                if prefix == target and event == "end_map":
                    obj = builder.value
                    builder = None
                    if target == PR:
                        self._provider_reference(obj)
                    else:
                        self._item(obj)
                        n_items += 1
                        if progress and n_items % progress == 0:
                            self.log(f"[c3]   {n_items:,} in_network items, {self.acc[ALL].rows:,} price rows "
                                     f"... {self.url.rsplit('/', 1)[-1]}")
                continue
            if prefix in TOP_FIELDS and event in ("string", "number"):
                self.top[prefix] = value
            elif prefix == PR and event == "start_map" and not self._skip_refs:
                builder, target = ObjectBuilder(), PR
                builder.event(event, value)
            elif prefix == IN and event == "start_map" and not refs_only:
                builder, target = ObjectBuilder(), IN
                builder.event(event, value)
            elif prefix in ("provider_references", "in_network") and event == "start_array":
                self.order.append(prefix)
                if prefix == "in_network" and refs_only:
                    pass
                elif prefix == "in_network" and self.tagger.enabled and not self.refs_ready \
                        and "provider_references" not in self.order:
                    raise _NeedRefsFirst()
            elif prefix == "provider_references" and event == "end_array" and "in_network" not in self.order:
                self.refs_ready = True
            elif prefix == "provider_references" and event == "end_array" and refs_only:
                self.refs_ready = True
                return

    _skip_refs = False

    def run(self) -> "FileScan":
        sc = self.cfg["stream"]
        t0 = time.time()
        try:
            try:
                self._stream(sc)
            except _NeedRefsFirst:
                # Tags need provider_references before in_network; this file has them after.
                # Read the references first, then grade the items with the references known.
                self.log(f"[c3]   in_network comes before provider_references; reading references first: {self.url}")
                self.__init__(self.url, self.cfg, self.tagger, self.log)
                self._stream(sc, refs_only=True)
                self.refs_ready, self._skip_refs, self.order = True, True, []
                self._stream(sc)
        except Exception as exc:
            self.status, self.error = "error", f"{exc.__class__.__name__}: {exc}"[:500]
        self._resolve_pending()
        if self.status == "ok" and self.stop_reason == "byte_cap_reached":
            self.status = "sampled"
        self.seconds = round(time.time() - t0, 2)
        return self

    def _stream(self, sc: dict, refs_only: bool = False) -> None:
        with open_stream(self.url, max_bytes=sc["max_bytes"], timeout=sc["timeout_seconds"], retries=sc["retries"],
                         user_agent=sc["user_agent"]) as (fh, counter):
            try:
                self._parse(fh, refs_only=refs_only)
            except ByteCapReached:
                self.stop_reason = "byte_cap_reached"
            finally:
                self.bytes_read = counter.bytes_read

    def _resolve_pending(self) -> None:
        """Rows read before provider_references: link them now that every reference is known."""
        a = self.acc[ALL]
        for refs, n in self.pending.items():
            if any(r not in self.refs for r in refs):
                self._err("dangling_provider_reference", n, detail=list(refs)[:5])
            if any(self.refs.get(r, (None, False))[1] for r in refs):
                a.resolved += n
        self.pending.clear()
        self.dedup.clear()


class _NeedRefsFirst(Exception):
    pass


def scan_file(url: str, cfg: dict, tagger=None, log=print) -> FileScan:
    return FileScan(url, cfg, tagger, log).run()
