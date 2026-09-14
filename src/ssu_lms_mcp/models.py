"""LMS 조회 결과 모델."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

SUBMISSION_TYPE_LABELS = {
    "online_upload": "과제(보고서)",
    "online_text_entry": "에세이",
    "online_quiz": "퀴즈",
    "discussion_topic": "토론",
}


def submission_type_label(raw: str | None) -> str:
    return SUBMISSION_TYPE_LABELS.get(raw or "", "기타")


class Course(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    name: str
    course_code: str = ""
    term: str = ""
    professor: str = ""
    credits: float | None = None
    progress: float | None = None
    materials: int = 0


class Notice(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    course_id: int
    title: str
    message: str = ""
    message_text: str = ""
    posted_at: str | None = None
    author: str = ""
    html_url: str = ""
    is_read: bool = False
    pinned: bool = False
    attachments: list[NoticeAttachment] = Field(default_factory=list)


class NoticeAttachment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    name: str
    url: str | None = None
    size: int | None = None
    content_type: str | None = None


class BoardPost(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    course_id: int
    board_id: int = 0
    board_name: str = ""
    title: str = ""
    message_text: str = ""
    posted_at: str | None = None
    author: str = ""
    html_url: str = ""
    attachments: list[NoticeAttachment] = Field(default_factory=list)


class Assignment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    course_id: int
    title: str
    due_at: str | None = None
    points_possible: float | None = None
    submission_types: list[str] = Field(default_factory=list)
    html_url: str = ""
    description: str = ""
    description_text: str = ""
    submitted: bool = False

    @property
    def due_date(self) -> datetime | None:
        return datetime.fromisoformat(self.due_at.replace("Z", "+00:00")) if self.due_at else None


class Material(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    course_id: int
    module_name: str = ""
    title: str = ""
    item_type: str = ""
    url: str | None = None
    position: int = 0
    content_id: int | None = None
    module_id: int | None = None
