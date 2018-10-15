# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
from pathlib import Path
import sqlite3
from contextlib import contextmanager
import tempfile
import shutil
import subprocess

from .cellar import Cellar, State
from .Logging import error, debug
from .Utils import get_timestamp, sample
from .Announcer import Announcer
from .db import WithDB

from typing import Tuple, Iterable, List, Iterator, Set, Dict, Optional
from .cellar import Hash, TPath


class Scheduler(WithDB):
    def __init__(self, cellar: Cellar, tmpdir: str = None) -> None:
        self.init_db(str(cellar.cafdir/'queue.db'))
        self.execute(
            'create table if not exists queue ('
            'taskhash text primary key, state integer, label text, path text, '
            'changed text, active integer'
            ') without rowid'
        )
        self.cellar = cellar
        self.tmpdir = tmpdir
        self._tmpdirs: Dict[Hash, Path] = {}
        self._labels: Dict[Hash, str] = {}
        cellar.register_hook('postsave')(self.submit)
        cellar.register_hook('tmpdir')(self._get_tmpdir)

    @contextmanager
    def _get_tmpdir(self, hashid: Hash) -> Iterator[Path]:
        label = self._labels[hashid]
        tmpdir = Path(tempfile.mkdtemp(prefix='caftsk_', dir=self.tmpdir))
        self._tmpdirs[hashid] = tmpdir
        self.execute(
            'update queue set path = ? where taskhash = ?',
            (str(tmpdir), hashid)
        )
        debug(f'Executing {label} in {tmpdir}')
        yield tmpdir

    def submit(self, tasks: List[Tuple[Hash, State, TPath]]) -> None:
        self.execute('drop table if exists current_tasks')
        self.execute('create temporary table current_tasks(hash text)')
        self.executemany('insert into current_tasks values (?)', (
            (hashid,) for hashid, *_ in tasks
        ))
        self.execute(
            'update queue set active = 0 where taskhash not in current_tasks'
        )
        self.execute(
            'delete from queue where active = 0 and state in (?, ?)',
            (State.CLEAN, State.DONE)
        )
        self.executemany(
            'insert or ignore into queue values (?,?,?,?,?,1)', (
                (hashid, state, label, '', get_timestamp())
                for hashid, state, label in tasks
            )
        )
        self.executemany(
            'update queue set active = 1, label = ? where taskhash = ?', (
                (label, hashid) for hashid, state, label in tasks
            )
        )
        self.executemany(
            'update queue set state = ? where taskhash = ?', (
                (state, hashid) for hashid, state, _ in tasks
                if state == State.DONE
            )
        )
        self.commit()

    @contextmanager
    def db_lock(self) -> Iterator[None]:
        self.execute('begin immediate transaction')
        try:
            yield
        finally:
            self.execute('end transaction')

    def candidate_tasks(self, states: Iterable[Hash], randomize: bool = False) \
            -> Iterator[Hash]:
        if randomize:
            yield from sample(states)
        else:
            yield from states

    def is_state_ok(self, state: State, hashid: Hash, label: str) -> bool:
        return state == State.CLEAN

    def skip_task(self, hashid: Hash) -> None:
        pass

    async def make(self, patterns: Optional[List[str]], limit: int = None,
                   nmaxerror: int = 5, dry: bool = False, randomize: bool = False
                   ) -> None:
        assert self.cellar._app
        if patterns:
            hashes: Optional[Set[Hash]] = set(self.cellar.glob(*patterns))
            if not hashes:
                return
        else:
            hashes = None
        self.commit()
        self._db.isolation_level = None
        nrun = 0
        nerror = 0
        print(f'{get_timestamp()}: Started work')
        while True:
            if nerror >= nmaxerror:
                print(f'{nerror} errors in row, quitting')
                break
            if limit and nrun >= limit:
                print(f'{nrun} tasks ran, quitting')
                break
            queue = self.get_queue()
            states = {hashid: state for hashid, (state, *_) in queue.items()}
            self._labels = {hashid: label for hashid, (_, label, *__) in queue.items()}
            skipped: Set[Hash] = set()
            will_continue = False
            was_interrupted = False
            debug(f'Starting candidate loop')
            for hashid in self.candidate_tasks(states, randomize=randomize):
                label = self._labels[hashid]
                debug(f'Got {hashid}:{label} as candidate')
                if hashid in skipped:
                    self.skip_task(hashid)
                    debug(f'{label} has been skipped before')
                    break
                else:
                    skipped.add(hashid)
                if hashes is not None and hashid not in hashes:
                    self.skip_task(hashid)
                    debug(f'{label} is in filter, skipping')
                    continue
                state = states[hashid]
                if not self.is_state_ok(state, hashid, label):
                    debug(f'{label} does not have conforming state, skipping')
                    continue
                task = self.cellar.get_task(hashid)
                assert task
                if any(
                        states[child] != State.DONE
                        for child in task.children
                ):
                    self.skip_task(hashid)
                    debug(f'{label} has unsealed children, skipping')
                    continue
                if dry:
                    self.skip_task(hashid)
                    continue
                with self.db_lock():
                    state, = self.execute(
                        'select state as "[state]" from queue where taskhash = ? and active = 1',
                        (hashid,)
                    ).fetchone()
                    if state != State.CLEAN:
                        print(f'{label} already locked!')
                        will_continue = True
                        break
                    self.execute(
                        'update queue set state = ?, changed = ? '
                        'where taskhash = ?',
                        (State.RUNNING, get_timestamp(), hashid)
                    )
                if not task.command:
                    self.cellar.seal_task(hashid, {})
                    self.task_done(hashid)
                    print(f'{get_timestamp()}: {label} finished successfully')
                    continue
                try:
                    out = await self.cellar._app._executors[task.execid](task.data)
                except KeyboardInterrupt:
                    was_interrupted = True
                    self.task_interrupt(hashid)
                    print(f'{get_timestamp()}: {label} was interrupted')
                    break
                except subprocess.CalledProcessError as e:
                    print(e)
                    nerror += 1
                    self.task_error(hashid)
                    print(f'{get_timestamp()}: {label} finished with error')
                else:
                    self.cellar.update_outputs_v2(hashid, State.DONE, out)
                    shutil.rmtree(self._tmpdirs.pop(hashid))
                    nerror = 0
                    self.task_done(hashid)
                    print(f'{get_timestamp()}: {label} finished successfully')
                skipped = set()
                nrun += 1
                will_continue = True
            if not will_continue:
                print(f'No available tasks to do, quitting')
                break
            if was_interrupted:
                print(f'{get_timestamp()}: Interrupted, quitting')
                break
        print(f'Executed {nrun} tasks')
        self._db.isolation_level = ''

    def get_states(self) -> Dict[Hash, State]:
        try:
            return dict(self.execute(
                'select taskhash, state as "[state]" from queue where active = 1'
            ))
        except sqlite3.OperationalError:
            error('There is no queue.')

    def get_queue(self) -> Dict[Hash, Tuple[State, TPath, str, str]]:
        try:
            return {
                hashid: row for hashid, *row
                in self.execute(
                    'select taskhash, state as "[state]", label, path, changed from queue '
                    'where active = 1'
                )
            }
        except sqlite3.OperationalError:
            error('There is no queue.')

    def reset_task(self, hashid: Hash) -> None:
        path, = self.execute(
            'select path from queue where taskhash = ?', (hashid,)
        ).fetchone()
        if path:
            try:
                shutil.rmtree(path)
            except FileNotFoundError:
                pass
        self.execute(
            'update queue set state = ?, changed = ?, path = "" '
            'where taskhash = ?',
            (State.CLEAN, get_timestamp(), hashid)
        )
        self.commit()

    def gc(self) -> None:
        cur = self.execute(
            'select path, taskhash from queue where state in (?,?,?)',
            (State.ERROR, State.INTERRUPTED, State.RUNNING)
        )
        for path, hashid in cur:
            try:
                shutil.rmtree(path)
            except FileNotFoundError:
                pass
        self.execute(
            'update queue set state = ?, changed = ?, path = "" '
            'where state in (?,?,?)', (
                State.CLEAN, get_timestamp(),
                State.ERROR, State.INTERRUPTED, State.RUNNING
            )
        )
        self.commit()

    def gc_all(self) -> None:
        self.execute('delete from queue where active = 0')
        self.commit()

    def task_error(self, hashid: Hash) -> None:
        self.execute(
            'update queue set state = ?, changed = ? where taskhash = ?',
            (State.ERROR, get_timestamp(), hashid)
        )
        self.commit()

    def task_done(self, hashid: Hash, remote: str = None) -> None:
        self.execute(
            'update queue set state = ?, changed = ?, path = ? '
            'where taskhash = ?', (
                State.DONE if not remote else State.DONEREMOTE,
                get_timestamp(),
                '' if not remote else f'REMOTE:{remote}',
                hashid
            )
        )
        self.commit()

    def task_interrupt(self, hashid: Hash) -> None:
        self.execute(
            'update queue set state = ?, changed = ? '
            'where taskhash = ?',
            (State.INTERRUPTED, get_timestamp(), hashid)
        )
        self.commit()


class RemoteScheduler(Scheduler):
    def __init__(self, cellar: Cellar, url: str, tmpdir: str = None, curl: str = None) -> None:
        super().__init__(cellar, tmpdir)
        self.announcer = Announcer(url, curl)

    def candidate_tasks(self, states: Iterable[Hash], randomize: bool = False) \
            -> Iterator[Hash]:
        while True:
            hashid = self.announcer.get_task()
            if hashid:
                yield hashid
            else:
                return

    def is_state_ok(self, state: State, hashid: Hash, label: str) -> bool:
        if state in (State.DONE, State.DONEREMOTE):
            print(f'Task {label} already done')
            self.task_done(hashid)
            return False
        if state in (State.ERROR, State.RUNNING, State.INTERRUPTED):
            self.reset_task(hashid)
            return True
        if state == State.CLEAN:
            return True
        assert False

    def skip_task(self, hashid: Hash) -> None:
        self.announcer.put_back(hashid)

    def task_error(self, hashid: Hash) -> None:
        super().task_error(hashid)
        self.announcer.task_error(hashid)

    def task_done(self, hashid: Hash, remote: str = None) -> None:
        super().task_done(hashid)
        self.announcer.task_done(hashid)

    def task_interrupt(self, hashid: Hash) -> None:
        super().task_error(hashid)
        self.announcer.put_back(hashid)