# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
import os
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, TypeVar, AsyncGenerator, Optional, Set

from ..graph import NodeExecuted
from ..tasks import Task, Corofunc
from ..sessions import Session, SessionPlugin, TaskExecute

log = logging.getLogger(__name__)

_T = TypeVar('_T')


class Parallel(SessionPlugin):
    name = 'parallel'

    def __init__(self, ncores: int = None) -> None:
        self._ncores = ncores or os.cpu_count() or 1
        self._available = self._ncores
        self._asyncio_tasks: Set[asyncio.Task[Any]] = set()
        self._pending: Optional[int] = None

    def post_enter(self, sess: Session) -> None:
        sess.storage['scheduler'] = self.run_coro

    async def pre_run(self) -> None:
        self._sem = asyncio.BoundedSemaphore(self._ncores)
        self._lock = asyncio.Lock()

    async def post_run(self) -> None:
        if not self._asyncio_tasks:
            return
        log.info(f'Cancelling {len(self._asyncio_tasks)} running tasks...')
        for task in self._asyncio_tasks:
            task.cancel()
        await asyncio.gather(*self._asyncio_tasks)
        assert not self._asyncio_tasks
        log.info('All tasks cancelled')

    def _release(self, ncores: int) -> None:
        if self._pending is None:
            for _ in range(ncores):
                self._sem.release()
            self._available += ncores
        else:
            self._pending += ncores

    def _stop(self) -> None:
        assert self._pending is None
        self._pending = 0
        log.info(f'Stopping scheduler')

    def ignored_exception(self) -> None:
        assert self._pending is not None
        log.info(f'Resuming scheduler with {self._pending} cores')
        pending = self._pending
        self._pending = None
        self._release(pending)

    def wrap_execute(self, execute: TaskExecute) -> TaskExecute:
        async def _execute(task: Task[Any], done: NodeExecuted[Task[Any]]) -> None:
            try:
                await execute(task, done)
            except Exception as e:
                if not isinstance(e, asyncio.CancelledError):
                    done((task, e, ()))
            asyncio_task = asyncio.current_task()
            assert asyncio_task
            self._asyncio_tasks.remove(asyncio_task)

        async def spawn_execute(*args: Any) -> None:
            asyncio_task = asyncio.create_task(_execute(*args))
            self._asyncio_tasks.add(asyncio_task)

        return spawn_execute

    @asynccontextmanager
    async def _acquire(self, ncores: int) -> AsyncGenerator[None, None]:
        async with self._lock:
            for _ in range(ncores):
                await self._sem.acquire()
                self._available -= 1
        try:
            yield
        except Exception as e:
            self._stop()
            raise
        finally:
            self._release(ncores)

    async def run_coro(self, corofunc: Corofunc[_T], *args: Any, **kwargs: Any
                       ) -> _T:
        task = self._app.running_task
        n = task.storage.get('ncores', 1)
        if n > self._available:
            log.debug(
                f'Waiting for {n-self._available}/{n} '
                f'unavailable cores for "{task}"'
            )
            waited = True
        else:
            waited = False
        async with self._acquire(n):
            if waited:
                log.debug(f'All {n} cores available for "{task}", resuming')
            return await corofunc(*args, **kwargs)