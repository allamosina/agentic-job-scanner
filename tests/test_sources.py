import json

import pytest

from job_monitor.config import Source
from job_monitor.sources import canonical_url, check_public_url, identity, jsonld_jobs, parse_ats


def test_tracking_removed_job_query_preserved():
    assert (
        canonical_url("https://EXAMPLE.com/career?utm_source=x&gh_jid=42#apply")
        == "https://example.com/career?gh_jid=42"
    )


def test_same_title_different_ids_remain_separate(job_data):
    other = job_data.model_copy(
        update={
            "ats_id": "greenhouse:43",
            "url": "https://example.com/jobs/43",
            "official_url": "https://example.com/jobs/43",
        }
    )
    assert identity(job_data) != identity(other)


def test_greenhouse_updated_not_used_as_posted():
    source = Source(id="test", kind="greenhouse", company="Example", board="example")
    job = parse_ats(
        source,
        {
            "jobs": [
                {
                    "id": 42,
                    "title": "Web Lead",
                    "absolute_url": "https://example.com/jobs/42",
                    "updated_at": "2026-09-08",
                    "content": "<p>Own web strategy</p>",
                }
            ]
        },
    )[0]
    assert job.posted_at is None
    assert job.description == "Own web strategy"


def test_ashby_unlisted_skipped():
    source = Source(id="a", kind="ashby", company="Example", board="example")
    assert parse_ats(source, {"jobs": [{"isListed": False}]}) == []


def test_jsonld_graph_without_official_proof():
    node = {
        "@graph": [
            {
                "@type": "JobPosting",
                "title": "Web Lead",
                "description": "<p>Website strategy</p>",
                "hiringOrganization": {"name": "Example"},
                "url": "/jobs/42",
            }
        ]
    }
    source = Source(id="board", kind="jsonld", url="https://board.example/jobs")
    jobs = jsonld_jobs(
        '<script type="application/ld+json">' + json.dumps(node) + "</script>", source.url, source
    )
    assert len(jobs) == 1
    assert jobs[0].official_url is None
    assert jobs[0].company == "Example"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/jobs",
        "http://[::1]/jobs",
        "http://169.254.169.254/latest/meta-data/",
        "https://user:pass@example.com/jobs",
    ],
)
async def test_private_urls_rejected(url):
    with pytest.raises(ValueError):
        await check_public_url(url)
