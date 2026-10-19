import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Label(BaseModel):
    """Mailbox label: a Gmail label or an Outlook category.

    IDs are opaque and mailbox-scoped. Outlook uses the category's unique name
    so message reads don't require an extra master-categories lookup.
    """

    id: str
    grant_id: str
    name: str
    color: str | None = None
    text_color: str | None = None
    system_label: bool = False


class CreateLabelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    color: str | None = None
    text_color: str | None = None

    @field_validator("name")
    @classmethod
    def require_visible_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Label name cannot be blank.")
        return value.strip()


class UpdateLabelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    color: str | None = None
    text_color: str | None = None

    @model_validator(mode="after")
    def require_update(self) -> "UpdateLabelRequest":
        if not self.model_fields_set:
            raise ValueError("Provide at least one label field to update.")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("Label name cannot be null.")
        if self.name is not None and not self.name.strip():
            raise ValueError("Label name cannot be blank.")
        if self.name is not None:
            self.name = self.name.strip()
        return self


class LabelResponse(BaseModel):
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    data: Label


class LabelListResponse(BaseModel):
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    data: list[Label]
