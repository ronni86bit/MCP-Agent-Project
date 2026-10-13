"""Typed inputs for the github-mcp tools."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CommitsInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=10, ge=1, le=30)
    branch: str = Field(default="main", min_length=1, max_length=120)


class LimitInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=10, ge=1, le=30)


class ActivityInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: int = Field(default=14, ge=1, le=90)
    limit: int = Field(default=30, ge=1, le=30)
