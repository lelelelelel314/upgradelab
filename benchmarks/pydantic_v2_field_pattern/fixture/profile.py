from pydantic import BaseModel, Field


class Registration(BaseModel):
    username: str = Field(min_length=3, regex=r"^[a-z][a-z0-9_]+$")
