import uuid

from pydantic import BaseModel, Field


class Folder(BaseModel):
    """Folder model matching Nylas API structure."""

    id: str
    grant_id: str
    name: str
    system_folder: bool = True
    attributes: list[str] = Field(default_factory=list)


class FolderResponse(BaseModel):
    """Response model for getting a single folder."""

    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    data: Folder
