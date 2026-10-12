"""
agents/a2a_protocol.py — Item #15
Internal Agent-to-Agent (A2A) protocol.

Typed envelope (AgentMessage) + AgentDispatcher with retry/timeout/error contracts.
Designed to be the communication backbone for future MCP integration (item #21).

Usage:
    dispatcher = AgentDispatcher()
    dispatcher.register("playbook", playbook_agent_fn)
    result = await dispatcher.send(AgentMessage(
        sender="triage_agent",
        recipient="playbook",
        intent="generate_playbook",
        payload={"classification": "sap_login_failure", "ticket": "..."},
    ))
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


# ── Envelope types ────────────────────────────────────────────────────────────

class MessageStatus(str, Enum):
    PENDING   = "pending"
    DELIVERED = "delivered"
    FAILED    = "failed"
    TIMEOUT   = "timeout"
    REJECTED  = "rejected"


@dataclass
class AgentMessage:
    """Typed envelope for inter-agent communication."""
    sender: str
    recipient: str
    intent: str
    payload: Dict[str, Any] = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    correlation_id: Optional[str] = None   # links reply to the original request
    trace_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResponse:
    """Standardised response envelope returned by AgentDispatcher.send()."""
    status: MessageStatus
    result: Any = None
    error: Optional[str] = None
    duration_ms: float = 0.0
    message_id: str = ""
    correlation_id: Optional[str] = None


# ── Handler type ──────────────────────────────────────────────────────────────

# A handler is any sync or async callable: (AgentMessage) → Any
AgentHandler = Callable[[AgentMessage], Any]


# ── Dispatcher ───────────────────────────────────────────────────────────────

class AgentDispatcher:
    """
    Registry + message bus for internal agents.

    - Handlers can be sync or async.
    - send() always returns AgentResponse (never raises on timeout / handler error).
    - Retries on transient errors (exception), not on logic failures (status REJECTED).
    """

    _DEFAULT_TIMEOUT = 30.0
    _DEFAULT_RETRIES = 2

    def __init__(self) -> None:
        self._registry: Dict[str, AgentHandler] = {}

    def register(self, agent_name: str, handler: AgentHandler) -> None:
        if agent_name in self._registry:
            logger.warning("A2A: overwriting handler for '%s'", agent_name)
        self._registry[agent_name] = handler
        logger.info("A2A: registered agent '%s'", agent_name)

    def unregister(self, agent_name: str) -> None:
        self._registry.pop(agent_name, None)

    def registered_agents(self) -> List[str]:
        return list(self._registry.keys())

    async def send(
        self,
        message: AgentMessage,
        timeout: float = _DEFAULT_TIMEOUT,
        retries: int = _DEFAULT_RETRIES,
    ) -> AgentResponse:
        """
        Deliver message to the target agent with retry/timeout.
        Returns AgentResponse regardless of outcome — never raises.
        """
        try:
            from trace_context import ensure_trace_id
            message.trace_id = message.trace_id or ensure_trace_id()
        except Exception:
            pass

        handler = self._registry.get(message.recipient)
        if handler is None:
            logger.warning("A2A: no handler for recipient '%s'", message.recipient)
            return AgentResponse(
                status=MessageStatus.REJECTED,
                error=f"No agent registered for '{message.recipient}'",
                message_id=message.message_id,
                correlation_id=message.correlation_id,
            )

        last_error: Optional[str] = None
        for attempt in range(1, retries + 2):
            t0 = time.monotonic()
            try:
                if asyncio.iscoroutinefunction(handler):
                    coro: Coroutine[Any, Any, Any] = handler(message)
                    result = await asyncio.wait_for(coro, timeout=timeout)
                else:
                    result = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(None, handler, message),
                        timeout=timeout,
                    )
                duration = (time.monotonic() - t0) * 1000
                logger.debug(
                    "A2A: %s→%s intent=%s OK in %.0fms (attempt %d)",
                    message.sender, message.recipient, message.intent, duration, attempt,
                )
                return AgentResponse(
                    status=MessageStatus.DELIVERED,
                    result=result,
                    duration_ms=round(duration, 1),
                    message_id=message.message_id,
                    correlation_id=message.correlation_id,
                )
            except asyncio.TimeoutError:
                duration = (time.monotonic() - t0) * 1000
                last_error = f"Timeout after {timeout}s (attempt {attempt})"
                logger.warning("A2A: timeout %s→%s attempt %d", message.sender, message.recipient, attempt)
                if attempt > retries:
                    return AgentResponse(
                        status=MessageStatus.TIMEOUT,
                        error=last_error,
                        duration_ms=round(duration, 1),
                        message_id=message.message_id,
                        correlation_id=message.correlation_id,
                    )
            except Exception as exc:
                duration = (time.monotonic() - t0) * 1000
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("A2A: error %s→%s attempt %d: %s", message.sender, message.recipient, attempt, exc)
                if attempt > retries:
                    return AgentResponse(
                        status=MessageStatus.FAILED,
                        error=last_error,
                        duration_ms=round(duration, 1),
                        message_id=message.message_id,
                        correlation_id=message.correlation_id,
                    )
                await asyncio.sleep(0.5 * attempt)

        return AgentResponse(
            status=MessageStatus.FAILED,
            error=last_error or "Unknown error",
            message_id=message.message_id,
            correlation_id=message.correlation_id,
        )

    def send_sync(
        self,
        message: AgentMessage,
        timeout: float = _DEFAULT_TIMEOUT,
        retries: int = _DEFAULT_RETRIES,
    ) -> AgentResponse:
        """
        Synchronous wrapper for send() — useful from non-async contexts (Flask routes, etc.).
        Creates a fresh event loop to avoid conflicts with the caller's loop.
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    fut = ex.submit(asyncio.run, self.send(message, timeout=timeout, retries=retries))
                    return fut.result(timeout=timeout + 5)
        except RuntimeError:
            pass
        return asyncio.run(self.send(message, timeout=timeout, retries=retries))


# ── Trace span ───────────────────────────────────────────────────────────────

@dataclass
class TraceSpan:
    """One agent's execution record within an orchestrated request."""
    agent: str
    intent: str
    start_ms: float = field(default_factory=lambda: time.time() * 1000)
    end_ms: float = 0.0
    duration_ms: float = 0.0
    input_summary: str = ""
    output_summary: str = ""
    decision: str = ""
    status: str = "ok"   # ok | skipped | error

    def finish(self, output_summary: str = "", decision: str = "", status: str = "ok") -> "TraceSpan":
        self.end_ms = time.time() * 1000
        self.duration_ms = round(self.end_ms - self.start_ms, 1)
        self.output_summary = output_summary
        self.decision = decision
        self.status = status
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent":          self.agent,
            "intent":         self.intent,
            "duration_ms":    self.duration_ms,
            "input_summary":  self.input_summary,
            "output_summary": self.output_summary,
            "decision":       self.decision,
            "status":         self.status,
        }


# ── Global dispatcher singleton ───────────────────────────────────────────────

_dispatcher: Optional[AgentDispatcher] = None


def get_dispatcher() -> AgentDispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = AgentDispatcher()
    return _dispatcher


def register_agent(name: str, handler: AgentHandler) -> None:
    get_dispatcher().register(name, handler)


async def send_message(
    sender: str,
    recipient: str,
    intent: str,
    payload: Optional[Dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    timeout: float = AgentDispatcher._DEFAULT_TIMEOUT,
    retries: int = AgentDispatcher._DEFAULT_RETRIES,
) -> AgentResponse:
    """Convenience function — no need to construct AgentMessage manually."""
    msg = AgentMessage(
        sender=sender,
        recipient=recipient,
        intent=intent,
        payload=payload or {},
        correlation_id=correlation_id,
    )
    return await get_dispatcher().send(msg, timeout=timeout, retries=retries)


async def broadcast(
    sender: str,
    recipients: List[str],
    intent: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = AgentDispatcher._DEFAULT_TIMEOUT,
) -> Dict[str, AgentResponse]:
    """Fan-out a message to multiple agents in parallel.
    Returns a dict of {recipient: AgentResponse}.
    Used for future multi-agent scenarios (e.g. simultaneous RCA + evidence gathering).
    """
    import asyncio as _asyncio
    tasks = {
        r: _asyncio.create_task(
            send_message(sender=sender, recipient=r, intent=intent, payload=payload, timeout=timeout)
        )
        for r in recipients
    }
    results: Dict[str, AgentResponse] = {}
    for recipient, task in tasks.items():
        try:
            results[recipient] = await task
        except Exception as exc:
            results[recipient] = AgentResponse(
                status=MessageStatus.FAILED,
                error=str(exc),
                message_id="",
            )
    return results
