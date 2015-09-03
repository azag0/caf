from pathlib import Path
import yaml

import imp
import os
from collections import namedtuple
import json
from contextlib import contextmanager
import subprocess
import hashlib
import tempfile
import shutil
from string import Template
import re

NULL_SHA = 40*'0'


class Calculation(object):
    def __init__(self, *args, **kwargs):
        self.files = [File(f) for f in args]
        self.kwargs = kwargs

    def prepare(self):
        for f in self.files:
            f.consume(self.kwargs)


class File(object):
    _cache = {}

    def __init__(self, path):
        self.path = Path(path)
        self.full_path = self.path.resolve()
        if self.full_path not in File._cache:
            File._cache[self.full_path] = Template(self.path.open().read())

    def consume(self, mapping):
        with self.path.open('w') as f:
            f.write(File._cache[self.full_path].substitute(mapping))


Result = namedtuple('Result', ['param', 'data'])
Task = namedtuple('Task', ['param', 'calc'])


def slugify(s):
    return re.sub(r'[^0-9a-zA-Z.-]', '-', s)


def get_sha_dir(top='.'):
    top = Path(top)
    h = hashlib.new('sha1')
    for path in sorted(top.glob('**/*')):
        h.update(str(path))
        with path.open() as f:
            h.update(f.read())
    return h.hexdigest()


def sha_to_path(sha, level=2, chunk=2):
    levels = []
    for l in range(level):
        levels.append(sha[l*chunk:(l+1)*chunk])
    levels.append(sha[level*chunk:])
    path = Path(levels[0])
    for l in levels[1:]:
        path = path/l
    return path


@contextmanager
def mktmpdir():
    tmpdir = tempfile.mkdtemp()
    yield tmpdir
    if Path(tmpdir).is_dir():
        shutil.rmtree(tmpdir)


@contextmanager
def cd(path):
    path = str(path)
    cwd = os.getcwd()
    os.chdir(path)
    yield
    os.chdir(cwd)


def find_program(cmd):
    return Path(subprocess.check_output(['which', cmd]).strip()).resolve()


def _load_cscript():
    cscript = imp.new_module('cscript')
    try:
        exec(compile(open('cscript').read(), 'cscript', 'exec'), cscript.__dict__)
    except:
        import traceback
        import sys
        print('There was an error while reading cscript.')
        traceback.print_exc()
        sys.exit(1)
    return cscript


class Context(object):
    def __init__(self):
        if Path('HEAD').is_file():
            self.sha_repo = open('HEAD').read().strip()
            self.clean = self.sha_repo != NULL_SHA
        else:
            diff = subprocess.check_output('git status --porcelain'.split())
            if diff:
                self.clean = False
                self.sha_repo = NULL_SHA
            else:
                self.clean = True
                self.sha_repo = subprocess.check_output('git rev-parse HEAD'.split()).strip()
        self.tasks = []
        out = Path('build')
        self.rundir = out/(self.sha_repo[:7] + '_runs')
        self.datafile = out/(self.sha_repo[:7] + '_data.p')
        self.resultdir = out/(self.sha_repo[:7] + '_results')
        cscript = _load_cscript()
        self.prepare = lambda: cscript.prepare(self)
        self.extract = lambda: cscript.extract(self)
        self.process = lambda: cscript.process(self)
        cafdir = Path(os.environ['HOME'])/'.caf'
        self.cafdir = cafdir if cafdir.is_dir() else None
        if self.cafdir:
            with (self.cafdir/'conf.yaml').open() as f:
                conf = yaml.load(f)
        else:
            conf = {}
        self.top = Path(getattr(cscript, 'top', conf.get('top'))).resolve()
        if 'cache' in conf:
            self.cache = Path(conf['cache']).resolve()
        else:
            cache = Path('_cache')
            if not cache.is_dir():
                cache.mkdir()
            self.cache = cache

    def add_task(self, calc, **param):
        self.tasks.append(Task(param, calc))


class ArrayEncoder(json.JSONEncoder):
    def default(self, obj):
        try:
            return obj.tolist()
        except AttributeError:
            return super().default(obj)
