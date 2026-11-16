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
_MAX_PROBLEMS = 5


def _problems(error: pydantic.ValidationError) -> str:
    return "\n".join(
        f"- {'.'.join(str(part) for part in issue['loc']) or 'reply'}: {issue['msg']}"
        for issue in error.errors(include_url=False, include_input=False)[
            :_MAX_PROBLEMS
        ]
    )


class LlmError(ServiceError):
    NETWORK = "network"
    RATE_LIMITED = "rate_limited"
    UNAUTHORISED = "unauthorised"
    INVALID_RESPONSE = "invalid_response"
    NOT_CONFIGURED = "not_configured"

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
        """Validates the reply against `schema`; a malformed reply is retried once, with
        the validation errors added to the request."""


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

        request = user

        for attempt in range(_JSON_ATTEMPTS):
            raw = await self._complete(instructions, request, temperature)

            if isinstance(raw, LlmError):
                return raw

            try:
                return schema.model_validate_json(raw)
            except pydantic.ValidationError as error:
                problems = _problems(error)
                logger.warning(
                    "LLM reply did not match the schema.",
                    extra={
                        "attempt": attempt + 1,
                        "schema": schema.__name__,
                        "problems": problems,
                    },
                )
                # NOTE: Most callers ask at temperature 0, so the same request would
                # get the same wrong reply; the retry names what to fix.
                request = (
                    f"{user}\n\nYour previous reply was rejected for these reasons; "
                    f"reply again with them fixed:\n{problems}"
                )

        return LlmError.INVALID_RESPONSE

    async def _complete(
        self, system: str, user: str, temperature: float
    ) -> OnSuccess[str]:
        try:
            completion = await self._client.chat.completions.create(
                model=self._model,
                temperature=temperature,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except openai.RateLimitError:
            logger.warning("LLM rate limited.")

            return LlmError.RATE_LIMITED
        except openai.AuthenticationError:
            logger.error("LLM rejected the API key.")

            return LlmError.UNAUTHORISED
        except openai.APIError:
            logger.exception("LLM request failed.")

            return LlmError.NETWORK

        choice = completion.choices[0] if completion.choices else None
        usage = completion.usage
        logger.debug(
            "LLM reply received.",
            extra={
                "finish_reason": None if choice is None else choice.finish_reason,
                "prompt_tokens": None if usage is None else usage.prompt_tokens,
                "completion_tokens": None if usage is None else usage.completion_tokens,
            },
        )

        if choice is not None and choice.finish_reason == "length":
            logger.warning("LLM reply was cut off at the token limit.")

        content = None if choice is None else choice.message.content

        if content is None or content == "":
            return LlmError.INVALID_RESPONSE

        return content


class UnconfiguredLlm(ImplementsLlm):
    """Stands in when no API key is set, so commands that never call the model (and the
    heuristic fallbacks of those that do) still work."""

    __slots__ = ()

    @override
    async def complete_json[M: pydantic.BaseModel](
        self,
        *,
        system: str,
        user: str,
        schema: type[M],
        temperature: float = 0.2,
    ) -> OnSuccess[M]:
        return LlmError.NOT_CONFIGURED


def default() -> ImplementsLlm:
    if settings.DEEPSEEK_API_KEY == "":
        logger.warning("No LLM API key configured; model calls will fail.")

        return UnconfiguredLlm()

    client = openai.AsyncOpenAI(
        api_key=settings.DEEPSEEK_API_KEY,
        base_url=settings.DEEPSEEK_BASE_URL,
        max_retries=2,
        timeout=120.0,
    )

    return OpenAiCompatibleLlm(client, model=settings.LLM_MODEL)
