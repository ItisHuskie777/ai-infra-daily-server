import json
from abc import ABC
from abc import abstractmethod
from typing import override

import openai
import pydantic

from app import logging
from app import settings
from app.result import OnSuccess
from app.result import ServiceError

logger = logging.get_logger(__name__)

_JSON_ATTEMPTS = 2


class LlmError(ServiceError):
    NETWORK = "network"
    RATE_LIMITED = "rate_limited"
    INVALID_RESPONSE = "invalid_response"

    @override
    def service(self) -> str:
        return "llm"


class ImplementsLlm(ABC):
    @abstractmethod
    async def complete_json[M: pydantic.BaseModel](
        self,
        *,
        system: str,
        user: str,
        schema: type[M],
        temperature: float = 0.2,
    ) -> OnSuccess[M]:
        """Validates the reply against `schema`; a malformed reply is retried once."""

    @abstractmethod
    async def complete_text(
        self, *, system: str, user: str, temperature: float = 0.4
    ) -> OnSuccess[str]: ...


class OpenAiCompatibleLlm(ImplementsLlm):
    __slots__ = ("_client", "_model")

    def __init__(self, client: openai.AsyncOpenAI, *, model: str) -> None:
        self._client = client
        self._model = model

    @override
    async def complete_json[M: pydantic.BaseModel](
        self,
        *,
        system: str,
        user: str,
        schema: type[M],
        temperature: float = 0.2,
    ) -> OnSuccess[M]:
        schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        instructions = (
            f"{system}\n\nReply with a single JSON object that validates against this "
            f"JSON schema and nothing else:\n{schema_text}"
        )

        for attempt in range(_JSON_ATTEMPTS):
            raw = await self._complete(instructions, user, temperature, json_mode=True)

            if isinstance(raw, LlmError):
                return raw

            try:
                return schema.model_validate_json(raw)
            except pydantic.ValidationError:
                logger.warning(
                    "LLM reply did not match the schema.",
                    extra={"attempt": attempt + 1, "schema": schema.__name__},
                )

        return LlmError.INVALID_RESPONSE

    @override
    async def complete_text(
        self, *, system: str, user: str, temperature: float = 0.4
    ) -> OnSuccess[str]:
        return await self._complete(system, user, temperature, json_mode=False)

    async def _complete(
        self, system: str, user: str, temperature: float, *, json_mode: bool
    ) -> OnSuccess[str]:
        try:
            completion = await self._client.chat.completions.create(
                model=self._model,
                temperature=temperature,
                response_format={"type": "json_object"}
                if json_mode
                else {"type": "text"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except openai.RateLimitError:
            logger.warning("LLM rate limited.")

            return LlmError.RATE_LIMITED
        except openai.APIError:
            logger.exception("LLM request failed.")

            return LlmError.NETWORK

        content = completion.choices[0].message.content if completion.choices else None

        if content is None or content == "":
            return LlmError.INVALID_RESPONSE

        return content


def default() -> ImplementsLlm:
    client = openai.AsyncOpenAI(
        api_key=settings.DEEPSEEK_API_KEY,
        base_url=settings.DEEPSEEK_BASE_URL,
        max_retries=2,
        timeout=120.0,
    )

    return OpenAiCompatibleLlm(client, model=settings.LLM_MODEL)
