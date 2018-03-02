# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
import re
import os
from contextlib import contextmanager
from datetime import datetime
import itertools
import hashlib
import stat
import random
from configparser import ConfigParser
from pathlib import Path

from typing import (
    Any, Iterator, Tuple, Iterable, TypeVar, List, Callable, Mapping, Union,
    NewType
)
from typing_extensions import Protocol

_T = TypeVar('_T')
_V = TypeVar('_V')
_K_contra = TypeVar('_K_contra', contravariant=True)
_V_co = TypeVar('_V_co', covariant=True)

Hash = NewType('Hash', str)


def get_hash(text: Union[str, bytes]) -> Hash:
    if isinstance(text, str):
        text = text.encode()
    return Hash(hashlib.sha1(text).hexdigest())


class Map(Protocol[_K_contra, _V_co]):
    def __getitem__(self, key: _K_contra) -> _V_co: ...


def config_group(config: ConfigParser, group: str) \
        -> Iterator[Tuple[str, Mapping[str, Any]]]:
    for name, section in config.items():
        m = re.match(r'(?P<group>\w+) *"(?P<member>\w+)"', name)
        if m and m['group'] == group:
            yield m['member'], section


class Shuffler:
    def __init__(self, n: int) -> None:
        self._idx = random.sample(range(n), n)

    def shuffle(self, iterable: Iterable[_T]) -> List[_T]:
        lst = list(iterable)
        assert len(lst) == len(self._idx)
        return [lst[i] for i in self._idx]

    def deshuffle(self, iterable: Iterable[_T]) -> List[_T]:
        lst = list(iterable)
        assert len(lst) == len(self._idx)
        deshuffled: List[_T] = len(self._idx)*[None]  # type: ignore
        for i, x in zip(self._idx, lst):
            deshuffled[i] = x
        return deshuffled


def get_timestamp() -> str:
    return datetime.now().isoformat(timespec='seconds')


def make_nonwritable(path: 'os.PathLike[str]') -> None:
    os.chmod(
        path,
        stat.S_IMODE(os.lstat(path).st_mode) &
        ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    )


def sample(seq: Iterable[_T]) -> Iterator[_T]:
    queue = list(seq)
    while queue:
        yield queue.pop(random.randrange(0, len(queue)))


@contextmanager
def cd(path: Union[str, Path]) -> Iterator[None]:
    path = str(path)
    cwd = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(cwd)


def groupby(lst: Iterable[_T], key: Callable[[_T], _V]) \
        -> Iterator[Tuple[_V, List[_T]]]:
    keylst = [(key(x), x) for x in lst]
    keylst.sort(key=lambda x: x[0])
    for k, group in itertools.groupby(keylst, key=lambda x: x[0]):
        yield k, [x[1] for x in group]
