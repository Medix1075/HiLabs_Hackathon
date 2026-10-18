"""Integration checks for the UI against outputs from the actual C3-C7 runner."""
import csv
import gzip
import http.server
import io
import json
import shutil
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import ca_world

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from ptd_ui import ApiError, PipelineData, PipelineJob, make_handler


def request(base, path, options=None, headers=None):
    body = json.dumps(options).encode() if options is not None else None
    req = urllib.request.Request(base + path, data=body,
                                 headers={'Content-Type': 'application/json', **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as res:
            data = res.read()
            return res.status, json.loads(data) if 'application/json' in res.headers.get('Content-Type', '') else data
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def serve(data):
    job = PipelineJob(data)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), make_handler(data, job, REPO / 'ui'))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return job, server, f'http://127.0.0.1:{server.server_port}'


def wait_run(base):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        status, run = request(base, '/api/pipeline/run')
        assert status == 200
        if run['status'] != 'running':
            return run
        time.sleep(.05)
    pytest.fail('Actual pipeline run did not finish within 45 seconds')


@pytest.fixture(scope='module')
def live(tmp_path_factory):
    root = tmp_path_factory.mktemp('ui-run')
    code = root / 'pipeline'
    shutil.copytree(REPO / 'pipeline', code, ignore=shutil.ignore_patterns('data', 'out', '__pycache__', '.pytest_cache', '.venv'))
    inputs = ca_world.build(root / 'inputs')
    # The workbook comes from pipeline/inputs/, never from the browser.
    (code / 'inputs').mkdir(exist_ok=True)
    shutil.copy(inputs['workbook'], code / 'inputs' / 'Rate Benchmarking Individual Weights_Hackathon.xlsx')
    data = PipelineData(code)
    job, server, base = serve(data)
    options = {'from_stage': 'c3', 'to_stage': 'c7', 'as_of': '2026-10-17', 'workers': 2,
               'rate_files': [str(inputs['rate_file'])],
               'directory': str(inputs['directory']), 'nppes': str(inputs['nppes']),
               'nppes_pl': str(inputs['pl']), 'dac': str(inputs['dac']), 'zip_county': str(inputs['zip_county'])}
    status, initial = request(base, '/api/pipeline/run', options)
    assert status == 202
    blocked, _ = request(base, '/api/pipeline/run', options)
    assert blocked == 409
    assert request(base, '/api/rates')[0] == 409
    run = wait_run(base)
    assert run['status'] == 'succeeded', run['logs']
    assert run['exitCode'] == 0
    yield {'data': data, 'base': base, 'run': run, 'inputs': inputs}
    server.shutdown()
    server.server_close()
    job.close()


def test_empty_outputs_are_honest_and_read_only(tmp_path):
    data = PipelineData(tmp_path)
    assert data.status()['ratesReady'] is False
    assert not any(data.status()['exports'].values())
    assert data.summary()['metrics']['ppoPlans'] is None
    assert data.summary()['available'] is False
    with pytest.raises(ApiError, match='No rate database'):
        data.rates({})
    assert not data.db.exists()
    assert list(tmp_path.iterdir()) == []


def test_actual_runner_logs_and_reports(live):
    run = live['run']
    assert '-m' in run['command'] and 'ptd_run' in run['command']
    assert any('===== C4 =====' in line['text'] for line in run['logs'])
    assert any('[run] done.' in line['text'] for line in run['logs'])
    assert Path(run['logFile']).read_text()
    assert live['data'].db.exists()
    status, body = request(live['base'], '/api/status')
    assert status == 200 and body['ratesReady']
    assert body['data_status']['c5_addresses'] and body['data_status']['c7_scores']
    assert body['exports']['confirmed-csv']
    offset = run['logs'][5]['seq']
    _, later = request(live['base'], f'/api/pipeline/run?after={offset}')
    assert all(line['seq'] > offset for line in later['logs'])


def test_summary_uses_latest_stage_outputs(live):
    _, summary = request(live['base'], '/api/summary')
    out = live['data'].out
    c5 = json.loads((out / 'c5/c5_run_summary.json').read_text())
    c6 = json.loads((out / 'c6/c6_run_summary.json').read_text())
    assert summary['metrics']['addressVerificationPct'] == c5['pct_npis_with_verified_address']
    assert summary['metrics']['confirmedRows'] == c6['files']['c6_provider_rate_file']['rows']
    assert summary['metrics']['fileScores']
    # Reload outputs on each request; no stale hardcoded/cached summary.
    saved = (out / 'c5/c5_run_summary.json').read_text()
    try:
        c5['pct_npis_with_verified_address'] = 12.34
        (out / 'c5/c5_run_summary.json').write_text(json.dumps(c5))
        assert live['data'].summary()['metrics']['addressVerificationPct'] == 12.34
    finally:
        (out / 'c5/c5_run_summary.json').write_text(saved)


def test_rate_rows_match_c6_deliverable_and_paginate(live):
    base, data = live['base'], live['data']
    status, first = request(base, '/api/rates?status=CONFIRMED_PPO&pageSize=3')
    assert status == 200 and len(first['rates']) == 3
    with gzip.open(data.out / 'c6/c6_provider_rate_file.csv.gz', 'rt') as fh:
        expected = list(csv.DictReader(fh))
    assert first['total'] == len(expected)
    _, second = request(base, '/api/rates?status=CONFIRMED_PPO&pageSize=3&page=2')
    assert {r['id'] for r in first['rates']}.isdisjoint(r['id'] for r in second['rates'])
    allowed = {(r['NPI'], r['TIN'], r['Address'], r['Billing Code'], float(r['Rate'])) for r in expected}
    for row in first['rates'] + second['rates']:
        assert (row['npi'], row['tin'], row['address'] or '', row['code'], row['rate']) in allowed
        assert len(row['npi']) == 10
    _, far_page = request(base, '/api/rates?status=CONFIRMED_PPO&pageSize=3&page=999999')
    assert far_page['rates']
    assert request(base, '/api/rates?page=abc')[0] == 400
    assert request(base, '/api/rates?pageSize=999999')[0] == 400
    _, injection = request(base, '/api/rates?q=%27%20OR%201%3D1--')
    assert injection['total'] == 0


def test_exports_are_actual_files_and_separate_candidates(live):
    base, data = live['base'], live['data']
    status, exported = request(base, '/api/export')
    assert status == 200
    with gzip.open(data.out / 'c6/c6_provider_rate_file.csv.gz', 'rb') as fh:
        assert exported == fh.read()
    confirmed = list(csv.DictReader(io.StringIO(exported.decode())))
    assert confirmed and all(r['Network Status'] == 'CONFIRMED_PPO' for r in confirmed)
    _, candidates = request(base, '/api/export?kind=candidates-csv')
    candidates = list(csv.DictReader(io.StringIO(candidates.decode())))
    assert candidates and all(r['Network Status'] != 'CONFIRMED_PPO' for r in candidates)
    _, xlsx = request(base, '/api/export?kind=confirmed-xlsx')
    assert xlsx == (data.out / 'c6/c6_provider_rate_file.xlsx').read_bytes()
    assert request(base, '/api/export?kind=../../README.md')[0] == 404


def test_c5_evidence_and_c7_benchmarks(live):
    base, data = live['base'], live['data']
    _, facets = request(base, '/api/rates/facets')
    assert facets['codes'] and 'CONFIRMED_PPO' in facets['statuses']
    _, groups = request(base, '/api/c5/entities')
    tin = groups['tins'][0]
    _, sites = request(base, '/api/c5/entities?tin=' + tin)
    assert sites['rows'] and all(r['tin'] == tin for r in sites['rows'])
    with sqlite3.connect(data.db) as con:
        expected = con.execute('SELECT COUNT(*) FROM c5_addresses WHERE tin=?', (tin,)).fetchone()[0]
    assert sites['total'] == expected
    _, benchmarks = request(base, '/api/benchmarks')
    with gzip.open(data.out / 'c7/c7_rate_benchmarks.csv.gz', 'rt') as fh:
        expected = list(csv.DictReader(fh))
    assert benchmarks['total'] == len(expected)
    assert benchmarks['rows'] == expected[:25]
    assert all(r['network_status'] == 'CONFIRMED_PPO' for r in benchmarks['rows'])
    _, candidates = request(base, '/api/benchmarks?set=candidates')
    assert candidates['available'] and candidates['total'] > 0
    assert all(r['network_status'] != 'CONFIRMED_PPO' for r in candidates['rows'])


def test_real_quality_and_plan_file_adapters(live):
    base, data = live['base'], live['data']
    _, quality = request(base, '/api/c3/metrics')
    with open(data.out / 'c3/c3_metrics.csv') as fh:
        assert quality['metrics'] == list(csv.DictReader(fh))
    plans = data.out / 'c2/c2_classification.csv'
    plans.parent.mkdir()
    plans.write_text('plan_name,plan_id,final_network_type,reason\nCA PPO,123,PPO,Explicit PPO\nCA HMO,124,HMO,Explicit HMO\n')
    _, result = request(base, '/api/c2/plans?network=PPO')
    assert result['total'] == 1 and result['rows'][0]['plan_id'] == '123'


def test_failures_return_real_nonzero_exit_code_and_missing_exports(tmp_path):
    code = tmp_path / 'pipeline'
    shutil.copytree(REPO / 'pipeline', code, ignore=shutil.ignore_patterns('data', 'out', '__pycache__', '.pytest_cache', '.venv'))
    data = PipelineData(code)
    job, server, base = serve(data)
    try:
        assert request(base, '/api/export')[0] == 404
        status, _ = request(base, '/api/pipeline/run', {'from_stage': 'c7', 'to_stage': 'c7'})
        assert status == 202
        run = wait_run(base)
        assert run['status'] == 'failed' and run['exitCode'] != 0
        assert any('No C6 rate table' in r['text'] for r in run['logs'])
    finally:
        server.shutdown();server.server_close();job.close()


def test_run_validation_and_cross_origin_requests(tmp_path):
    job, server, base = serve(PipelineData(tmp_path))
    try:
        for options in ({'command':'echo wrong'}, {'workers':0}, {'as_of':'bad'}, {'from_stage':'c7','to_stage':'c1'},
                        {'workbook':'Rate.xlsx'}, {'ppo_check_csv':'c4_ppo_check.csv'}, {'check_directory':True},
                        {'nppes':'missing.csv'}, {'rate_files':'bad'}):
            assert request(base, '/api/pipeline/run', options)[0] == 400
        assert request(base, '/api/pipeline/run', {}, {'Origin':'https://example.com'})[0] == 403
        assert request(base, '/api/status', headers={'Host':'attacker.example'})[0] == 403
        assert request(base, '/api/does-not-exist')[0] == 404
        assert request(base, '/')[0] == 200
        assert request(base, '/app.js')[0] == 200
    finally:
        server.shutdown();server.server_close();job.close()


def test_runner_used_workbook_from_inputs_folder(live):
    run = live['run']
    assert any('[input] workbook from' in line['text'] for line in run['logs'])
    assert '--workbook' not in run['command']
    _, config = request(live['base'], '/api/pipeline/config')
    assert config['inputs']['workbook'].endswith('Rate Benchmarking Individual Weights_Hackathon.xlsx')
    assert config['inputs']['ppo_check_csv'] is None and config['inputs']['live_directory_check'] is False


def test_rate_filters_by_npi_tin_provider_and_range(live):
    base, data = live['base'], live['data']
    _, first = request(base, '/api/rates?sort=rate_asc&pageSize=5')
    rates = [r['rate'] for r in first['rates']]
    assert rates == sorted(rates)
    row = first['rates'][0]
    with sqlite3.connect(data.db) as con:
        n_npi = con.execute('SELECT COUNT(*) FROM c6_rates r JOIN c5_addresses a ON a.npi=r.npi AND a.tin=r.tin '
                            'WHERE r.npi=?', (int(row['npi']),)).fetchone()[0]
    _, by_npi = request(base, f"/api/rates?npi={row['npi']}&pageSize=200")
    assert by_npi['total'] == n_npi and all(r['npi'] == row['npi'] for r in by_npi['rates'])
    _, by_tin = request(base, f"/api/rates?tin={row['tin']}&pageSize=200")
    assert by_tin['total'] and all(r['tin'] == row['tin'] for r in by_tin['rates'])
    _, by_name = request(base, '/api/rates?provider=' + urllib.request.quote(row['name'][:6]))
    assert by_name['total'] and all(row['name'][:6].lower() in r['name'].lower() for r in by_name['rates'])
    _, ranged = request(base, '/api/rates?minRate=50&maxRate=150&pageSize=200')
    assert ranged['total'] and all(50 <= r['rate'] <= 150 for r in ranged['rates'])
    assert request(base, '/api/rates?minRate=abc')[0] == 400
    assert request(base, '/api/rates?sort=drop')[0] == 400


def test_code_spread_matches_c6_rates(live):
    base, data = live['base'], live['data']
    _, spread = request(base, '/api/rates/spread?code=99213')
    v = spread['variant']
    with sqlite3.connect(data.db) as con:
        expected = sorted(r[0] for r in con.execute(
            "SELECT rate FROM c6_rates WHERE code='99213' AND negotiated_type=? AND billing_class=? AND "
            "COALESCE(setting,'')=? AND COALESCE(pos,'')=? AND COALESCE(modifier,'')=?",
            (v['negotiated_type'], v['billing_class'], v['setting'], v['pos'], v['modifier'])))
    assert spread['stats']['n'] == len(expected) == len(spread['providers'])
    assert spread['stats']['min'] == expected[0] and spread['stats']['max'] == expected[-1]
    assert sum(b['count'] for b in spread['histogram']) == len(expected)
    assert [p['rate'] for p in spread['providers']] == expected
    _, confirmed = request(base, '/api/rates/spread?code=99213&status=CONFIRMED_PPO')
    assert confirmed['providers'] and all(p['network_status'] == 'CONFIRMED_PPO' for p in confirmed['providers'])
    assert request(base, '/api/rates/spread')[0] == 400


def test_compare_lines_up_same_code_and_variant(live):
    base = live['base']
    _, spread = request(base, '/api/rates/spread?code=99214')
    cheap, dear = spread['providers'][0], spread['providers'][-1]
    pairs = f"{cheap['npi']}:{cheap['tin']},{dear['npi']}:{dear['tin']}"
    _, result = request(base, '/api/compare?pairs=' + pairs)
    keys = [p['key'] for p in result['providers']]
    assert keys == pairs.split(',')
    line = next(r for r in result['rows'] if r['code'] == '99214' and r['negotiated_type'] == spread['variant']['negotiated_type']
                and r['pos'] == spread['variant']['pos'])
    assert line['rates'][keys[0]] == cheap['rate'] and line['rates'][keys[1]] == dear['rate']
    assert line['cheapest'] == min(cheap['rate'], dear['rate'])
    assert line['market']['n'] >= 2
    if result['basket']:
        common = [r for r in result['rows'] if all(k in r['rates'] for k in keys)]
        assert result['basket'][keys[0]] == sum(r['rates'][keys[0]] for r in common)
    _, missing = request(base, '/api/compare?pairs=1234567893:000000000')
    assert missing['missing'] == ['1234567893:000000000'] and not missing['rows']
    assert request(base, '/api/compare?pairs=' + ','.join(f'1{i:09d}:1' for i in range(7)))[0] == 400
    assert request(base, '/api/compare?pairs=abc')[0] == 400


def test_tin_npi_address_tree(live):
    base, data = live['base'], live['data']
    _, overview = request(base, '/api/c5/overview')
    with sqlite3.connect(data.db) as con:
        assert overview['tins'] == con.execute('SELECT COUNT(DISTINCT tin) FROM c5_addresses').fetchone()[0]
        assert sum(overview['npisPerTin'].values()) == overview['tins']
        assert sum(overview['tinsPerNpi'].values()) == overview['npis']
        assert sum(overview['sitesPerPair'].values()) == overview['pairs']
        _, groups = request(base, '/api/c5/search')
        tin = groups['rows'][0]['tin']
        npis = {str(r[0]) for r in con.execute('SELECT DISTINCT npi FROM c5_addresses WHERE tin=?', (tin,))}
        ranks = con.execute("SELECT COUNT(*) FROM c5_addresses WHERE tin=? AND COALESCE(address,'')<>''", (tin,)).fetchone()[0]
    assert [r['npis'] for r in groups['rows']] == sorted((r['npis'] for r in groups['rows']), reverse=True)
    _, tree = request(base, '/api/c5/tin?tin=' + tin)
    assert {r['npi'] for r in tree['rows']} == npis and tree['npis'] == len(npis)
    assert sum(len(r['sites']) for r in tree['rows']) == ranks
    for r in tree['rows']:
        assert [s['rank'] for s in r['sites']] == list(range(1, len(r['sites']) + 1))
    npi = next(iter(npis))
    _, found = request(base, '/api/c5/search?q=' + npi)
    assert tin in {r['tin'] for r in found['rows']} and found['npi'] == npi
    assert request(base, '/api/c5/tin?tin=000')[0] == 404
