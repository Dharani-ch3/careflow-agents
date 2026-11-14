"""Model access.

Agents depend on one tiny interface, :class:`LLM`: give it a system prompt, a user
prompt and a Pydantic schema, get back a validated instance. That keeps agents
independent of any one provider, and makes them easy to test with a scripted fake.

``provider = "offline"`` returns ``None``. Every agent has a deterministic fallback
for that case, which is what CI and the demo use.
"""

from __future__ import annotations

from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from careflow.config import Settings

T = TypeVar("T", bound=BaseModel)


class LLM(Protocol):
    name: str

    async def structured(self, *, system: str, user: str, schema: type[T]) -> T: ...


class ChatModelLLM:
    """Adapter over any LangChain chat model that supports structured output."""

    def __init__(self, model: Any, name: str, method: str | None = None) -> None:
        self._model = model
        self.name = name
        self._method = method

    async def structured(self, *, system: str, user: str, schema: type[T]) -> T:
        kwargs = {"method": self._method} if self._method else {}
        runnable = self._model.with_structured_output(schema, **kwargs)
        result = await runnable.ainvoke([("system", system), ("human", user)])
        # Some providers hand back a dict; validate either way so bad output fails loudly.
        return schema.model_validate(result if isinstance(result, dict) else result.model_dump())


def make_llm(settings: Settings) -> LLM | None:
    provider, model = settings.llm_provider, settings.model_name
    if provider == "offline" or model is None:
        return None
    temperature = settings.llm_temperature
    chat: Any
    method: str | None = None
    try:
        if provider == "openai":
            from langchain_openai import ChatOpenAI

            chat = ChatOpenAI(model=model, temperature=temperature, timeout=30, max_retries=0)
        elif provider == "anthropic":
            from langchain_anthropic import ChatAnthropic

            opts: dict[str, Any] = {"model": model, "temperature": temperature}
            chat = ChatAnthropic(**opts, timeout=30, max_retries=0)
            # Native JSON-schema output; forced tool calls aren't supported on newer models.
            method = "json_schema"
        elif provider == "ollama":
            from langchain_ollama import ChatOllama

            chat = ChatOllama(
                model=model, temperature=temperature, base_url=settings.ollama_base_url
            )
        else:  # pragma: no cover - guarded by the Literal type
            raise ValueError(f"Unknown provider {provider}")
    except ImportError as exc:
        raise RuntimeError(
            f"The '{provider}' provider needs an extra: pip install 'careflow-agents[{provider}]'"
        ) from exc
    # Retries are handled once, by the graph's retry policy, not again inside the SDK.
    return ChatModelLLM(chat, name=f"{provider}:{model}", method=method)
