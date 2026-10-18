"""Local UI adapters for the existing C1-C7 outputs and ptd_run entry point.

Only PipelineJob writes files. Data queries open the pipeline database read-only;
missing outputs stay missing rather than being replaced with sample records.
"""
from __future__ import annotations

import csv
import gzip
import http.server
import importlib.util
import json
import math
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STAGES = ('c1', 'c2', 'c3', 'c4', 'c3net', 'c5', 'c6', 'c7')


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def now():
    return datetime.now(timezone.utc).isoformat()


def integer(query, key, default, maximum=None, minimum=1):
    try:
        value = int(query.get(key, [str(default)])[0])
    except (ValueError, TypeError):
        raise ApiError(f'{key} must be an integer')
    if value < minimum or (maximum is not None and value > maximum):
        raise ApiError(f'{key} must be between {minimum} and {maximum or "the supported maximum"}')
    return value


def rows_csv(path):
    if path.exists():
        opener = gzip.open if path.suffix == '.gz' else open
        with opener(path, 'rt', encoding='utf-8-sig', newline='') as fh:
            yield from csv.DictReader(fh)


def read_json(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        raise ApiError(f'Cannot read pipeline report {path.name}: {exc}', 503)


def truth(value):
    return str(value).lower() in ('1', 'true', 'yes')


def clean_json(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(v) for v in value]
    return value


class PipelineData:
    def __init__(self, pipeline_dir):
        self.root = Path(pipeline_dir).resolve()
        self.out = self.root / 'out'
        self.data = self.root / 'data'
        self.db = self.out / 'rates.sqlite'

    @contextmanager
    def connection(self):
        if not self.db.exists():
            raise ApiError('No rate database yet. Run the pipeline through C6 first.', 409)
        con = sqlite3.connect(self.db.as_uri() + '?mode=ro', uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()

    @staticmethod
    def tables(con):
        return {r[0] for r in con.execute('SELECT name FROM sqlite_master')}

    def status(self):
        tables = set()
        error = None
        if self.db.exists():
            try:
                with self.connection() as con:
                    tables = self.tables(con)
            except sqlite3.Error as exc:
                error = str(exc)
        return {'server': 'online', 'ratesReady': 'c6_rates' in tables,
                'databaseError': error, 'exports': {kind: (self.out / relative).is_file() for kind, relative in {
                    'confirmed-csv': 'c6/c6_provider_rate_file.csv.gz',
                    'confirmed-xlsx': 'c6/c6_provider_rate_file.xlsx',
                    'scored-xlsx': 'c7/c7_provider_rate_file_scored.xlsx',
                    'candidates-csv': 'c6/c6_provider_rate_file_candidates.csv.gz',
                }.items()}, 'dataDirectory': str(self.data),
                'outputDirectory': str(self.out), 'database': str(self.db),
                'data_status': {'index_table_csv': (self.data / 'index_table.csv').exists(),
                                'rate_files_csv': (self.data / 'rate_files.csv').exists(),
                                'c2_classification': (self.out / 'c2/c2_classification.csv').exists(),
                                'c5_addresses': 'c5_addresses' in tables,
                                'c7_scores': 'c7_provider_scores' in tables}}

    def summary(self):
        stages = {s: read_json(self.out / s / f'{s}_run_summary.json')
                  for s in ('c2', 'c3', 'c4', 'c5', 'c6', 'c7')}
        c2, c3, c4, c5, c6, c7 = (stages[s] for s in stages)
        toc = read_json(self.data / 'toc_urls.json')
        files = c6.get('files', {})
        count = lambda path: sum(1 for _ in rows_csv(path)) if path.exists() else None
        return {'stages': stages, 'metrics': {
            'totalIndexPlans': count(self.data / 'index_table.csv'),
            'indexFilesCount': len(toc.get('links', toc.get('toc_urls', []))) if toc else None,
            'caGroupPlans': c2.get('in_scope_rows'), 'ppoPlans': c2.get('in_scope_ppo_rows'),
            'planTypes': c2.get('final_types', {}), 'fileScores': c3.get('scores', {}),
            'addressCoveragePct': c5.get('pct_npis_with_address'),
            'addressVerificationPct': c5.get('pct_npis_with_verified_address'),
            'confirmedRows': files.get('c6_provider_rate_file', {}).get('rows'),
            'candidateRows': files.get('c6_provider_rate_file_candidates', {}).get('rows'),
            'providersScored': c7.get('providers_scored'), 'costTiers': c7.get('tiers', {}),
            'pairNetworkStatus': c4.get('pair_network_status') or {},
            'referenceNetworkStatus': c4.get('network_status') or {},
        }, 'finished': {s: v.get('finished') for s, v in stages.items()},
            'available': any(stages.values()) or (self.data / 'index_table.csv').exists()}

    def rate_source(self, con):
        tables = self.tables(con)
        if 'c6_rates' not in tables:
            raise ApiError('C6 rates are not available. Run the pipeline through C6.', 409)
        fields = ['r.*', "CAST(r.npi AS TEXT) AS provider_npi"]
        joins = []
        if 'c5_providers' in tables:
            joins.append('LEFT JOIN c5_providers p ON p.npi=r.npi')
            fields += ["p.name AS name", 'p.deactivated AS deactivated']
        else:
            fields += ["'' AS name", 'NULL AS deactivated']
        if 'c5_addresses' in tables:
            joins.append('LEFT JOIN c5_addresses a ON a.npi=r.npi AND a.tin=r.tin')
            fields += ['a.address AS address', 'a.city AS city', 'a.state AS state', 'a.zip5 AS zip',
                       'a.rank AS address_rank', 'a.confidence AS address_confidence',
                       'a.verified AS verified', 'a.sources AS address_sources', 'a.evidence AS address_evidence',
                       "printf('%s:%s',r.rowid,COALESCE(a.rowid,0)) AS id"]
        else:
            fields += ["'' AS address", "'' AS city", "'' AS state", "'' AS zip", 'NULL AS address_rank',
                       'NULL AS address_confidence', 'NULL AS verified', "'' AS address_sources",
                       "'' AS address_evidence", "printf('%s:0',r.rowid) AS id"]
        if 'c4_codes' in tables:
            joins.append('LEFT JOIN c4_codes d ON d.code_type=r.code_type AND d.code=r.code')
            fields.append('d.description AS description')
        else:
            fields.append("'' AS description")
        if 'c4_pairs' in tables:
            joins.append('LEFT JOIN c4_pairs n ON n.npi=r.npi AND n.tin=r.tin AND n.file_id=r.file_id')
            fields.append('n.status_basis AS network_basis')
        else:
            fields.append("'' AS network_basis")
        scores = []
        for alias, table, condition in (
            ('pc', 'c7_provider_scores', "r.network_status='CONFIRMED_PPO'"),
            ('pu', 'c7_provider_scores_candidates', "r.network_status IN ('PPO_CANDIDATE','UNRESOLVED')")):
            if table in tables:
                joins.append(f'LEFT JOIN {table} {alias} ON {alias}.npi=r.npi AND {alias}.tin=r.tin AND {condition}')
                scores.append(alias)
        for column in ('cost_index', 'tier', 'score_confidence'):
            expr = 'COALESCE(' + ','.join(f'{a}.{column}' for a in scores) + ',NULL)' if scores else 'NULL'
            fields.append(f'{expr} AS {column}')
        return 'SELECT ' + ','.join(fields) + ' FROM c6_rates r ' + ' '.join(joins)

    SORTS = {'provider': 'provider_npi,tin,code_type,code,file_id,address_rank,id',
             'rate_asc': 'rate,provider_npi,tin,code,address_rank,id',
             'rate_desc': 'rate DESC,provider_npi,tin,code,address_rank,id',
             'code': 'code_type,code,rate,provider_npi,tin,address_rank,id'}

    @staticmethod
    def rate_filters(query):
        """WHERE clause shared by the rate table and the code spread: exact NPI/TIN/code, name contains, rate range."""
        conditions, args = [], []
        for key, column in (('code', 'code'), ('specialty', 'specialty'), ('status', 'network_status'),
                            ('tier', 'tier'), ('region', 'region'), ('npi', 'provider_npi')):
            value = query.get(key, [''])[0].strip()
            if value and value != 'ALL':
                conditions.append(f'{column}=?')
                args.append(value)
        tin = query.get('tin', [''])[0].strip()
        if tin:
            conditions.append("replace(tin,'-','')=?")
            args.append(tin.replace('-', ''))
        provider = query.get('provider', [''])[0].strip()
        if provider:
            conditions.append("instr(lower(COALESCE(name,'')),lower(?))>0")
            args.append(provider)
        for key, op in (('minRate', '>='), ('maxRate', '<=')):
            value = query.get(key, [''])[0].strip()
            if value:
                try:
                    args.append(float(value))
                except ValueError:
                    raise ApiError(f'{key} must be a number')
                conditions.append(f'rate{op}?')
        return conditions, args

    def rates(self, query):
        page = integer(query, 'page', 1)
        size = integer(query, 'pageSize', 25, 200)
        order = self.SORTS.get(query.get('sort', ['provider'])[0])
        if not order:
            raise ApiError('sort must be one of ' + ', '.join(self.SORTS))
        conditions, args = self.rate_filters(query)
        term = query.get('q', [''])[0].strip()
        if term:
            conditions.append("instr(lower(COALESCE(name,'')||' '||provider_npi||' '||tin||' '||"
                              "COALESCE(address,'')||' '||COALESCE(city,'')||' '||COALESCE(specialty,'')||' '||code),lower(?))>0")
            args.append(term)
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        with self.connection() as con:
            source = 'WITH rates AS (' + self.rate_source(con) + ') '
            total = con.execute(source + 'SELECT COUNT(*) FROM rates' + where, args).fetchone()[0]
            page = min(page, max(1, (total + size - 1) // size))
            records = [dict(r) for r in con.execute(source + 'SELECT * FROM rates' + where +
                       f' ORDER BY {order} LIMIT ? OFFSET ?',
                       args + [size, (page - 1) * size])]
            for r in records:
                r['npi'] = r.pop('provider_npi')
                r['verified'] = truth(r['verified']) if r['verified'] is not None else None
                r['deactivated'] = truth(r['deactivated']) if r['deactivated'] is not None else None
            return {'total': total, 'page': page, 'pageSize': size, 'rates': records}

    def facets(self):
        """Filter choices. Read from c6_rates and the C7 score tables directly (no address join), so they stay fast."""
        with self.connection() as con:
            tables = self.tables(con)
            if 'c6_rates' not in tables:
                raise ApiError('C6 rates are not available. Run the pipeline through C6.', 409)
            distinct = lambda table, column: [r[0] for r in con.execute(
                f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL AND {column}<>'' ORDER BY 1")]
            out = {key: distinct('c6_rates', column) for key, column in (
                ('codes', 'code'), ('specialties', 'specialty'), ('statuses', 'network_status'), ('regions', 'region'))}
            out['tiers'] = sorted({t for table in ('c7_provider_scores', 'c7_provider_scores_candidates')
                                   if table in tables for t in distinct(table, 'tier')})
            out['codeLabels'] = {r[0]: r[1] for r in con.execute(
                "SELECT code, MAX(COALESCE(description,'')) FROM c4_codes GROUP BY code") if r[1]} if 'c4_codes' in tables else {}
            return out

    def csv_page(self, path, query, predicate=lambda r: True):
        page, size = integer(query, 'page', 1), integer(query, 'pageSize', 25, 200)
        records, total = [], 0
        tail = deque(maxlen=size)
        for row in rows_csv(path):
            if predicate(row):
                tail.append(row)
                if (page - 1) * size <= total < page * size:
                    records.append(row)
                total += 1
        last_page = max(1, (total + size - 1) // size)
        if page > last_page:
            page = last_page
            records = list(tail)[-(total % size or size):] if total else []
        return {'available': path.exists(), 'total': total, 'page': page, 'pageSize': size, 'rows': records}

    def plans(self, query):
        term = query.get('q', [''])[0].lower()
        network = query.get('network', [''])[0]
        return self.csv_page(self.out / 'c2/c2_classification.csv', query, lambda r:
                             (not term or term in ' '.join(str(v) for v in r.values()).lower()) and
                             (not network or r.get('final_network_type') == network))

    def quality(self):
        return {'scores': list(rows_csv(self.out / 'c3/c3_file_scores.csv')),
                'metrics': list(rows_csv(self.out / 'c3/c3_metrics.csv'))}

    def entities(self, query):
        tin = query.get('tin', [''])[0].strip()
        with self.connection() as con:
            if 'c5_addresses' not in self.tables(con):
                raise ApiError('C5 practice addresses are not available yet.', 409)
            if not tin:
                return {'tins': [r[0] for r in con.execute('SELECT DISTINCT tin FROM c5_addresses ORDER BY tin')],
                        'rows': [], 'total': 0}
            size, page = integer(query, 'pageSize', 25, 200), integer(query, 'page', 1)
            total = con.execute('SELECT COUNT(*) FROM c5_addresses WHERE tin=?', (tin,)).fetchone()[0]
            page = min(page, max(1, (total + size - 1) // size))
            tables = self.tables(con)
            provider = 'LEFT JOIN c5_providers p ON p.npi=a.npi' if 'c5_providers' in tables else ''
            name = 'p.name' if provider else "''"
            records = [dict(r) for r in con.execute(f'SELECT a.*,CAST(a.npi AS TEXT) AS provider_npi,{name} AS name '
                       f'FROM c5_addresses a {provider} WHERE a.tin=? ORDER BY a.npi,a.rank LIMIT ? OFFSET ?',
                       (tin, size, (page - 1) * size))]
            for r in records:
                r['npi'] = r.pop('provider_npi')
                r['verified'] = truth(r['verified'])
            return {'tin': tin, 'rows': records, 'total': total, 'page': page, 'pageSize': size}

    VARIANT = ('negotiated_type', 'billing_class', 'setting', 'pos', 'modifier')

    @classmethod
    def variant_key(cls, row):
        return '|'.join(str(row[c] if row[c] is not None else '') for c in cls.VARIANT)

    @staticmethod
    def quantiles(values):
        """Min, quartiles (linear interpolation, as numpy's default) and max of a list of rates."""
        v = sorted(values)
        if not v:
            return {}
        def q(p):
            i = (len(v) - 1) * p
            lo = math.floor(i)
            return v[lo] + (v[min(lo + 1, len(v) - 1)] - v[lo]) * (i - lo)
        return {'n': len(v), 'min': v[0], 'q1': q(.25), 'median': q(.5), 'q3': q(.75), 'max': v[-1]}

    def code_spread(self, query):
        """Every provider's rate for one billing code and price variant: quartiles, histogram, ranked list.

        One point per NPI + TIN (C6 holds one rate per pair, code and variant; addresses are not repeated)."""
        code = query.get('code', [''])[0].strip()
        if not code:
            raise ApiError('Choose a billing code')
        status = query.get('status', [''])[0].strip()
        limit = integer(query, 'limit', 300, 2000)
        with self.connection() as con:
            source = 'WITH rates AS (' + self.rate_source(con) + ') '
            variants = [dict(r) for r in con.execute(
                source + 'SELECT ' + ','.join(f"COALESCE({c},'') AS {c}" for c in self.VARIANT) +
                ", COUNT(DISTINCT provider_npi||'|'||tin) AS providers FROM rates WHERE code=? "
                + ('AND network_status=? ' if status else '') + 'GROUP BY ' + ','.join(self.VARIANT) +
                ' ORDER BY providers DESC, 1, 2, 3, 4, 5', [code] + ([status] if status else []))]
            for v in variants:
                v['key'] = self.variant_key(v)
            if not variants:
                return {'code': code, 'variants': [], 'stats': {}, 'providers': [], 'histogram': []}
            key = query.get('variant', [''])[0] or variants[0]['key']
            chosen = next((v for v in variants if v['key'] == key), None)
            if chosen is None:
                raise ApiError('Unknown price variant for this code')
            where = 'code=? AND ' + ' AND '.join(f"COALESCE({c},'')=?" for c in self.VARIANT)
            args = [code] + [chosen[c] for c in self.VARIANT]
            if status:
                where += ' AND network_status=?'
                args.append(status)
            rows = [dict(r) for r in con.execute(
                source + 'SELECT provider_npi AS npi, tin, MAX(COALESCE(name,\'\')) AS name, MAX(specialty) AS specialty, '
                'MAX(region) AS region, MAX(network_status) AS network_status, MIN(rate) AS rate, MAX(tier) AS tier, '
                'MAX(cost_index) AS cost_index, MAX(CASE WHEN address_rank=1 THEN address END) AS address, '
                'MAX(description) AS description '
                f'FROM rates WHERE {where} GROUP BY provider_npi, tin ORDER BY rate, provider_npi, tin', args)]
        stats = self.quantiles([r['rate'] for r in rows])
        for r in rows:
            r['vs_median'] = r['rate'] / stats['median'] if stats.get('median') else None
        lo, hi = stats['min'], stats['max']
        bins = min(20, max(1, len(set(r['rate'] for r in rows))))
        width = (hi - lo) / bins if hi > lo else 1
        histogram = [{'from': lo + i * width, 'to': lo + (i + 1) * width, 'count': 0} for i in range(bins)]
        for r in rows:
            histogram[min(bins - 1, int((r['rate'] - lo) / width)) if hi > lo else 0]['count'] += 1
        shown = rows if len(rows) <= limit else rows[:limit // 2] + rows[-(limit - limit // 2):]
        return {'code': code, 'description': rows[0]['description'] if rows else '', 'status': status,
                'variants': variants, 'variant': chosen, 'stats': stats, 'histogram': histogram,
                'providers': shown, 'truncated': len(rows) > len(shown)}

    @staticmethod
    def pair_list(query, maximum=6):
        pairs = []
        for item in query.get('pairs', [''])[0].split(','):
            if item.strip():
                npi, _, tin = item.strip().partition(':')
                if not npi.isdigit() or not tin:
                    raise ApiError('pairs must look like NPI:TIN,NPI:TIN')
                pairs.append((npi, tin))
        if not pairs:
            raise ApiError('Choose at least one provider (NPI and TIN) to compare')
        if len(pairs) > maximum:
            raise ApiError(f'Compare at most {maximum} providers at once')
        return list(dict.fromkeys(pairs))

    def compare(self, query):
        """Side by side rates of a few NPI + TIN providers, code by code and variant by variant.

        Each row also carries the median over every provider in the rate file with the same code and
        variant (and network status, when a status is chosen), so a cheap rate is cheap against the market."""
        pairs = self.pair_list(query)
        status = query.get('status', [''])[0].strip()
        with self.connection() as con:
            source = 'WITH rates AS (' + self.rate_source(con) + ') '
            match = ' OR '.join(['(provider_npi=? AND tin=?)'] * len(pairs))
            flat = [v for p in pairs for v in p]
            providers = {(r['npi'], r['tin']): dict(r) for r in con.execute(
                source + "SELECT provider_npi AS npi, tin, MAX(COALESCE(name,'')) AS name, MAX(specialty) AS specialty, "
                'MAX(region) AS region, MAX(network_status) AS network_status, MAX(network_basis) AS network_basis, '
                'MAX(tier) AS tier, MAX(cost_index) AS cost_index, MAX(score_confidence) AS score_confidence, '
                'MAX(CASE WHEN address_rank=1 THEN address END) AS address, COUNT(DISTINCT address) AS sites '
                f'FROM rates WHERE {match} GROUP BY provider_npi, tin', flat)}
            missing = [f'{n}:{t}' for n, t in pairs if (n, t) not in providers]
            rates = [dict(r) for r in con.execute(
                source + 'SELECT DISTINCT provider_npi AS npi, tin, code_type, code, MAX(description) AS description, '
                + ','.join(f"COALESCE({c},'') AS {c}" for c in self.VARIANT) + ', MIN(rate) AS rate '
                f'FROM rates WHERE {match} GROUP BY provider_npi, tin, code_type, code, ' + ','.join(self.VARIANT), flat)]
            codes = sorted({r['code'] for r in rates})
            market = {}
            if codes:
                mk = ('SELECT code_type, code, ' + ','.join(f"COALESCE({c},'') AS {c}" for c in self.VARIANT) +
                      ', rate FROM c6_rates WHERE code IN (' + ','.join('?' * len(codes)) + ')' +
                      (' AND network_status=?' if status else ''))
                for r in con.execute(mk, codes + ([status] if status else [])):
                    market.setdefault((r['code_type'], r['code'], self.variant_key(r)), []).append(r['rate'])
        lines = {}
        for r in rates:
            key = (r['code_type'], r['code'], self.variant_key(r))
            line = lines.setdefault(key, {'code_type': r['code_type'], 'code': r['code'], 'description': r['description'],
                                          **{c: r[c] for c in self.VARIANT}, 'rates': {}})
            line['rates'][f"{r['npi']}:{r['tin']}"] = r['rate']
        rows = []
        for key, line in sorted(lines.items()):
            m = self.quantiles(market.get(key, []))
            line['market'] = m
            values = line['rates'].values()
            line['cheapest'] = min(values)
            line['spread'] = max(values) / min(values) if min(values) else None
            rows.append(line)
        order = [f'{n}:{t}' for n, t in pairs if (n, t) in providers]
        common = [r for r in rows if all(k in r['rates'] for k in order)]
        basket = {k: sum(r['rates'][k] for r in common) for k in order} if common and len(order) > 1 else {}
        return {'providers': [{**providers[tuple(k.split(':', 1))], 'key': k} for k in order], 'missing': missing,
                'rows': rows, 'commonCodes': len(common), 'basket': basket, 'status': status}

    def c5_overview(self):
        """How TINs, NPIs and practice sites link up across the whole C5 table (for the mapping view)."""
        with self.connection() as con:
            if 'c5_addresses' not in self.tables(con):
                raise ApiError('C5 practice addresses are not available yet.', 409)
            one = lambda sql: con.execute(sql).fetchone()[0]
            def buckets(sql, edges):
                out = {label: 0 for label, _, _ in edges}
                for (n,) in con.execute(sql):
                    for label, lo, hi in edges:
                        if lo <= n <= hi:
                            out[label] += 1
                            break
                return out
            big = 10 ** 9
            return {
                'tins': one('SELECT COUNT(DISTINCT tin) FROM c5_addresses'),
                'npis': one('SELECT COUNT(DISTINCT npi) FROM c5_addresses'),
                'pairs': one("SELECT COUNT(DISTINCT npi||'|'||tin) FROM c5_addresses"),
                'sites': one("SELECT COUNT(DISTINCT site_key) FROM c5_addresses WHERE COALESCE(site_key,'')<>''"),
                'pairsWithAddress': one("SELECT COUNT(DISTINCT npi||'|'||tin) FROM c5_addresses WHERE COALESCE(address,'')<>''"),
                'pairsVerified': one("SELECT COUNT(DISTINCT npi||'|'||tin) FROM c5_addresses WHERE rank=1 AND verified IN (1,'1','True','true')"),
                'npisPerTin': buckets('SELECT COUNT(DISTINCT npi) FROM c5_addresses GROUP BY tin',
                                      [('1', 1, 1), ('2', 2, 2), ('3-5', 3, 5), ('6-10', 6, 10), ('11-50', 11, 50), ('51+', 51, big)]),
                'tinsPerNpi': buckets('SELECT COUNT(DISTINCT tin) FROM c5_addresses GROUP BY npi',
                                      [('1', 1, 1), ('2', 2, 2), ('3', 3, 3), ('4+', 4, big)]),
                'sitesPerPair': buckets("SELECT SUM(COALESCE(address,'')<>'') FROM c5_addresses GROUP BY npi, tin",
                                        [('No address', 0, 0), ('1 site', 1, 1), ('2 sites', 2, 2), ('3+ sites', 3, big)]),
            }

    def c5_search(self, query):
        """TINs matching a TIN, an NPI or a provider name, largest groups first."""
        term = query.get('q', [''])[0].strip()
        size, page = integer(query, 'pageSize', 20, 100), integer(query, 'page', 1)
        with self.connection() as con:
            tables = self.tables(con)
            if 'c5_addresses' not in tables:
                raise ApiError('C5 practice addresses are not available yet.', 409)
            names = 'c5_providers' in tables
            where, args = '', []
            if term:
                digits = term.replace('-', '')
                clauses = ["replace(a.tin,'-','') LIKE ?", 'CAST(a.npi AS TEXT)=?']
                args = [digits + '%', digits]
                if names:
                    clauses.append("a.tin IN (SELECT a2.tin FROM c5_addresses a2 JOIN c5_providers p2 ON p2.npi=a2.npi "
                                   "WHERE instr(lower(COALESCE(p2.name,'')),lower(?))>0)")
                    args.append(term)
                where = 'WHERE a.tin IN (SELECT a.tin FROM c5_addresses a WHERE ' + ' OR '.join(clauses) + ')'
            org = ("(SELECT MAX(p.name) FROM c5_addresses x JOIN c5_providers p ON p.npi=x.npi "
                   "WHERE x.tin=a.tin AND CAST(p.entity_type AS TEXT)='2')") if names else "''"
            base = (f'SELECT a.tin, COUNT(DISTINCT a.npi) AS npis, COUNT(DISTINCT NULLIF(a.site_key,\'\')) AS sites, '
                    f"SUM(a.rank=1 AND a.verified IN (1,'1','True','true')) AS verified_pairs, {org} AS org_name "
                    f'FROM c5_addresses a {where} GROUP BY a.tin')
            total = con.execute(f'SELECT COUNT(*) FROM ({base})', args).fetchone()[0]
            page = min(page, max(1, (total + size - 1) // size))
            rows = [dict(r) for r in con.execute(base + ' ORDER BY npis DESC, a.tin LIMIT ? OFFSET ?',
                                                 args + [size, (page - 1) * size])]
            return {'q': term, 'rows': rows, 'total': total, 'page': page, 'pageSize': size,
                    'npi': term if term.isdigit() and len(term) == 10 else ''}

    def c5_tin(self, query):
        """One TIN's tree: TIN -> its NPIs (network status, other TINs) -> each NPI's ranked practice sites."""
        tin = query.get('tin', [''])[0].strip()
        if not tin:
            raise ApiError('Choose a TIN')
        size, page = integer(query, 'pageSize', 50, 200), integer(query, 'page', 1)
        with self.connection() as con:
            tables = self.tables(con)
            if 'c5_addresses' not in tables:
                raise ApiError('C5 practice addresses are not available yet.', 409)
            npis = [r[0] for r in con.execute('SELECT DISTINCT npi FROM c5_addresses WHERE tin=? ORDER BY npi', (tin,))]
            if not npis:
                raise ApiError(f'TIN {tin} has no NPIs in C5', 404)
            total = len(npis)
            page = min(page, max(1, (total + size - 1) // size))
            chunk = npis[(page - 1) * size:page * size]
            marks = ','.join('?' * len(chunk))
            info = {}
            if 'c5_providers' in tables:
                info = {r['npi']: dict(r) for r in con.execute(
                    f'SELECT npi, name, entity_type, primary_taxonomy, deactivated FROM c5_providers WHERE npi IN ({marks})', chunk)}
            status = {}
            if 'c4_pairs' in tables:
                for r in con.execute(f'SELECT npi, status, status_basis FROM c4_pairs WHERE tin=? AND npi IN ({marks})',
                                     [tin] + chunk):
                    status.setdefault(r['npi'], (r['status'], r['status_basis']))
            other = {r[0]: r[1] for r in con.execute(
                f'SELECT npi, COUNT(DISTINCT tin) - 1 FROM c5_addresses WHERE npi IN ({marks}) GROUP BY npi', chunk)}
            rates = {}
            if 'c6_rates' in tables:
                rates = {r[0]: (r[1], r[2]) for r in con.execute(
                    f'SELECT npi, COUNT(*), COUNT(DISTINCT code) FROM c6_rates WHERE tin=? AND npi IN ({marks}) GROUP BY npi',
                    [tin] + chunk)}
            sites = {}
            for r in con.execute(f'SELECT * FROM c5_addresses WHERE tin=? AND npi IN ({marks}) ORDER BY npi, rank',
                                 [tin] + chunk):
                r = dict(r)
                if r.get('address'):
                    sites.setdefault(r['npi'], []).append(
                        {k: r.get(k) for k in ('rank', 'address', 'city', 'zip5', 'county', 'confidence', 'sources',
                                               'families', 'evidence', 'site_key')} | {'verified': truth(r.get('verified'))})
            org = con.execute("SELECT MAX(p.name) FROM c5_addresses a JOIN c5_providers p ON p.npi=a.npi WHERE a.tin=? "
                              "AND CAST(p.entity_type AS TEXT)='2'", (tin,)).fetchone()[0] if 'c5_providers' in tables else None
            all_sites = con.execute("SELECT COUNT(DISTINCT NULLIF(site_key,'')) FROM c5_addresses WHERE tin=?", (tin,)).fetchone()[0]
        rows = []
        for n in chunk:
            p = info.get(n) or {}
            st = status.get(n, (None, None))
            rows.append({'npi': str(n), 'name': p.get('name') or '', 'entity_type': str(p.get('entity_type') or ''),
                         'taxonomy': p.get('primary_taxonomy') or '', 'deactivated': truth(p.get('deactivated')),
                         'network_status': st[0], 'network_basis': st[1], 'other_tins': other.get(n, 0),
                         'rate_rows': (rates.get(n) or (0, 0))[0], 'codes': (rates.get(n) or (0, 0))[1],
                         'sites': sites.get(n, [])})
        return {'tin': tin, 'org_name': org or '', 'npis': total, 'sites': all_sites, 'rows': rows,
                'page': page, 'pageSize': size, 'total': total}

    def benchmarks(self, query):
        group = query.get('set', ['confirmed'])[0]
        if group not in ('confirmed', 'candidates'):
            raise ApiError('set must be confirmed or candidates')
        folder = 'c7' if group == 'confirmed' else 'c7_candidates'
        term = query.get('q', [''])[0].lower()
        code = query.get('code', [''])[0]
        return self.csv_page(self.out / folder / 'c7_rate_benchmarks.csv.gz', query,
                             lambda r: (not code or r.get('code') == code) and
                             (not term or term in ' '.join(str(v) for v in r.values()).lower()))

    def export(self, kind):
        exports = {
            'confirmed-csv': ('c6/c6_provider_rate_file.csv.gz', '1_provider_rate_file.csv', 'text/csv; charset=utf-8'),
            'confirmed-xlsx': ('c6/c6_provider_rate_file.xlsx', '1_provider_rate_file.xlsx',
                               'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
            'scored-xlsx': ('c7/c7_provider_rate_file_scored.xlsx', '1_provider_rate_file_with_cost_scores.xlsx',
                            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
            'candidates-csv': ('c6/c6_provider_rate_file_candidates.csv.gz', 'candidate_rates_not_confirmed.csv',
                               'text/csv; charset=utf-8'),
        }
        if kind not in exports:
            raise ApiError('Unknown export', 404)
        relative, name, mime = exports[kind]
        path = self.out / relative
        if not path.is_file():
            raise ApiError(f'{name} has not been generated. Run the required pipeline stages first.', 404)
        return path, name, mime


class PipelineJob:
    """One real runner process at a time; pollable logs and exit status."""
    def __init__(self, data):
        self.data = data
        self.lock = threading.RLock()
        self.proc = None
        self.state = {'id': None, 'status': 'idle', 'exitCode': None, 'started': None, 'finished': None, 'command': []}
        self.logs = deque(maxlen=3000)
        self.sequence = 0
        self.stopping = False

    def inputs(self):
        """What ptd_run will pick up from pipeline/inputs/ (the pipeline's own discovery code, not a copy)."""
        spec = importlib.util.spec_from_file_location('ptd_run_inputs', self.data.root / 'ptd_run' / 'inputs.py')
        if spec is None or not (self.data.root / 'ptd_run' / 'inputs.py').is_file():
            return {'inputs_dir': str(self.data.root / 'inputs'), 'workbook': None, 'ppo_check_csv': None,
                    'live_directory_check': False, 'live_directory_check_unavailable': 'pipeline has no ptd_run/inputs.py'}
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.discover(self.data.root / module.INPUTS_DIR)

    def config(self):
        return {'stages': list(STAGES), 'inputs': self.inputs(), 'as_of': date.today().isoformat()}

    def command(self, options):
        # The workbook and the saved PPO check come from pipeline/inputs/ (ptd_run finds them), never from the browser.
        path_keys = ('directory', 'nppes', 'nppes_pl', 'dac', 'zip_county', 'html_file')
        flags = ('fetch_public',)
        allowed = set(path_keys + flags + ('from_stage', 'to_stage', 'as_of', 'rate_files', 'workers'))
        unknown = set(options) - allowed
        if unknown:
            raise ApiError('Unsupported run options: ' + ', '.join(sorted(unknown)))
        start, stop = options.get('from_stage', 'c1'), options.get('to_stage', 'c7')
        if start not in STAGES or stop not in STAGES or STAGES.index(start) > STAGES.index(stop):
            raise ApiError('Choose valid start/end stages in pipeline order')
        cmd = [sys.executable, '-u', '-m', 'ptd_run', 'all', '--from', start, '--to', stop]
        for key in path_keys:
            value = options.get(key)
            if value:
                if not isinstance(value, str):
                    raise ApiError(f'{key} must be a file path')
                path = Path(value).expanduser()
                path = path if path.is_absolute() else self.data.root / path
                if not path.is_file():
                    raise ApiError(f'{key} file not found: {path}')
                cmd += ['--' + key.replace('_', '-'), str(path.resolve())]
        for key in flags:
            value = options.get(key, False)
            if not isinstance(value, bool):
                raise ApiError(f'{key} must be true or false')
            if value:
                cmd.append('--' + key.replace('_', '-'))
        if options.get('as_of'):
            try:
                date.fromisoformat(options['as_of'])
            except (TypeError, ValueError):
                raise ApiError('as_of must be YYYY-MM-DD')
            cmd += ['--as-of', options['as_of']]
        if options.get('workers') is not None:
            workers = options['workers']
            if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 32:
                raise ApiError('workers must be an integer between 1 and 32')
            cmd += ['--workers', str(workers)]
        files = options.get('rate_files', [])
        if not isinstance(files, list) or any(not isinstance(p, str) for p in files):
            raise ApiError('rate_files must be a list of local file paths')
        if files:
            cmd += ['--files']
            for value in files:
                path = Path(value).expanduser()
                path = path if path.is_absolute() else self.data.root / path
                if not path.is_file():
                    raise ApiError(f'Rate file not found: {path}')
                cmd.append(str(path.resolve()))
        return cmd

    def start(self, options):
        cmd = self.command(options)
        with self.lock:
            if self.stopping:
                raise ApiError('Server is shutting down', 503)
            if self.state['status'] == 'running':
                raise ApiError('A pipeline run is already active', 409)
            log_dir = self.data.out / 'ui_runs'
            log_dir.mkdir(parents=True, exist_ok=True)
            run_id = uuid.uuid4().hex
            try:
                proc = subprocess.Popen(cmd, cwd=self.data.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding='utf-8', errors='replace', bufsize=1,
                                        start_new_session=(os.name != 'nt'))
            except OSError as exc:
                raise ApiError(f'Could not start the pipeline: {exc}', 503)
            self.proc = proc
            self.state = {'id': run_id, 'status': 'running', 'exitCode': None,
                          'started': now(), 'finished': None, 'command': cmd, 'logFile': str(log_dir / f'{run_id}.log')}
            self.logs.clear()
            self.sequence = 0
            threading.Thread(target=self._watch, args=(proc,), daemon=True).start()
            return self.snapshot()

    def _watch(self, proc):
        try:
            with open(self.state['logFile'], 'w', encoding='utf-8') as fh:
                for line in proc.stdout:
                    fh.write(line)
                    fh.flush()
                    with self.lock:
                        self.sequence += 1
                        self.logs.append({'seq': self.sequence, 'text': line.rstrip('\n')})
            code = proc.wait()
        except Exception as exc:
            proc.wait()
            code = proc.returncode or 1
            with self.lock:
                self.sequence += 1
                self.logs.append({'seq': self.sequence, 'text': f'Log capture failed: {exc}'})
        finally:
            proc.stdout.close()
        with self.lock:
            self.state.update(status='succeeded' if code == 0 else 'failed', exitCode=code, finished=now())

    def snapshot(self, after=0):
        with self.lock:
            return {**self.state, 'logs': [r for r in self.logs if r['seq'] > after], 'nextOffset': self.sequence,
                    'logsTruncated': bool(self.logs and after < self.logs[0]['seq'] - 1)}

    def close(self):
        with self.lock:
            self.stopping = True
            proc = self.proc
        if proc and proc.poll() is None:
            if os.name == 'nt':
                proc.terminate()
            else:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    return
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == 'nt':
                    proc.kill()
                else:
                    os.killpg(proc.pid, signal.SIGKILL)


def make_handler(data, job, ui_dir):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ui_dir), **kwargs)

        def send_json(self, value, status=200):
            body = json.dumps(clean_json(value), allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def local_host(self):
            return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}',
                                                 f'localhost:{self.server.server_port}')

        def do_GET(self):
            if not self.local_host():
                return self.send_json({'error': 'Use the localhost URL printed by the UI server'}, 403)
            path = urlparse(self.path).path
            query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            if not path.startswith('/api/'):
                return super().do_GET()
            try:
                if path == '/api/status':
                    return self.send_json({**data.status(), 'run': job.snapshot(integer(query, 'after', 0, minimum=0))})
                if path == '/api/pipeline/config':
                    return self.send_json(job.config())
                if path == '/api/pipeline/run':
                    return self.send_json(job.snapshot(integer(query, 'after', 0, minimum=0)))
                if job.snapshot()['status'] == 'running':
                    raise ApiError('Pipeline is running. Results refresh when it finishes.', 409)
                routes = {'/api/summary': lambda: data.summary(), '/api/rates': lambda: data.rates(query),
                          '/api/rates/facets': lambda: data.facets(), '/api/c2/plans': lambda: data.plans(query),
                          '/api/c3/metrics': lambda: data.quality(), '/api/c5/entities': lambda: data.entities(query),
                          '/api/benchmarks': lambda: data.benchmarks(query),
                          '/api/rates/spread': lambda: data.code_spread(query),
                          '/api/compare': lambda: data.compare(query),
                          '/api/c5/overview': lambda: data.c5_overview(),
                          '/api/c5/search': lambda: data.c5_search(query),
                          '/api/c5/tin': lambda: data.c5_tin(query)}
                if path in routes:
                    return self.send_json(routes[path]())
                if path == '/api/export':
                    file, name, mime = data.export(query.get('kind', ['confirmed-csv'])[0])
                    opener = gzip.open if file.suffix == '.gz' else open
                    with opener(file, 'rb') as fh:
                        self.send_response(200)
                        self.send_header('Content-Type', mime)
                        self.send_header('Content-Disposition', f'attachment; filename="{name}"')
                        self.send_header('Cache-Control', 'no-store')
                        if file.suffix != '.gz':
                            self.send_header('Content-Length', str(file.stat().st_size))
                        self.end_headers()
                        shutil.copyfileobj(fh, self.wfile)
                    return
                raise ApiError('Unknown API endpoint', 404)
            except ApiError as exc:
                self.send_json({'error': str(exc)}, exc.status)
            except (sqlite3.Error, OSError, csv.Error) as exc:
                self.send_json({'error': f'Cannot read pipeline outputs: {exc}'}, 503)

        def do_POST(self):
            try:
                if not self.local_host():
                    raise ApiError('Use the localhost URL printed by the UI server', 403)
                if urlparse(self.path).path != '/api/pipeline/run':
                    raise ApiError('Unknown API endpoint', 404)
                origin = self.headers.get('Origin')
                if origin and urlparse(origin).netloc != self.headers.get('Host'):
                    raise ApiError('Run requests must come from this UI', 403)
                if self.headers.get_content_type() != 'application/json':
                    raise ApiError('Send run options as application/json', 415)
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    raise ApiError('Invalid request size')
                if not 0 < size <= 16384:
                    raise ApiError('Run options must be a JSON object smaller than 16 KB')
                try:
                    options = json.loads(self.rfile.read(size))
                except (ValueError, UnicodeError):
                    raise ApiError('Invalid JSON')
                if not isinstance(options, dict):
                    raise ApiError('Run options must be a JSON object')
                self.send_json(job.start(options), 202)
            except ApiError as exc:
                self.send_json({'error': str(exc)}, exc.status)
            except OSError as exc:
                self.send_json({'error': f'Cannot start pipeline: {exc}'}, 503)

    return Handler
