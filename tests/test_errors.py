import pytest  # type: ignore

from caf2 import Rule, Session
from caf2.errors import NoActiveSession, ArgNotInSession, DependencyCycle, \
    UnhookableResult, TaskHookChangedHash

from tests.test_core import identity


def test_no_session():
    with pytest.raises(NoActiveSession):
        identity(10)


@pytest.mark.filterwarnings("ignore:tasks were never run")
def test_fut_not_in_session():
    with pytest.raises(ArgNotInSession):
        with Session():
            task = identity(1)
        with Session():
            identity(task[0])


@pytest.mark.filterwarnings("ignore:tasks were never run")
def test_arg_not_in_session():
    with pytest.raises(ArgNotInSession):
        with Session():
            task = identity(1)
        with Session():
            identity(task)


def test_dependency_cycle():
    @Rule
    def f(x):
        return f(x)

    with pytest.raises(DependencyCycle):
        with Session() as sess:
            sess.eval(f(1))


def test_unhookable():
    @Rule
    def f(x):
        return object()

    with pytest.raises(UnhookableResult):
        with Session() as sess:
            task = f(1)
            task.add_hook(lambda x: x)
            sess.eval(task)


def test_invalid_hook():
    with pytest.raises(TaskHookChangedHash):
        with Session() as sess:
            task = identity(1)
            task.add_hook(lambda x: 0)
            sess.eval(task)
