from sqlalchemy import func, select, text

from .models import Feedback, Job, Observation, State, Usage, Version, utcnow
from .sources import JobData, canonical_url, identity


def save_job(session, data: JobData):
    key = identity(data)
    url = canonical_url(data.official_url or data.url)
    job = session.scalar(select(Job).where(Job.canonical_key == key))
    if job is None:
        job = session.scalar(select(Job).where(Job.canonical_url == url))
    new = job is None
    if new:
        job = Job(
            canonical_key=key,
            company=data.company,
            title=data.title,
            location=data.location,
            canonical_url=url,
            official_url=data.official_url,
            ats_id=data.ats_id,
            current_hash=data.content_hash(),
        )
        session.add(job)
        session.flush()
    job.last_seen, job.active = utcnow(), True
    # Do not downgrade a verified official JD to a partial aggregator description.
    authoritative = new or data.official_url or not job.official_url
    if authoritative:
        job.title, job.location, job.current_hash = data.title, data.location, data.content_hash()
        job.official_url = data.official_url or job.official_url
        if data.official_url:
            job.canonical_url = canonical_url(data.official_url)
    version = session.scalar(
        select(Version).where(Version.job_id == job.id, Version.content_hash == job.current_hash)
    )
    changed = version is None
    if changed:
        version = Version(job_id=job.id, content_hash=job.current_hash, payload=data.model_dump())
        session.add(version)
        session.flush()
    source_key = canonical_url(data.discovered_url) + "|" + identity(data)
    observation = session.scalar(
        select(Observation).where(
            Observation.source_id == data.source_id, Observation.source_key == source_key
        )
    )
    if observation is None:
        session.add(
            Observation(
                job_id=job.id, source_id=data.source_id, source_key=source_key, url=data.discovered_url
            )
        )
    else:
        observation.last_seen = utcnow()
    return job.id, new, changed


class BudgetUnavailable(RuntimeError):
    pass


def reserve_call(factory, provider, cap):
    if cap <= 0:
        raise BudgetUnavailable(f"{provider} disabled: daily cap is zero")
    with factory.begin() as session:
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": "budget:" + provider})
        today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        count = session.scalar(
            select(func.count())
            .select_from(Usage)
            .where(Usage.provider == provider, Usage.created_at >= today)
        )
        if count >= cap:
            raise BudgetUnavailable(f"{provider} daily call cap reached")
        usage = Usage(provider=provider)
        session.add(usage)
        session.flush()
        return usage.id


def finish_call(factory, usage_id, status, input_tokens=0, output_tokens=0):
    with factory.begin() as session:
        usage = session.get(Usage, usage_id)
        usage.status, usage.input_tokens, usage.output_tokens = status, input_tokens, output_tokens


def feedback_summary(session, user_id):
    from datetime import datetime

    query = select(Feedback).where(Feedback.user_id == str(user_id))
    reset = session.get(State, "learning_reset")
    if reset:
        query = query.where(Feedback.created_at > datetime.fromisoformat(reset.value["at"]))
    rows = session.scalars(query.order_by(Feedback.created_at.desc())).all()
    # Latest explicit rating per job; saved/applied/reasons alone aren't positive/negative labels.
    ratings = {}
    for row in rows:
        if row.action in {"like", "dislike"} and row.job_id not in ratings:
            ratings[row.job_id] = row.action
    return ratings


def feedback_boost(session, user_id, track: str, evaluation_tracks: dict[str, str]) -> float:
    ratings = feedback_summary(session, user_id)
    votes = [
        1 if action == "like" else -1
        for jid, action in ratings.items()
        if evaluation_tracks.get(jid) == track
    ]
    # Smooth sparse observations; no feedback => zero. Ranking only, not score or eligibility.
    return 5 * sum(votes) / (len(votes) + 4)
