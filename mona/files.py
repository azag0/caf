# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
import json
from pathlib import Path
from abc import ABC, abstractmethod
from typing import Union, Optional, cast, Iterable, List, Callable, TypeVar, Type

from .sessions import Session
from .rules import Rule
from .hashing import Hash, Hashed, HashResolver, HashedBytes, HashedComposite
from .utils import make_nonwritable, Pathable, shorten_text

__version__ = '0.3.0'
__all__ = ['File', 'file_collection', 'add_source']

_R = TypeVar('_R', bound=Rule)  # type: ignore
_FM = TypeVar('_FM', bound='FileManager')


def add_source(path: Pathable) -> Callable[[_R], _R]:
    """Create a rule decorator to add a source to the task arguments.

    The source is passed as :class:`File`. The file argument is appended to the
    directly passed arguments.
    """

    def decorator(rule: _R) -> _R:
        rule.add_extra_arg(lambda: HashedFile(File.from_path(path)))
        return rule

    return decorator


@Rule
async def file_collection(files: List['File']) -> None:
    """Create a void task whose purpose is to label a file collection.

    :param files: a list of :class:`File`
    """
    pass


class FileManager(ABC):
    @abstractmethod
    def store_path(self, path: Path, *, keep: bool) -> 'Hash':
        ...

    @abstractmethod
    def store_bytes(self, content: bytes) -> 'Hash':
        ...

    @abstractmethod
    def get_bytes(self, content_hash: Hash) -> bytes:
        ...

    @abstractmethod
    def target_in(self, path: Path, content_hash: Hash, *, mutable: bool) -> None:
        ...

    @classmethod
    def active(cls: Type[_FM]) -> Optional[_FM]:
        fmngr = cast(Optional[_FM], Session.active().storage.get('file_manager'))
        assert not fmngr or isinstance(fmngr, cls)
        return fmngr


class File:
    """Represents a file located at an abstract relative path.

    Users should create instances by one of the classmethod constructors
    documented below rather than directly.
    """

    def __init__(self, path: Path, content: Union[bytes, Hash]):
        assert not path.is_absolute()
        self._path = path
        self._content = content
        if not isinstance(content, bytes):
            fmngr = FileManager.active()
            assert fmngr
            self._fmngr = fmngr

    def __repr__(self) -> str:
        if isinstance(self._content, bytes):
            content = repr(shorten_text(self._content, 20))
        else:
            content = self._content[:6]
        return f'<File path={self._path} content={content}>'

    def __str__(self) -> str:
        return str(self._path)

    @property
    def path(self) -> Path:
        """Abstract path to the file."""
        return self._path

    @property
    def stem(self) -> str:
        """Equivalent to :attr:`path.stem`."""
        return self._path.stem

    @property
    def name(self) -> str:
        """Equivalent to :attr:`path.name`."""
        return self._path.name

    @property
    def content(self) -> Union[bytes, Hash]:
        """Content as bytes or its hash."""
        return self._content

    def read_bytes(self) -> bytes:
        """Return content of the file as bytes."""
        if isinstance(self._content, bytes):
            return self._content
        return self._fmngr.get_bytes(self._content)

    def read_text(self) -> str:
        """Return content of the file as string."""
        return self.read_bytes().decode()

    def target_in(self, path: Path, *, mutable: bool = False) -> None:
        """Create an actual file or a symlink at the given location.

        :param Path path: where the file should be created
        :param bool mutable: whether the created file will be mutable
        """
        target = path / self._path
        if isinstance(self._content, bytes):
            target.write_bytes(self._content)
            if not mutable:
                make_nonwritable(target)
        else:
            self._fmngr.target_in(target, self._content, mutable=mutable)

    @classmethod
    def from_str(cls, path: Pathable, content: Union[str, bytes]) -> 'File':
        """Create a file from a string or bytes.

        :param path: the abstract path of the created file instance
        :param content: the content of the file
        """
        path = Path(path)
        if isinstance(content, str):
            content = content.encode()
        fmngr = FileManager.active()
        if fmngr:
            return cls(path, fmngr.store_bytes(content))
        return cls(path, content)

    @classmethod
    def from_path(
        cls, path: Pathable, root: Union[str, Path] = None, *, keep: bool = True
    ) -> 'File':
        """Create a file from a physical file.

        :param path: the path of the physical file. Also a basis for the
                     abstract path of the file instance.
        :param root: If given, the abstract path will be created from the
                     physical path taken relative to the root. If not given,
                     the ``path`` argument must be relative.
        :param bool keep: whether the physical file should kept or destroyed
        """
        path = Path(path)
        relpath = path.relative_to(root) if root else path
        fmngr = FileManager.active()
        if fmngr:
            return cls(relpath, fmngr.store_path(path, keep=keep))
        file = cls(relpath, path.read_bytes())
        if not keep:
            path.unlink()
        return file


class HashedFile(Hashed[File]):
    def __init__(self, file: File):
        self._path = file.path
        if isinstance(file.content, bytes):
            self._content: Optional[HashedBytes] = HashedBytes(file.content)
            self._content_hash = self._content.hashid
        else:
            self._content = None
            self._content_hash = file.content
        Hashed.__init__(self)

    @property
    def spec(self) -> bytes:
        return json.dumps([str(self._path), self._content_hash]).encode()

    @classmethod
    def from_spec(cls, spec: bytes, resolve: HashResolver) -> 'HashedFile':
        path_str: str
        content_hash: Hash
        path_str, content_hash = json.loads(spec)
        path = Path(path_str)
        fmngr = FileManager.active()
        if fmngr:
            return cls(File(path, content_hash))
        return cls(File(path, cast(HashedBytes, resolve(content_hash)).value))

    @property
    def value(self) -> File:
        return File(
            self._path, self._content.value if self._content else self._content_hash
        )

    @property
    def label(self) -> str:
        return f'./{self._path}'

    @property
    def components(self) -> Iterable['Hashed[object]']:
        if self._content:
            return (self._content,)
        return ()


HashedComposite.type_swaps[File] = HashedFile
