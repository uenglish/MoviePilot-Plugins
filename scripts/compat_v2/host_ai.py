"""MoviePilot V2 AI bridge; credentials remain owned by the host."""
import inspect

from app.agent.llm.helper import LLMHelper as HostLLMHelper
from app.agent.llm.provider import LLMProviderManager


def resolve_llm_provider_runtime():
    return LLMProviderManager()


class LLMHelper(HostLLMHelper):
    @staticmethod
    async def close_llm(llm):
        """Close per-request clients; V2 has no host close_llm helper."""
        closed = set()
        # Root clients own the OpenAI/Anthropic transports. Explicit httpx
        # clients cover custom endpoints and bindings that expose no root.
        for name in ('root_async_client', 'root_client', 'http_async_client', 'http_client', 'async_client', 'client'):
            client = getattr(llm, name, None)
            if client is None or id(client) in closed:
                continue
            closed.add(id(client))
            if getattr(client, 'is_closed', False) is True:
                continue
            closer = getattr(client, 'aclose', None) or getattr(client, 'close', None)
            if not callable(closer):
                continue
            try:
                result = closer()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                # Cleanup must not turn a successful answer into a retry or
                # expose provider messages that may contain authentication.
                continue
