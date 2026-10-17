from pathlib import Path

import pytest

from ptd_pipeline.scraper import ScrapeError, parse_toc_links

FIXTURE = Path(__file__).parent / "fixtures" / "centene_page.html"
BASE = "https://www.centene.com/price-transparency-files.html"


@pytest.fixture(scope="module")
def links():
    return parse_toc_links(FIXTURE.read_text(encoding="utf-8"), base_url=BASE)


def test_only_toc_json_links_are_returned(links):
    names = [l.filename for l in links]
    assert names == [
        "2099-01-31_ambetter_index.json",
        "2099-01-31_fidelis_index.json",
        "2099-01-31_qualchoice_index.json",
        "2099-01-31_qualchoice_alf_plan_index.json",
        "2099-01-31_qualchoice_big_four_index.json",
        "2099-02-02_healthnet_index.json",
    ]


def test_employee_section_excluded(links):
    urls = " ".join(l.url for l in links).lower()
    for blocked in ("cigna", "hmhs", "hmsa", "anthem", "healthnet.com", "employee_index"):
        assert blocked not in urls


def test_urls_are_absolute(links):
    assert all(l.url.startswith("https://www.centene.com/content/dam/") for l in links)


def test_brand_and_sub_index(links):
    by_name = {l.filename: l for l in links}
    assert by_name["2099-01-31_ambetter_index.json"].brand == "Ambetter"
    assert by_name["2099-02-02_healthnet_index.json"].brand == "Health Net"
    sub = by_name["2099-01-31_qualchoice_alf_plan_index.json"]
    assert sub.is_sub_index and sub.brand == "QualChoice"
    assert sub.parent_url.endswith("2099-01-31_qualchoice_index.json")
    assert not by_name["2099-01-31_qualchoice_index.json"].is_sub_index


def test_dates_are_parsed_not_hardcoded(links):
    assert {l.file_date for l in links} == {"2099-01-31", "2099-02-02"}


def test_missing_heading_raises():
    with pytest.raises(ScrapeError):
        parse_toc_links("<html><body><h2>Nothing here</h2></body></html>", base_url=BASE)
