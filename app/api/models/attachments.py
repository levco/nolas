from pydantic import BaseModel


class AttachmentMetadata(BaseModel):
    """Metadata for an attachment."""

    id: str
    content_type: str
    filename: str
    size: int
    grant_id: str
    is_inline: bool = False
    content_id: str | None = None


class AttachmentMetadataResponse(BaseModel):
    """Response model for getting a single attachment."""

    request_id: str
    data: AttachmentMetadata
