"""pipeline/inputs/: the runner picks up the HiLabs workbook and the saved PPO check without being told."""
from argparse import Namespace

from ptd_run import cli, inputs


def args(**kw):
    base = dict(inputs=None, workbook=None, ppo_check_csv=None, no_ppo_check_csv=False, check_directory=False,
                no_check_directory=False)
    return Namespace(**{**base, **kw})


def test_discovers_both_files(tmp_path):
    (tmp_path / inputs.WORKBOOK_NAME).write_bytes(b'x')
    (tmp_path / 'other.xlsx').write_bytes(b'x')
    (tmp_path / inputs.PPO_CHECK_NAME).write_text(C4_HEADER)
    found = inputs.discover(tmp_path)
    assert found['workbook'].endswith(inputs.WORKBOOK_NAME)
    assert found['ppo_check_csv'].endswith(inputs.PPO_CHECK_NAME)
    assert found['live_directory_check'] is False


def test_other_names_are_accepted(tmp_path):
    (tmp_path / 'weights.xlsx').write_bytes(b'x')
    (tmp_path / '~$weights.xlsx').write_bytes(b'x')
    (tmp_path / 'my_ppo_check_2026.csv').write_text(C4_HEADER)
    assert inputs.find_workbook(tmp_path).name == 'weights.xlsx'
    assert inputs.find_ppo_check(tmp_path).name == 'my_ppo_check_2026.csv'


def test_saved_check_is_used_and_site_not_searched(tmp_path):
    (tmp_path / inputs.WORKBOOK_NAME).write_bytes(b'x')
    (tmp_path / inputs.PPO_CHECK_NAME).write_text(C4_HEADER)
    a = args(inputs=tmp_path)
    cli._use_repo_inputs(a, 0, len(cli.STAGES) - 1)
    assert a.workbook.endswith(inputs.WORKBOOK_NAME) and a.ppo_check_csv.endswith(inputs.PPO_CHECK_NAME)
    assert a.check_directory is False


def test_command_line_wins(tmp_path):
    (tmp_path / inputs.WORKBOOK_NAME).write_bytes(b'x')
    (tmp_path / inputs.PPO_CHECK_NAME).write_text(C4_HEADER)
    a = args(inputs=tmp_path, workbook='mine.xlsx', ppo_check_csv='mine.csv')
    cli._use_repo_inputs(a, 0, len(cli.STAGES) - 1)
    assert (a.workbook, a.ppo_check_csv) == ('mine.xlsx', 'mine.csv')


def test_no_saved_check_turns_on_live_search(tmp_path, monkeypatch):
    monkeypatch.setattr(inputs, 'live_check_available', lambda: (True, ''))
    a = args(inputs=tmp_path)
    cli._use_repo_inputs(a, 0, len(cli.STAGES) - 1)
    assert a.check_directory is True and a.ppo_check_csv is None
    b = args(inputs=tmp_path, no_check_directory=True)
    cli._use_repo_inputs(b, 0, len(cli.STAGES) - 1)
    assert b.check_directory is False
    c = args(inputs=tmp_path)  # a run that starts after C4 never searches
    cli._use_repo_inputs(c, cli.STAGES.index('c5'), len(cli.STAGES) - 1)
    assert c.check_directory is False


def test_live_search_off_switch(tmp_path, monkeypatch):
    monkeypatch.setenv('PTD_DIRECTORY_CHECK', 'off')
    a = args(inputs=tmp_path)
    cli._use_repo_inputs(a, 0, len(cli.STAGES) - 1)
    assert a.check_directory is False


C4_HEADER = 'npi,tin,site_checked,network_searched,npi_search_result,directory_tin,directory_addresses,verdict\n'
LEGACY_HEADER = ('npi,rate_file_tin,search_location,npi_search_result,directory_name,directory_product,'
                 'tin_fallback_directory_npis,verdict\n')


def test_prefers_the_c4_table_with_most_npis(tmp_path):
    (tmp_path / 'ppo_check_results.csv').write_text(LEGACY_HEADER + '1093892275,94-1623075,"Yreka, CA",found,X,PPO,,FOUND_PPO_BY_NPI\n')
    (tmp_path / 'old_ppo_check.csv').write_text(C4_HEADER + '1003017351,20-1413484,yes,PPO,found,,,FOUND_PPO_BY_NPI\n')
    (tmp_path / 'notes.csv').write_text('a,b\n1,2\n')
    found = inputs.discover(tmp_path)
    assert found['ppo_check_csv'].endswith('old_ppo_check.csv') and found['ppo_check_layout'] == 'c4'
    assert found['ppo_check_npis'] == 1 and found['ignored_csv'] == ['notes.csv']
    (tmp_path / 'old_ppo_check.csv').unlink()
    found = inputs.discover(tmp_path)
    assert found['ppo_check_csv'].endswith('ppo_check_results.csv') and found['ppo_check_layout'] == 'legacy'


def test_c4_reads_the_early_layout_and_refuses_other_csvs(tmp_path):
    from ptd_c4 import ppo_check
    legacy = tmp_path / 'ppo_check_results.csv'
    legacy.write_text(LEGACY_HEADER
                      + '1093892275,94-1623075,"Yreka, CA",found,Fairchild,PPO | PPO,,FOUND_PPO_BY_NPI\n'
                      + '1447478714,30-0590479,"Los Angeles, CA",no results,,,1111111111 | 2222222222,NOT_FOUND_BY_NPI_SAME_TIN\n'
                      + '1740929694,88-2557073,,,,,,NOT_CHECKED\n')
    rows, cov = ppo_check.from_csv(legacy)
    assert set(cov) == {'1093892275', '1447478714'}
    assert cov['1093892275']['status'] == 'found' and cov['1447478714']['tin'] == '300590479'
    listed = [r for r in rows if r['npi'] == '1093892275']
    assert len(listed) == 1 and listed[0]['tin'] == '' and 'PPO' in listed[0]['network_name']
    assert {r['npi'] for r in rows if r['tin'] == '300590479'} == {'1111111111', '2222222222'}
    assert ppo_check.count_checked(legacy) == ('legacy', 2)
    wrong = tmp_path / 'wrong.csv'
    wrong.write_text('npi,name\n1,x\n')
    import pytest
    with pytest.raises(ValueError, match='not a PPO check table'):
        ppo_check.from_csv(wrong)
