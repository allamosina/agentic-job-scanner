from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel

from .config import Source, fingerprint


class JobData(BaseModel):
    company: str
    title: str
    location: str
    url: str
    official_url: str | None = None
    ats_id: str | None = None
    description: str
    remote_policy: str = "Unknown"
    employment_type: str = "Unknown"
    posted_at: str | None = None
    valid_through: str | None = None
    salary: dict | None = None
    source_id: str
    discovered_url: str

    def material(self):
        return self.model_dump(exclude={"source_id", "discovered_url", "url", "official_url"})

    def content_hash(self):
        return fingerprint(self.material())


@dataclass
class FetchResult:
    jobs: list[JobData] = field(default_factory=list)
    complete: bool = False
    note: str = ""


def text(html_value: str) -> str:
    return " ".join(BeautifulSoup(html.unescape(html_value or ""), "html.parser").get_text(" ").split())


def same_company(left: str, right: str) -> bool:
    def normalize(value):
        value = re.sub(r"\b(inc|ltd|limited|llc|corporation|corp)\b", "", value.casefold())
        return re.sub(r"[^a-z0-9]", "", value)

    return bool(normalize(left)) and normalize(left) == normalize(right)


def canonical_url(url: str) -> str:
    u = urlsplit(url)
    # Preserve job IDs in queries, especially gh_jid; only strip tracking.
    query = [
        (k, v)
        for k, v in parse_qsl(u.query)
        if not k.lower().startswith("utm_") and k.lower() not in {"gh_src", "source", "ref", "referrer"}
    ]
    return urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path.rstrip("/"), urlencode(sorted(query)), ""))


def identity(job: JobData) -> str:
    url = canonical_url(job.official_url or job.url)
    u = urlsplit(url)
    parts = u.path.strip("/").split("/")
    if u.hostname in {"jobs.lever.co", "jobs.eu.lever.co", "jobs.ashbyhq.com"} and len(parts) >= 2:
        return f"ats:{u.hostname}:{parts[0]}:{parts[1]}"
    match = re.search(r"/([^/]+)/jobs/(\d+)", u.path)
    if u.hostname in {"boards.greenhouse.io", "job-boards.greenhouse.io"} and match:
        return f"ats:greenhouse:{match[1]}:{match[2]}"
    if job.ats_id:
        return f"ats:{job.company.casefold()}:{job.ats_id}"
    return url


async def check_public_url(url: str):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTP(S) URLs without credentials are allowed")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("Non-standard port")
    infos = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, parsed.port or 443)
    if not infos or any(not ipaddress.ip_address(i[4][0]).is_global for i in infos):
        raise ValueError("Private/local network address is not a vacancy source")
    return infos[0][4][0]


class PublicTransport(httpx.AsyncBaseTransport):
    """Pin each connection to its checked IP; retain original HTTP Host and TLS SNI.

    No keepalive: two distinct HTTPS origins can share an IP but must never reuse
    a TLS connection under the IP-based pool key. The outer client still sees the
    original hostname, including for cookies and redirect processing.
    """

    def __init__(self):
        self.inner = httpx.AsyncHTTPTransport(
            trust_env=False, limits=httpx.Limits(max_connections=10, max_keepalive_connections=0)
        )

    async def handle_async_request(self, request):
        address = await check_public_url(str(request.url))
        headers = request.headers.copy()
        headers["Host"] = request.url.netloc.decode("ascii")
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=address),
            headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": request.url.host},
        )
        return await self.inner.handle_async_request(pinned)

    async def aclose(self):
        await self.inner.aclose()


class Web:
    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=25,
            follow_redirects=False,
            headers={"User-Agent": "PersonalJobMonitor/0.1"},
            transport=PublicTransport(),
            trust_env=False,
        )
        self.robots: dict[str, RobotFileParser] = {}

    async def close(self):
        await self.client.aclose()

    async def get(self, url, headers=None, params=None):
        for _ in range(6):
            await check_public_url(url)
            async with self.client.stream("GET", url, headers=headers, params=params) as response:
                if response.is_redirect:
                    url = urljoin(str(response.url), response.headers["location"])
                    # No credentials carried across redirects.
                    headers, params = None, None
                    continue
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 8_000_000:
                        raise ValueError("Page exceeds 8 MB limit")
                return httpx.Response(
                    response.status_code,
                    headers={
                        k: v
                        for k, v in response.headers.items()
                        if k.lower() not in {"content-encoding", "content-length"}
                    },
                    content=bytes(content),
                    request=response.request,
                )
        raise ValueError("Too many redirects")

    async def page(self, url):
        origin = urlunsplit((*urlsplit(url)[:2], "", "", ""))
        if origin not in self.robots:
            parser = RobotFileParser()
            try:
                response = await self.get(origin + "/robots.txt")
                parser.parse(response.text.splitlines())
            except httpx.HTTPStatusError as error:
                if error.response.status_code == 404:
                    parser.parse([])
                else:
                    raise
            self.robots[origin] = parser
        if not self.robots[origin].can_fetch("PersonalJobMonitor", url):
            raise ValueError("robots.txt disallows this page")
        return await self.get(url)


def timestamp(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, UTC).isoformat()
    return value or None


def parse_ats(source: Source, data) -> list[JobData]:
    result = []
    rows = data if source.kind == "lever" else data["jobs"]
    for row in rows:
        if source.kind == "greenhouse":
            url = row["absolute_url"]
            title, location, desc = (
                row["title"],
                row.get("location", {}).get("name", "Unknown"),
                row.get("content", ""),
            )
            # updated_at is deliberately not used as publication date.
            posted, remote, employment, salary = row.get("first_published"), "Unknown", "Unknown", None
        elif source.kind == "lever":
            url = row["hostedUrl"]
            title, location = row["text"], row.get("categories", {}).get("location", "Unknown")
            desc = (
                row.get("descriptionPlain", row.get("description", ""))
                + " "
                + " ".join(
                    section.get("text", "") + " " + section.get("content", "")
                    for section in row.get("lists", [])
                )
            )
            desc += " " + row.get("additionalPlain", row.get("additional", ""))
            posted, remote = timestamp(row.get("createdAt")), row.get("workplaceType", "Unknown")
            employment, salary = (
                row.get("categories", {}).get("commitment", "Unknown"),
                row.get("salaryRange"),
            )
        else:
            if row.get("isListed") is False:
                continue
            url = row["jobUrl"]
            title, location = row["title"], row.get("location", "Unknown")
            desc = row.get("descriptionHtml", row.get("descriptionPlain", ""))
            posted = row.get("publishedAt")
            remote = row.get("workplaceType") or ("Remote" if row.get("isRemote") else "Unknown")
            employment, salary = row.get("employmentType", "Unknown"), row.get("compensation")
        result.append(
            JobData(
                company=source.company or "Unknown",
                title=title,
                location=location,
                url=url,
                official_url=url,
                ats_id=f"{source.kind}:{row['id']}",
                description=text(desc),
                posted_at=posted,
                remote_policy=remote,
                employment_type=employment,
                salary=salary,
                source_id=source.id,
                discovered_url=url,
            )
        )
    return result


def jsonld_jobs(body: str, url: str, source: Source) -> list[JobData]:
    soup, nodes = BeautifulSoup(body, "html.parser"), []

    def visit(node):
        if isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, dict):
            types = node.get("@type", [])
            if types == "JobPosting" or (isinstance(types, list) and "JobPosting" in types):
                nodes.append(node)
            for key in ("@graph", "itemListElement", "item"):
                if key in node:
                    visit(node[key])

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            visit(json.loads(script.get_text()))
        except (ValueError, TypeError):
            continue
    jobs = []
    for node in nodes:
        if not node.get("title") or not node.get("description"):
            continue
        company = node.get("hiringOrganization", {})
        company = company.get("name", "Unknown") if isinstance(company, dict) else "Unknown"
        if source.company and not same_company(source.company, company):
            continue
        locations = node.get("jobLocation", [])
        if isinstance(locations, dict):
            locations = [locations]
        location = "; ".join(
            text(json.dumps(i.get("address", i), ensure_ascii=False))
            for i in locations
            if isinstance(i, dict)
        )
        role_url = urljoin(url, node.get("url") or url)
        salary = node.get("baseSalary")
        jobs.append(
            JobData(
                company=source.company or company,
                title=node["title"],
                location=location or "Unknown",
                url=role_url,
                official_url=role_url if source.employer_source else None,
                description=text(node["description"]),
                remote_policy=str(node.get("jobLocationType", "Unknown")),
                employment_type=str(node.get("employmentType", "Unknown")),
                posted_at=node.get("datePosted"),
                valid_through=node.get("validThrough"),
                salary=salary if isinstance(salary, dict) else None,
                source_id=source.id,
                discovered_url=url,
            )
        )
    return jobs


async def fetch_source(web: Web, source: Source, max_pages: int) -> FetchResult:
    if source.kind == "greenhouse":
        response = await web.get(
            f"https://boards-api.greenhouse.io/v1/boards/{source.board}/jobs?content=true"
        )
        return FetchResult(parse_ats(source, response.json()), True)
    if source.kind == "ashby":
        response = await web.get(
            f"https://api.ashbyhq.com/posting-api/job-board/{source.board}?includeCompensation=true"
        )
        return FetchResult(parse_ats(source, response.json()), True)
    if source.kind == "lever":
        jobs = []
        for page in range(max_pages):
            response = await web.get(
                f"https://api.lever.co/v0/postings/{source.board}",
                params={"mode": "json", "skip": page * 100, "limit": 100},
            )
            rows = response.json()
            jobs.extend(parse_ats(source, rows))
            if len(rows) < 100:
                return FetchResult(jobs, True)
        return FetchResult(jobs, False, "Pagination limit reached")
    if not source.url:
        return FetchResult([], False, "URL missing")
    response = await web.page(source.url)
    jobs = jsonld_jobs(response.text, str(response.url), source)
    soup = BeautifulSoup(response.text, "html.parser")
    links = list(
        dict.fromkeys(
            urljoin(str(response.url), a["href"])
            for a in soup.select("a[href]")
            if re.search(r"/(jobs?|careers?|positions?)/.+", a["href"])
        )
    )
    # Bounded traversal, explicitly reported as partial (never claimed full coverage).
    for link in links[: max_pages - 1]:
        if urlsplit(link).hostname != urlsplit(str(response.url)).hostname:
            continue
        try:
            page = await web.page(link)
            jobs.extend(jsonld_jobs(page.text, str(page.url), source))
        except (httpx.HTTPError, ValueError):
            continue
    unique = {identity(j): j for j in jobs}
    return FetchResult(
        list(unique.values()),
        False,
        "Bounded public JSON-LD discovery; full board coverage not verified"
        if jobs
        else "No machine-readable JobPosting found; source not successfully scanned",
    )
