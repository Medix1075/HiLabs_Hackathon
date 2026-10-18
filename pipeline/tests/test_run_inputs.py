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
    (tmp_path / inputs.PPO_CHECK_NAME).write_text('npi\n')
    found = inputs.discover(tmp_path)
    assert found['workbook'].endswith(inputs.WORKBOOK_NAME)
    assert found['ppo_check_csv'].endswith(inputs.PPO_CHECK_NAME)
    assert found['live_directory_check'] is False


def test_other_names_are_accepted(tmp_path):
    (tmp_path / 'weights.xlsx').write_bytes(b'x')
    (tmp_path / '~$weights.xlsx').write_bytes(b'x')
    (tmp_path / 'my_ppo_check_2026.csv').write_text('npi\n')
    assert inputs.find_workbook(tmp_path).name == 'weights.xlsx'
    assert inputs.find_ppo_check(tmp_path).name == 'my_ppo_check_2026.csv'


def test_saved_check_is_used_and_site_not_searched(tmp_path):
    (tmp_path / inputs.WORKBOOK_NAME).write_bytes(b'x')
    (tmp_path / inputs.PPO_CHECK_NAME).write_text('npi\n')
    a = args(inputs=tmp_path)
    cli._use_repo_inputs(a, 0, len(cli.STAGES) - 1)
    assert a.workbook.endswith(inputs.WORKBOOK_NAME) and a.ppo_check_csv.endswith(inputs.PPO_CHECK_NAME)
    assert a.check_directory is False


def test_command_line_wins(tmp_path):
    (tmp_path / inputs.WORKBOOK_NAME).write_bytes(b'x')
    (tmp_path / inputs.PPO_CHECK_NAME).write_text('npi\n')
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
