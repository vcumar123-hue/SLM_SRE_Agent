"""
The only way a planner may call a model (design 7.1 and review finding "model budgets are not enforced").

Every request goes through ModelGateway.request, which enforces, before and after the call:
  * the run's model-call and token budgets (a request that would exceed them is refused before it is made);
  * `model_call_timeout_s`, bounded also by the run's remaining deadline, on the shared bounded **model** executor (a pool of its own,
    never the one a planner runs on); the effective timeout and a cancel token are passed to the model adapter so a stuck call can stop;
  * the per-call output limit (`max_tokens_planner` or `max_tokens_synthesis`), passed to the model as its max_tokens and checked
    against the tokens it reports: an over-limit reply is discarded, never returned to the planner;
  * the run-wide token budget: usage is counted before the reply is handed back.
Planners that talk to a model without this gateway are still bounded by the runner's outer timeout and its own checks on the
reply, but a model planner must use it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, Optional

from agentruntime.contracts import Budgets
from agentruntime.executor import CallTimeout, CancelToken, ExecutorBusy, ExecutorFailed, MODEL_EXECUTOR, BoundedExecutor


@dataclass
class ModelResponse:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    model: Optional[str] = None


@dataclass
class ModelUsage:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out


class ModelBudgetError(Exception):
    """A run-wide model budget (calls, tokens or deadline) is used up: the run ends `budget_exhausted`."""


class ModelTimeoutError(Exception):
    pass


class ModelOverLimit(Exception):
    """A single reply exceeded its per-call output limit: it is discarded and the run ends `failed_model`."""


class ModelUnavailable(Exception):
    pass


class ModelGateway:
    def __init__(self, budgets: Budgets, usage: ModelUsage, remaining_s: Callable[[], float],
                 executor: BoundedExecutor = MODEL_EXECUTOR) -> None:
        self._budgets = budgets
        self.usage = usage
        self._remaining_s = remaining_s
        self._executor = executor

    def request(self, fn: Callable[[int, float, CancelToken], ModelResponse],
                kind: Literal["planner", "synthesis"] = "planner") -> ModelResponse:
        """Make one model request. `fn(max_tokens, timeout_s, cancel)` is the model adapter: it must apply `timeout_s` at the HTTP-client
        level, stop when `cancel` is set where it can, and return a ModelResponse. The gateway still bounds the wait itself."""
        b = self._budgets
        if self.usage.calls >= b.max_model_calls:
            raise ModelBudgetError("model call budget used")
        left_tokens = b.max_tokens_run - self.usage.tokens
        if left_tokens <= 0:
            raise ModelBudgetError("token budget used")
        limit = b.max_tokens_synthesis if kind == "synthesis" else b.max_tokens_planner
        max_tokens = int(min(limit, left_tokens))
        timeout = min(b.model_call_timeout_s, self._remaining_s())
        if timeout <= 0:
            raise ModelBudgetError("deadline reached")
        self.usage.calls += 1                      # a request that times out still used a call
        try:
            response = self._executor.run(lambda token: fn(max_tokens, timeout, token), timeout)
        except CallTimeout as exc:
            raise ModelTimeoutError(str(exc)) from exc
        except (ExecutorBusy, ExecutorFailed) as exc:
            self.usage.calls -= 1                  # refused before it started: not a used call
            raise ModelUnavailable(str(exc)) from exc
        if not isinstance(response, ModelResponse):
            raise ModelUnavailable("the model call did not return a ModelResponse")
        self.usage.tokens_in += max(0, int(response.tokens_in))
        self.usage.tokens_out += max(0, int(response.tokens_out))
        if response.tokens_out > limit:
            raise ModelOverLimit(f"reply of {response.tokens_out} tokens exceeds the {kind} limit of {limit}")
        if self.usage.tokens > b.max_tokens_run:
            raise ModelBudgetError("token budget exceeded by this reply")
        return response
