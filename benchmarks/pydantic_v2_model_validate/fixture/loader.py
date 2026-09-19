from pydantic import BaseModel


class Job(BaseModel):
    name: str
    retries: int


def load_job(payload: dict[str, object]) -> Job:
    return Job.parse_obj(payload)
