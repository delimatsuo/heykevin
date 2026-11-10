"""Ownership and bounded shutdown of a voice pipeline's asyncio tasks."""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from app.utils.logging import get_logger

logger = get_logger(__name__)


class CallTasks:
    """Owned task-to-task awaits use shield plus explicit cancel(), not propagation."""

    def __init__(self, *, drain_timeout: float = 2.0, resource_timeout: float = 2.0):
        self.stopping = False
        self.drain_timeout = drain_timeout
        self.resource_timeout = resource_timeout
        self._tasks: set[asyncio.Future] = set()
        self._resources: set[asyncio.Task] = set()
        self._shutdown: asyncio.Task | None = None

    def create(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Future:
        if self.stopping:
            coroutine.close()
            rejected = asyncio.get_running_loop().create_future()
            rejected.cancel()
            return rejected
        task = asyncio.create_task(coroutine)
        self.adopt(task)
        return task

    def adopt(self, *tasks) -> None:
        """Also cover legacy task attributes and externally supplied test tasks."""
        for task in tasks:
            if isinstance(task, asyncio.Future) and task not in self._tasks:
                self._tasks.add(task)
                task.add_done_callback(self._done)

    def _done(self, task: asyncio.Future) -> None:
        self._tasks.discard(task)
        self._resources.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error:
                logger.warning("Call task failed: %s", type(error).__name__)

    @staticmethod
    def cancel(task: asyncio.Future) -> None:
        """Request cancellation once; preserve an already-running finally block."""
        if task is not asyncio.current_task() and not task.done():
            if not isinstance(task, asyncio.Task) or not task.cancelling():
                task.cancel()

    async def close(self, resources: tuple[Callable[[], Coroutine], ...] = ()) -> None:
        """External callers join shutdown; owned callers request it and unwind.

        One shielded cleanup prevents concurrent stop callers from cancelling
        each other. A worker must not await the cleanup that joins that worker.
        Timed-out tasks remain retained until done; this cannot undo provider
        or executor work already started.
        """
        self.stopping = True
        owned_caller = asyncio.current_task() in self._tasks | self._resources
        if self._shutdown is None:
            self._shutdown = asyncio.create_task(self._finish(resources))
            # The shutdown itself is retained separately, never joined by itself.
            self._shutdown.add_done_callback(self._observe_shutdown)
        if not owned_caller:
            await asyncio.shield(self._shutdown)

    @staticmethod
    def _observe_shutdown(task: asyncio.Task) -> None:
        if not task.cancelled():
            error = task.exception()
            if error:
                logger.warning("Call shutdown failed: %s", type(error).__name__)

    async def _finish(self, resources) -> None:
        pending = {task for task in self._tasks if not task.done()}
        for task in pending:
            # A second cancel can interrupt the finally block we need to drain.
            self.cancel(task)
        if pending:
            _, pending = await asyncio.wait(pending, timeout=self.drain_timeout)
            if pending:
                logger.warning("Call shutdown drain timed out: pending=%d", len(pending))
        for close in resources:
            await self.close_resource(close)
        # Include cleanup started by a connection finishing during this drain.
        pending_resources = {task for task in self._resources if not task.done()}
        if pending_resources:
            _, pending_resources = await asyncio.wait(
                pending_resources, timeout=self.resource_timeout
            )
            if pending_resources:
                logger.warning(
                    "Call resource cleanup still pending: count=%d", len(pending_resources)
                )

    async def close_resource(self, close: Callable[[], Coroutine]) -> None:
        """Also dispose connections completing after shutdown has already begun."""
        task = asyncio.create_task(self._finish_resource(close))
        self._resources.add(task)
        task.add_done_callback(self._done)
        # Resource cleanup must survive cancellation of its connecting worker.
        await asyncio.shield(task)

    async def _finish_resource(self, close) -> None:
        task = asyncio.create_task(close())
        self._resources.add(task)
        task.add_done_callback(self._done)
        try:
            _, pending = await asyncio.wait({task}, timeout=self.resource_timeout)
            if pending:
                logger.warning("Call resource close timed out")
        finally:
            if not task.done():
                task.cancel()
                # Let cooperative cancellation settle without an unbounded join.
                await asyncio.wait({task}, timeout=0)


def pipeline_tasks(pipeline) -> CallTasks:
    """Lazily initialize for compatibility with existing borrowed tool contexts."""
    scope = getattr(pipeline, "_call_tasks", None)
    if not isinstance(scope, CallTasks):
        scope = pipeline._call_tasks = CallTasks()
    return scope
