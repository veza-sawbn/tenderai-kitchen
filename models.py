from datetime import datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


def utcnow():
    return datetime.now(timezone.utc)


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(255), nullable=True)
    capabilities_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    locations_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    issues: Mapped[list["ProfileIssue"]] = relationship("ProfileIssue", back_populates="profile", cascade="all, delete-orphan")
    analysis_jobs: Mapped[list["AnalysisJob"]] = relationship("AnalysisJob", back_populates="profile")

    def capability_list(self) -> list[str]:
        if not self.capabilities_text:
            return []
        return [x.strip() for x in self.capabilities_text.split(",") if x.strip()]

    def location_list(self) -> list[str]:
        if not self.locations_text:
            return []
        return [x.strip() for x in self.locations_text.split(",") if x.strip()]


class ProfileIssue(Base):
    __tablename__ = "profile_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), nullable=False, index=True)
    issue_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    penalty_weight: Mapped[float] = mapped_column(Float, default=5.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    profile: Mapped["Profile"] = relationship("Profile", back_populates="issues")


class TenderCache(Base):
    __tablename__ = "tenders_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tender_uid: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    ocid: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    source_release_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    buyer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    province: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    tender_type: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    industry: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    issued_date: Mapped[Date | None] = mapped_column(Date, nullable=True, index=True)
    closing_date: Mapped[Date | None] = mapped_column(Date, nullable=True, index=True)
    document_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_live: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    documents: Mapped[list["TenderDocumentCache"]] = relationship(
        "TenderDocumentCache", back_populates="tender", cascade="all, delete-orphan"
    )
    analysis_jobs: Mapped[list["AnalysisJob"]] = relationship("AnalysisJob", back_populates="tender")


class TenderDocumentCache(Base):
    __tablename__ = "tender_documents_cache"
    __table_args__ = (
        UniqueConstraint("tender_id", "document_url", name="uq_tender_document_url"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tender_id: Mapped[int] = mapped_column(ForeignKey("tenders_cache.id"), nullable=False, index=True)
    document_url: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetch_status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    binary_content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    tender: Mapped["TenderCache"] = relationship("TenderCache", back_populates="documents")


class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("profiles.id"), nullable=True, index=True)
    tender_id: Mapped[int | None] = mapped_column(ForeignKey("tenders_cache.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    strengths_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    risks_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommendations_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposal_draft_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    profile: Mapped["Profile"] = relationship("Profile", back_populates="analysis_jobs")
    tender: Mapped["TenderCache"] = relationship("TenderCache", back_populates="analysis_jobs")


class IngestRun(Base):
    __tablename__ = "ingest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(30), default="running", nullable=False)
    pages_attempted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_succeeded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tenders_seen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tenders_upserted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
