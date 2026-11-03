from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from .models import JobSpec, utcnow


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"
    key: Mapped[str] = mapped_column(String(400), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    repository: Mapped[str] = mapped_column(String(250))
    sha: Mapped[str] = mapped_column(String(64))
    spec: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(16), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    next_run: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Delivery(Base):
    __tablename__ = "deliveries"
    id: Mapped[str] = mapped_column(String(150), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RepoSetting(Base):
    __tablename__ = "repository_settings"
    repository: Mapped[str] = mapped_column(String(250), primary_key=True)
    jev_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    approval_mode: Mapped[str | None] = mapped_column(String(24), nullable=True)
    auto_enqueue: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    llm_profile: Mapped[str | None] = mapped_column(String(48), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_repo_time", "repository", "created_at"),)
    fingerprint: Mapped[str] = mapped_column(String(500), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    repository: Mapped[str] = mapped_column(String(250), nullable=False)
    subject: Mapped[str] = mapped_column(String(400), nullable=False)
    sha: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    details: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Store:
    def __init__(self, url: str, *, auto_create: bool = True):
        kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite:") else {}
        self.engine = create_engine(url, pool_pre_ping=True, **kwargs)
        self.session = sessionmaker(self.engine, expire_on_commit=False)
        if auto_create:
            Base.metadata.create_all(self.engine)

    def enqueue(self, delivery: str, specs: list[JobSpec], debounce: int) -> int:
        with self.session.begin() as db:
            if db.get(Delivery, delivery):
                return 0
            db.add(Delivery(id=delivery))
            count = 0
            for spec in specs:
                row = db.get(Job, spec.key)
                if row is None:
                    db.add(Job(key=spec.key, kind=spec.kind, repository=spec.repository,
                               sha=spec.sha, spec=spec.model_dump_json(), status="queued",
                               next_run=utcnow() + timedelta(seconds=debounce if spec.kind == "pr" else 0)))
                    count += 1
                elif spec.force:
                    # Human-review events reuse the existing verdict; edits clear it.
                    state = json.loads(row.state)
                    if spec.trigger in {"pull_request.edited", "admin.recheck"}:
                        state = {}
                    row.state = json.dumps(state)
                    row.status = "queued"
                    row.spec = spec.model_dump_json()
                    row.next_run = utcnow() + timedelta(seconds=debounce if spec.kind == "pr" else 0)
                    row.updated_at = utcnow()
                    row.generation += 1
                    count += 1
            return count

    def claim(self) -> tuple[str, int, JobSpec, dict] | None:
        """One-at-a-time lease. SKIP LOCKED also permits PostgreSQL workers if scaled later."""
        now = utcnow()
        with self.session.begin() as db:
            statement = (
                select(Job).where(
                    ((Job.status == "queued") & (Job.next_run <= now))
                    | ((Job.status == "working") & (Job.leased_until < now))
                ).order_by(Job.next_run).limit(1)
            )
            if self.engine.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            row = db.execute(statement).scalar_one_or_none()
            if row is None:
                return None
            row.status = "working"
            row.leased_until = now + timedelta(minutes=10)
            row.updated_at = now
            return row.key, row.generation, JobSpec.model_validate_json(row.spec), json.loads(row.state)

    def complete(self, key: str, generation: int, state: dict, status: str = "done", error: str = "") -> None:
        with self.session.begin() as db:
            row = db.get(Job, key)
            if row.generation != generation:
                return
            row.status, row.state, row.error = status, json.dumps(state), error[:1000]
            row.leased_until = None
            row.updated_at = utcnow()
            if row.kind in ("pr", "queue"):
                fingerprint = f"job:{key}:{generation}"
                if db.get(AuditEvent, fingerprint) is None:
                    verdict = (state.get("verdict") or {}).get("verdict", "not_evaluated")
                    evidence = {"status": status, "verdict": verdict,
                                "risk": state.get("risk"), "llm_profile": state.get("llm_profile", "default"),
                                "ci_summary_sha256": hashlib.sha256(
                                    str(state.get("ci_summary", "")).encode("utf-8")).hexdigest(),
                                "token_usage": {k: (state.get("usage") or {}).get(k, 0)
                                                for k in ("prompt_tokens", "completion_tokens")}}
                    db.add(AuditEvent(fingerprint=fingerprint, event_type="review_decision",
                                      repository=row.repository, subject=key[:400], sha=row.sha,
                                      details=json.dumps(evidence)))

    def requeue(self, key: str, generation: int, state: dict, delay: int, reason: str, *, count: bool = False) -> int:
        with self.session.begin() as db:
            row = db.get(Job, key)
            if row.generation != generation:
                return row.attempts
            row.status, row.state, row.error = "queued", json.dumps(state), reason[:1000]
            row.next_run = utcnow() + timedelta(seconds=delay)
            row.leased_until = None
            row.updated_at = utcnow()
            if count:
                row.attempts += 1
            return row.attempts

    def classification(self, repository: str, number: int, sha: str) -> dict | None:
        key = f"{repository}:{number}:label:{sha}"
        with self.session() as db:
            row = db.get(Job, key)
            if row is None or row.status != "done":
                return None
            return json.loads(row.state)

    def repo_settings(self, repository: str) -> dict:
        with self.session() as db:
            item = db.get(RepoSetting, repository.lower())
            if item is None:
                return {"jev_enabled": None, "approval_mode": None, "auto_enqueue": None, "llm_profile": None}
            return {"jev_enabled": item.jev_enabled, "approval_mode": item.approval_mode,
                    "auto_enqueue": item.auto_enqueue, "llm_profile": item.llm_profile}

    def update_repo_settings(self, repository: str, values: dict) -> dict:
        with self.session.begin() as db:
            item = db.get(RepoSetting, repository.lower())
            if item is None:
                item = RepoSetting(repository=repository.lower())
                db.add(item)
            for key in ("jev_enabled", "approval_mode", "auto_enqueue", "llm_profile"):
                if key in values:
                    setattr(item, key, values[key])
            item.updated_at = utcnow()
            result = {"jev_enabled": item.jev_enabled, "approval_mode": item.approval_mode,
                      "auto_enqueue": item.auto_enqueue, "llm_profile": item.llm_profile}
            # Persist only changed setting values. Never persist model credentials here.
            from uuid import uuid4
            db.add(AuditEvent(fingerprint=f"policy:{uuid4().hex}", event_type="policy_updated",
                              repository=repository.lower(), subject="repository_settings", sha="",
                              details=json.dumps({k: values[k] for k in values})))
        return result

    def audit(self, repository: str | None = None, limit: int = 50) -> list[dict]:
        with self.session() as db:
            stmt = select(AuditEvent).order_by(AuditEvent.created_at.desc(), AuditEvent.fingerprint.desc())
            if repository:
                stmt = stmt.where(AuditEvent.repository == repository.lower())
            rows = db.execute(stmt.limit(limit)).scalars().all()
            return [{"event_type": row.event_type, "repository": row.repository,
                     "subject": row.subject, "sha": row.sha, "details": json.loads(row.details),
                     "created_at": row.created_at.isoformat()} for row in rows]

    def metrics(self, days: int = 30) -> dict:
        from collections import Counter
        cutoff = utcnow() - timedelta(days=days)
        with self.session() as db:
            rows = db.execute(select(Job).where(Job.updated_at >= cutoff)).scalars().all()
            by_status = Counter(row.status for row in rows)
            by_kind = Counter(row.kind for row in rows)
            verdicts = Counter()
            tokens = {"prompt_tokens": 0, "completion_tokens": 0}
            for row in rows:
                state = json.loads(row.state)
                verdict = (state.get("verdict") or {}).get("verdict")
                if verdict:
                    verdicts[verdict] += 1
                usage = state.get("usage") or {}
                for key in tokens:
                    tokens[key] += int(usage.get(key, 0) or 0)
            return {"days": days, "jobs_total": len(rows), "status": dict(by_status),
                    "kind": dict(by_kind), "verdicts": dict(verdicts), "tokens": tokens}

    def recent_jobs(self, limit: int = 30) -> list[dict]:
        with self.session() as db:
            items = db.execute(select(Job).order_by(Job.updated_at.desc()).limit(limit)).scalars().all()
            def safe_details(item: Job) -> dict:
                state = json.loads(item.state)
                return {key: state.get(key) for key in
                        ("risk", "jev_status", "enqueue_status", "enqueue_error") if key in state} | {
                    "verdict": (state.get("verdict") or {}).get("verdict"),
                    "token_usage": {k: (state.get("usage") or {}).get(k, 0)
                                    for k in ("prompt_tokens", "completion_tokens")}}
            return [{"key": item.key, "status": item.status, "kind": item.kind,
                     "repository": item.repository, "sha": item.sha, "error": item.error,
                     "details": safe_details(item),
                     "updated_at": item.updated_at.isoformat() if item.updated_at else None}
                    for item in items]

    def status_summary(self) -> dict:
        from sqlalchemy import func
        with self.session() as db:
            rows = db.execute(select(Job.status, func.count()).group_by(Job.status)).all()
            return dict(rows)
