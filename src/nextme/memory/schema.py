"""Pydantic schemas for per-user persistent memory."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Fact(BaseModel):
    """A single remembered fact about a user or their environment."""

    text: str
    confidence: float = 0.9
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime | None = None
    source: str = "conversation"


class FactStore(BaseModel):
    """Collection of facts for a single context."""

    facts: list[Fact] = Field(default_factory=list)


class UserContextMemory(BaseModel):
    """User preferences and interaction style."""

    preferred_language: str = "zh"
    communication_style: str = ""
    notes: str = ""
    updated_at: datetime = Field(default_factory=datetime.now)


class PersonalInfo(BaseModel):
    """User personal info."""

    name: str = ""
    timezone: str = ""
    role: str = ""
    updated_at: datetime = Field(default_factory=datetime.now)
