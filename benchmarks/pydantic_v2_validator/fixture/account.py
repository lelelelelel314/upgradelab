from pydantic import BaseModel, validator


class Account(BaseModel):
    email: str

    @validator("email")
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()
