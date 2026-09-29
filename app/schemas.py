"""FastAPI 请求/响应模型。"""

from pydantic import BaseModel, Field


class CreateTaskIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TransitionIn(BaseModel):
    new_status: str
    expected_version: int = Field(ge=1)
    note: str | None = None


class ClaimIn(BaseModel):
    executor: str = Field(min_length=1, max_length=100)


class BulkCancelIn(BaseModel):
    tasks: dict[int, int]
