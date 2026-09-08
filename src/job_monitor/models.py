from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow():
    return datetime.now(UTC)


def new_id():
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    canonical_key: Mapped[str] = mapped_column(Text, unique=True)
    company: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    location: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text)
    official_url: Mapped[str | None] = mapped_column(Text)
    ats_id: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    current_hash: Mapped[str] = mapped_column(String(64))


class Version(Base):
    __tablename__ = "job_versions"
    __table_args__ = (UniqueConstraint("job_id", "content_hash"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Observation(Base):
    __tablename__ = "observations"
    __table_args__ = (UniqueConstraint("source_id", "source_key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    source_id: Mapped[str] = mapped_column(Text)
    source_key: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Evaluation(Base):
    __tablename__ = "evaluations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    version_id: Mapped[str] = mapped_column(ForeignKey("job_versions.id"), index=True)
    policy_hash: Mapped[str] = mapped_column(String(64))
    profile_hash: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(Text)
    result: Mapped[dict] = mapped_column(JSON)
    score: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(Text)
    eligible: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Research(Base):
    __tablename__ = "company_research"
    company: Mapped[str] = mapped_column(Text, primary_key=True)
    evidence: Mapped[list] = mapped_column(JSON)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Run(Base):
    __tablename__ = "source_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(Text, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, default="running")
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    event_key: Mapped[str] = mapped_column(Text, unique=True)
    user_id: Mapped[str] = mapped_column(String(32))
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    action: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("chat_id", "version_id"),
        UniqueConstraint("chat_id", "slot", "kind"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    chat_id: Mapped[str] = mapped_column(String(32))
    slot: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    version_id: Mapped[str | None] = mapped_column(ForeignKey("job_versions.id"))
    evaluation_id: Mapped[str | None] = mapped_column(ForeignKey("evaluations.id"))
    body: Mapped[str] = mapped_column(Text)
    buttons: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(Text, default="pending")
    message_id: Mapped[int | None] = mapped_column(Integer)
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class State(Base):
    __tablename__ = "runtime_state"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class NotionApplication(Base):
    __tablename__ = "notion_applications"
    page_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    data_source_id: Mapped[str] = mapped_column(String(36))
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    max_observed_stage: Mapped[int | None] = mapped_column(Integer)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    missing: Mapped[bool] = mapped_column(Boolean, default=False)


class ApplicationEvent(Base):
    __tablename__ = "application_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    page_id: Mapped[str] = mapped_column(ForeignKey("notion_applications.page_id"), index=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApplicationLink(Base):
    __tablename__ = "application_links"
    __table_args__ = (UniqueConstraint("page_id", "job_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    page_id: Mapped[str] = mapped_column(ForeignKey("notion_applications.page_id"))
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))


class Usage(Base):
    __tablename__ = "api_usage"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(Text, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(Text, default="reserved")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float)


class Signal(Base):
    __tablename__ = "outbound_signals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    evidence_key: Mapped[str] = mapped_column(String(64), unique=True)
    company: Mapped[str] = mapped_column(Text)
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
