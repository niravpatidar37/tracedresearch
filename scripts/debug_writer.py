"""Debug: re-run the writer prompt for a stored run and print raw model output (uses disk cache)."""

import sys

from research_assistant.agents.writer import SYSTEM, _user_prompt
from research_assistant.config import Settings
from research_assistant.llm.base import LLMRequest
from research_assistant.llm.factory import make_llm
from research_assistant.llm.structured import strict_schema
from research_assistant.schemas import ReportDraft
from research_assistant.store import RunStore

settings = Settings.from_env()
store = RunStore.from_url(settings.redis_url)
st = store.load(sys.argv[1])
llm, models = make_llm(settings)
req = LLMRequest(
    model=models.writer,
    system=SYSTEM,
    user=_user_prompt(st),
    max_tokens=3000,
    json_schema=strict_schema(ReportDraft.model_json_schema()),
    schema_name="ReportDraft",
)
r = llm.complete(req)
print(r.cached, r.text[:3000])
