import tomllib
from pathlib import Path
from string import Formatter

from pydantic import BaseModel, Field, model_validator

from app.config import settings


class Rules(BaseModel):
    followup_days: list[int] = Field(default=[3, 7], min_length=2, max_length=2)
    scan_days: int = Field(default=30, ge=1, le=90)
    max_ai_calls_per_run: int = Field(default=10, ge=0, le=100)
    signature: str = ""
    exclude_phrases: list[str] = []
    template1: str = "{greeting}\n\nI wanted to follow up on my previous email regarding the opportunity. Have you had a chance to review my profile/CV?\n\nThanks,\n{signature}"
    template2: str = "{greeting}\n\nJust following up once more regarding my earlier email. Please let me know if there may be a relevant opportunity or if any additional information would be helpful.\n\nThanks,\n{signature}"

    @model_validator(mode="after")
    def valid(self):
        if not 1 <= self.followup_days[0] < self.followup_days[1] <= 90:
            raise ValueError("Use two increasing follow-up days between 1 and 90")
        for template in (self.template1, self.template2):
            if len(template) > 5000:
                raise ValueError("Template too long")
            for _, field, spec, conversion in Formatter().parse(template):
                if field is not None and (
                    field not in {"greeting", "signature"} or spec or conversion
                ):
                    raise ValueError("Only {greeting} and {signature} placeholders are supported")
        return self


def load_rules():
    path = Path(settings().rules_file)
    return Rules(**tomllib.loads(path.read_text())) if path.exists() else Rules()
