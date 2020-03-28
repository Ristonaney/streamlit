# Copyright 2018-2020 Streamlit Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A hashing utility for code."""

import collections
import dis
import enum
import functools
import hashlib
import importlib
import inspect
import io
import os
import pickle
import re
import sys
import tempfile
import textwrap
import threading
import weakref
import types
from typing import Any, List, Pattern

from streamlit import config
from streamlit import file_util
from streamlit import type_util
from streamlit.errors import StreamlitAPIException, MarkdownFormattedException
from streamlit.folder_black_list import FolderBlackList
from streamlit.logger import get_logger

_LOGGER = get_logger(__name__)


# If a dataframe has more than this many rows, we consider it large and hash a sample.
_PANDAS_ROWS_LARGE = 100000
_PANDAS_SAMPLE_SIZE = 10000


# Similar to dataframes, we also sample large numpy arrays.
_NP_SIZE_LARGE = 1000000
_NP_SAMPLE_SIZE = 100000


# Arbitrary item to denote where we found a cycle in a hashed object.
# This allows us to hash self-referencing lists, dictionaries, etc.
_CYCLE_PLACEHOLDER = b"streamlit-57R34ML17-hesamagicalponyflyingthroughthesky-CYCLE"


_FOLDER_BLACK_LIST = FolderBlackList(config.get_option("server.folderWatchBlacklist"))


Context = collections.namedtuple("Context", ["globals", "cells", "varnames"])


def update_hash(val, hasher, hash_reason, hash_source, context=None, hash_funcs=None):
    """Updates a hashlib hasher with the hash of val.

    This is the main entrypoint to hashing.py.
    """
    hash_stacks.current.hash_reason = hash_reason
    hash_stacks.current.hash_source = hash_source

    ch = _CodeHasher(hash_funcs)
    ch.update(hasher, val, context)


class HashReason(enum.Enum):
    CACHING_FUNC_ARGS = 0
    CACHING_FUNC_BODY = 1
    CACHING_FUNC_OUTPUT = 2
    CACHING_BLOCK = 3


class _HashStack(object):
    """Stack of what has been hashed, for debug and circular reference detection.

    This internally keeps 1 stack per thread.

    Internally, this stores the ID of pushed objects rather than the objects
    themselves because otherwise the "in" operator inside __contains__ would
    fail for objects that don't return a boolean for "==" operator. For
    example, arr == 10 where arr is a NumPy array returns another NumPy array.
    This causes the "in" to crash since it expects a boolean.
    """

    def __init__(self):
        self._stack = (
            collections.OrderedDict()
        )  # type: collections.OrderedDict[int, List[Any]]

        # The reason why we're doing this hashing, for debug purposes.
        self.hash_reason = None

        # Either a function or a code block, depending on whether the reason is
        # due to hashing part of a function (i.e. body, args, output) or an
        # st.Cache codeblock.
        self.hash_source = None

    def push(self, val: Any):
        self._stack[id(val)] = val

    def pop(self):
        self._stack.popitem()

    def __contains__(self, val: Any):
        return id(val) in self._stack

    def pretty_print(self):
        def to_str(v):
            try:
                return "Object of type %s: %s" % (type_util.get_fqn_type(v), str(v))
            except:
                return "<Unable to convert item to string>"

        # IDEA: Maybe we should remove our internal "hash_funcs" from the
        # stack. I'm not removing those now because even though those aren't
        # useful to users I think they might be useful when we're debugging an
        # issue sent by a user. So let's wait a few months and see if they're
        # indeed useful...
        return "\n".join(to_str(x) for x in reversed(self._stack.values()))


class _HashStacks(object):
    """Stacks of what has been hashed, with at most 1 stack per thread."""

    def __init__(self):
        self._stacks = (
            weakref.WeakKeyDictionary()
        )  # type: weakref.WeakKeyDictionary[threading.Thread, _HashStack]

    @property
    def current(self):
        current_thread = threading.current_thread()

        stack = self._stacks.get(current_thread, None)

        if stack is None:
            stack = _HashStack()
            self._stacks[current_thread] = stack

        return stack


hash_stacks = _HashStacks()


def _is_magicmock(obj):
    return type_util.is_type(obj, "unittest.mock.MagicMock") or type_util.is_type(
        obj, "mock.mock.MagicMock"
    )


def _get_context(func) -> Context:
    code = func.__code__  # type: types.CodeType

    # Mapping from variable name to the value if we can resolve it.
    # Otherwise map to the name.
    cells = {}

    for var in code.co_cellvars:
        cells[var] = var  # Instead of value, we use the name.

    if code.co_freevars:
        assert len(code.co_freevars) == len(func.__closure__)
        cells.update(
            zip(code.co_freevars, map(lambda c: c.cell_contents, func.__closure__))  # type: ignore[no-any-return]
        )

    varnames = {}
    if inspect.ismethod(func):
        varnames = {"self": func.__self__}

    return Context(globals=func.__globals__, cells=cells, varnames=varnames)


def _int_to_bytes(i):
    num_bytes = (i.bit_length() + 8) // 8
    return i.to_bytes(num_bytes, "little", signed=True)


def _key(obj):
    """Return key for memoization."""

    if obj is None:
        return None

    def is_simple(obj):
        return (
            isinstance(obj, bytes)
            or isinstance(obj, bytearray)
            or isinstance(obj, str)
            or isinstance(obj, float)
            or isinstance(obj, int)
            or isinstance(obj, bool)
            or obj is None
        )

    if is_simple(obj):
        return obj

    if isinstance(obj, tuple):
        if all(map(is_simple, obj)):
            return obj

    if isinstance(obj, list):
        if all(map(is_simple, obj)):
            return ("__l", tuple(obj))

    if (
        type_util.is_type(obj, "pandas.core.frame.DataFrame")
        or type_util.is_type(obj, "numpy.ndarray")
        or inspect.isbuiltin(obj)
        or inspect.isroutine(obj)
        or inspect.iscode(obj)
    ):
        return id(obj)

    return NoResult


class _CodeHasher:
    """A hasher that can hash code objects including dependencies."""

    def __init__(self, hash_funcs=None):
        # Can't use types as the keys in the internal _hash_funcs because
        # we always remove user-written modules from memory when rerunning a
        # script in order to reload it and grab the latest code changes.
        # (See LocalSourcesWatcher.py:on_file_changed) This causes
        # the type object to refer to different underlying class instances each run,
        # so type-based comparisons fail. To solve this, we use the types converted
        # to fully-qualified strings as keys in our internal dict.
        if hash_funcs:
            self._hash_funcs = {
                k if isinstance(k, str) else type_util.get_fqn(k): v
                for k, v in hash_funcs.items()
            }
        else:
            self._hash_funcs = {}

        self._hashes = {}

        # The number of the bytes in the hash.
        self.size = 0

        # An ever increasing counter.
        self._counter = 0

    def to_bytes(self, obj, context=None):
        """Add memoization to _to_bytes and protect against cycles in data structures."""
        key = _key(obj)

        if key is not NoResult:
            if key in self._hashes:
                return self._hashes[key]

            # Add a tombstone hash to break recursive calls.
            self._counter += 1
            self._hashes[key] = b"tombstone:%s" % _int_to_bytes(self._counter)

        if obj in hash_stacks.current:
            return _CYCLE_PLACEHOLDER

        hash_stacks.current.push(obj)

        try:
            # Turn these on for debugging.
            # _LOGGER.debug("About to hash: %s", obj)
            tname = type(obj).__qualname__.encode()
            b = b"%s:%s" % (tname, self._to_bytes(obj, context))
            # _LOGGER.debug("Done hashing: %s", obj)

            # Hmmm... It's psosible that the size calculation is wrong. When we
            # call to_bytes inside _to_bytes things get double-counted.
            self.size += sys.getsizeof(b)

            if key is not None:
                self._hashes[key] = b

        except (UnhashableTypeError, UserHashError, InternalHashError):
            # Re-raise exceptions we hand-raise internally.
            raise

        except BaseException as e:
            raise InternalHashError(e, obj)

        finally:
            # In case an UnhashableTypeError (or other) error is thrown, clean up the
            # stack so we don't get false positives in future hashing calls
            hash_stacks.current.pop()

        return b

    def update(self, hasher, obj, context=None):
        """Update the provided hasher with the hash of an object."""
        b = self.to_bytes(obj, context)
        hasher.update(b)

    def _file_should_be_hashed(self, filename):
        filepath = os.path.abspath(filename)
        file_is_blacklisted = _FOLDER_BLACK_LIST.is_blacklisted(filepath)
        # Short circuiting for performance.
        if file_is_blacklisted:
            return False
        return file_util.file_is_in_folder_glob(
            filepath, self._get_main_script_directory()
        ) or file_util.file_in_pythonpath(filepath)

    def _to_bytes(self, obj, context):
        """Hash objects to bytes, including code with dependencies.

        Python's built in `hash` does not produce consistent results across
        runs.
        """

        if _is_magicmock(obj):
            # MagicMock can result in objects that appear to be infinitely
            # deep, so we don't try to hash them at all.
            return self.to_bytes(id(obj))

        elif isinstance(obj, bytes) or isinstance(obj, bytearray):
            return obj

        elif type_util.get_fqn_type(obj) in self._hash_funcs:
            # Escape hatch for unsupported objects
            hash_func = self._hash_funcs[type_util.get_fqn_type(obj)]
            try:
                output = hash_func(obj)
            except BaseException as e:
                raise UserHashError(e, obj, hash_func=hash_func)

            return self.to_bytes(output)

        elif isinstance(obj, str):
            return obj.encode()

        elif isinstance(obj, float):
            return self.to_bytes(hash(obj))

        elif isinstance(obj, int):
            return _int_to_bytes(obj)

        elif isinstance(obj, (list, tuple)):
            h = hashlib.new("md5")
            for item in obj:
                self.update(h, item, context)
            return h.digest()

        elif isinstance(obj, dict):
            h = hashlib.new("md5")
            for item in obj.items():
                self.update(h, item, context)
            return h.digest()

        elif obj is None:
            return b"0"

        elif obj is True:
            return b"1"

        elif obj is False:
            return b"0"

        elif type_util.is_type(obj, "pandas.core.frame.DataFrame") or type_util.is_type(
            obj, "pandas.core.series.Series"
        ):
            import pandas as pd

            if len(obj) >= _PANDAS_ROWS_LARGE:
                obj = obj.sample(n=_PANDAS_SAMPLE_SIZE, random_state=0)
            try:
                return b"%s" % pd.util.hash_pandas_object(obj).sum()
            except TypeError:
                # Use pickle if pandas cannot hash the object for example if
                # it contains unhashable objects.
                return b"%s" % pickle.dumps(obj, pickle.HIGHEST_PROTOCOL)

        elif type_util.is_type(obj, "numpy.ndarray"):
            h = hashlib.new("md5")
            self.update(h, obj.shape)

            if obj.size >= _NP_SIZE_LARGE:
                import numpy as np

                state = np.random.RandomState(0)
                obj = state.choice(obj.flat, size=_NP_SAMPLE_SIZE)

            self.update(h, obj.tobytes())
            return h.digest()

        elif inspect.isbuiltin(obj):
            return obj.__name__.encode()

        elif hasattr(obj, "name") and (
            isinstance(obj, io.IOBase)
            # Handle temporary files used during testing
            or isinstance(obj, tempfile._TemporaryFileWrapper)  # type: ignore[attr-defined]
        ):
            # Hash files as name + last modification date + offset.
            # NB: we're using hasattr("name") to differentiate between
            # on-disk and in-memory StringIO/BytesIO file representations.
            # That means that this condition must come *before* the next
            # condition, which just checks for StringIO/BytesIO.
            h = hashlib.new("md5")
            obj_name = getattr(obj, "name", "wonthappen")  # Just to appease MyPy.
            self.update(h, obj_name)
            self.update(h, os.path.getmtime(obj_name))
            self.update(h, obj.tell())
            return h.digest()

        elif isinstance(obj, Pattern):
            return self.to_bytes([obj.pattern, obj.flags])

        elif isinstance(obj, io.StringIO) or isinstance(obj, io.BytesIO):
            # Hash in-memory StringIO/BytesIO by their full contents
            # and seek position.
            h = hashlib.new("md5")
            self.update(h, obj.tell())
            self.update(h, obj.getvalue())
            return h.digest()

        elif type_util.is_type(obj, "numpy.ufunc"):
            # For numpy.remainder, this returns remainder.
            return obj.__name__.encode()

        elif inspect.isroutine(obj):
            if hasattr(obj, "__wrapped__"):
                # Ignore the wrapper of wrapped functions.
                return self.to_bytes(obj.__wrapped__)

            if obj.__module__.startswith("streamlit"):
                # Ignore streamlit modules even if they are in the CWD
                # (e.g. during development).
                return self.to_bytes("%s.%s" % (obj.__module__, obj.__name__))

            h = hashlib.new("md5")

            if self._file_should_be_hashed(obj.__code__.co_filename):
                context = _get_context(obj)
                if obj.__defaults__:
                    self.update(h, obj.__defaults__, context)
                h.update(self._code_to_bytes(obj.__code__, context))
            else:
                # Don't hash code that is not in the current working directory.
                self.update(h, obj.__module__)
                self.update(h, obj.__name__)
            return h.digest()

        elif inspect.iscode(obj):
            return self._code_to_bytes(obj, context)

        elif inspect.ismodule(obj):
            # TODO: Figure out how to best show this kind of warning to the
            # user. In the meantime, show nothing. This scenario is too common,
            # so the current warning is quite annoying...
            # st.warning(('Streamlit does not support hashing modules. '
            #             'We did not hash `%s`.') % obj.__name__)
            # TODO: Hash more than just the name for internal modules.
            return self.to_bytes(obj.__name__)

        elif inspect.isclass(obj):
            # TODO: Figure out how to best show this kind of warning to the
            # user. In the meantime, show nothing. This scenario is too common,
            # (e.g. in every "except" statement) so the current warning is
            # quite annoying...
            # st.warning(('Streamlit does not support hashing classes. '
            #             'We did not hash `%s`.') % obj.__name__)
            # TODO: Hash more than just the name of classes.
            return self.to_bytes(obj.__name__)

        elif isinstance(obj, functools.partial):
            # The return value of functools.partial is not a plain function:
            # it's a callable object that remembers the original function plus
            # the values you pickled into it. So here we need to special-case it.
            h = hashlib.new("md5")
            self.update(h, obj.args)
            self.update(h, obj.func)
            self.update(h, obj.keywords)
            return h.digest()

        else:
            # As a last resort, hash the output of the object's __reduce__ method
            h = hashlib.new("md5")
            try:
                reduce_data = obj.__reduce__()
            except BaseException as e:
                raise UnhashableTypeError(e, obj)

            for item in reduce_data:
                self.update(h, item, context)
            return h.digest()

    def _code_to_bytes(self, code, context):
        h = hashlib.new("md5")

        # Hash the bytecode.
        self.update(h, code.co_code)

        # Hash constants that are referenced by the bytecode but ignore names of lambdas.
        consts = [
            n
            for n in code.co_consts
            if not isinstance(n, str) or not n.endswith(".<lambda>")
        ]
        self.update(h, consts, context)

        for ref in get_referenced_objects(code, context):
            self.update(h, ref, context)

        return h.digest()

    @staticmethod
    def _get_main_script_directory():
        """Get the directory of the main script.
        """
        import __main__  # type: ignore[import]
        import os

        # This works because we set __main__.__file__ to the report
        # script path in ScriptRunner.
        main_path = __main__.__file__
        return os.path.dirname(main_path)


def get_referenced_objects(code, context):
    # Top of the stack
    tos = None  # type: Any
    lineno = None
    refs = []

    def set_tos(t):
        nonlocal tos
        if tos is not None:
            # Hash tos so we support reading multiple objects
            refs.append(tos)
        tos = t

    # Our goal is to find referenced objects. The problem is that co_names
    # does not have full qualified names in it. So if you access `foo.bar`,
    # co_names has `foo` and `bar` in it but it doesn't tell us that the
    # code reads `bar` of `foo`. We are going over the bytecode to resolve
    # from which object an attribute is requested.
    # Read more about bytecode at https://docs.python.org/3/library/dis.html

    for op in dis.get_instructions(code):
        try:
            # Sometimes starts_line is None, in which case let's just remember the
            # previous start_line (if any). This way when there's an exception we at
            # least can point users somewhat near the line where the error stems from.
            if op.starts_line is not None:
                lineno = op.starts_line

            if op.opname in ["LOAD_GLOBAL", "LOAD_NAME"]:
                if op.argval in context.globals:
                    set_tos(context.globals[op.argval])
                else:
                    set_tos(op.argval)
            elif op.opname in ["LOAD_DEREF", "LOAD_CLOSURE"]:
                set_tos(context.cells[op.argval])
            elif op.opname == "IMPORT_NAME":
                try:
                    set_tos(importlib.import_module(op.argval))
                except ImportError:
                    set_tos(op.argval)
            elif op.opname in ["LOAD_METHOD", "LOAD_ATTR", "IMPORT_FROM"]:
                if tos is None:
                    refs.append(op.argval)
                elif isinstance(tos, str):
                    tos += "." + op.argval
                else:
                    tos = getattr(tos, op.argval)
            elif op.opname == "DELETE_FAST" and tos:
                del context.varnames[op.argval]
                tos = None
            elif op.opname == "STORE_FAST" and tos:
                context.varnames[op.argval] = tos
                tos = None
            elif op.opname == "LOAD_FAST" and op.argval in context.varnames:
                set_tos(context.varnames[op.argval])
            else:
                # For all other instructions, hash the current TOS.
                if tos is not None:
                    refs.append(tos)
                    tos = None
        except Exception as e:
            raise UserHashError(e, code, lineno=lineno)

    return refs


class NoResult(object):
    """Placeholder class for return values when None is meaningful."""

    pass


class UnhashableTypeError(StreamlitAPIException):
    def __init__(self, orig_exc, failed_obj):
        msg = self._get_message(orig_exc, failed_obj)
        super(UnhashableTypeError, self).__init__(msg)
        self.with_traceback(orig_exc.__traceback__)

    def _get_message(self, orig_exc, failed_obj):
        args = _get_error_message_args(orig_exc, failed_obj)

        # This needs to have zero indentation otherwise %(hash_stack)s will
        # render incorrectly in Markdown.
        return (
            """
Cannot hash object of type `%(failed_obj_type_str)s`, found in %(object_part)s
%(object_desc)s.

While caching %(object_part)s %(object_desc)s, Streamlit encountered an
object of type `%(failed_obj_type_str)s`, which it does not know how to hash.

To address this, please try helping Streamlit understand how to hash that type
by passing the `hash_funcs` argument into `@st.cache`. For example:

```
@st.cache(hash_funcs={%(failed_obj_type_str)s: my_hash_func})
def my_func(...):
    ...
```

If you don't know where the object of type `%(failed_obj_type_str)s` is coming
from, try looking at the hash chain below for an object that you do recognize,
then pass that to `hash_funcs` instead:

```
%(hash_stack)s
```

Please see the `hash_funcs` [documentation]
(https://streamlit.io/docs/advanced_caching.html)
for more details.
            """
            % args
        ).strip("\n")


class UserHashError(StreamlitAPIException):
    def __init__(self, orig_exc, cached_func_or_code, hash_func=None, lineno=None):
        self.alternate_name = type(orig_exc).__name__

        if hash_func:
            msg = self._get_message_from_func(orig_exc, cached_func_or_code, hash_func)
        else:
            msg = self._get_message_from_code(orig_exc, cached_func_or_code, lineno)

        super(UserHashError, self).__init__(msg)
        self.with_traceback(orig_exc.__traceback__)

    def _get_message_from_func(self, orig_exc, cached_func, hash_func):
        args = _get_error_message_args(orig_exc, cached_func)

        if hasattr(hash_func, "__name__"):
            args["hash_func_name"] = "`%s()`" % hash_func.__name__
        else:
            args["hash_func_name"] = "a function"

        return (
            """
%(orig_exception_desc)s

This error is likely due to a bug in %(hash_func_name)s, which is a
user-defined hash function that was passed into the `@st.cache` decorator of
%(object_desc)s.

%(hash_func_name)s failed when hashing an object of type
`%(failed_obj_type_str)s`.  If you don't know where that object is coming from,
try looking at the hash chain below for an object that you do recognize, then
pass that to `hash_funcs` instead:

```
%(hash_stack)s
```

If you think this is actually a Streamlit bug, please [file a bug report here.]
(https://github.com/streamlit/streamlit/issues/new/choose)
            """
            % args
        ).strip("\n")

    def _get_message_from_code(self, orig_exc, cached_code, lineno):
        args = _get_error_message_args(orig_exc, cached_code)

        failing_lines = _get_failing_lines(cached_code, lineno)
        failing_lines_str = "".join(failing_lines)
        failing_lines_str = textwrap.dedent(failing_lines_str).strip("\n")

        args["failing_lines_str"] = failing_lines_str
        args["filename"] = cached_code.co_filename
        args["lineno"] = lineno

        # This needs to have zero indentation otherwise %(lines_str)s will
        # render incorrectly in Markdown.
        return (
            """
%(orig_exception_desc)s

Streamlit encountered an error while caching %(object_part)s %(object_desc)s.
This is likely due to a bug in `%(filename)s` near line `%(lineno)s`:

```
%(failing_lines_str)s
```

Please modify the code above to address this.

If you think this is actually a Streamlit bug, you may [file a bug report
here.] (https://github.com/streamlit/streamlit/issues/new/choose)
        """
            % args
        ).strip("\n")


class InternalHashError(MarkdownFormattedException):
    """Exception in Streamlit hashing code (i.e. not a user error)"""

    def __init__(self, orig_exc, failed_obj):
        msg = self._get_message(orig_exc, failed_obj)
        super(InternalHashError, self).__init__(msg)
        self.with_traceback(orig_exc.__traceback__)

    def _get_message(self, orig_exc, failed_obj):
        args = _get_error_message_args(orig_exc, failed_obj)

        # This needs to have zero indentation otherwise %(hash_stack)s will
        # render incorrectly in Markdown.
        return (
            """
%(orig_exception_desc)s

While caching %(object_part)s %(object_desc)s, Streamlit encountered an
object of type `%(failed_obj_type_str)s`, which it does not know how to hash.

**In this specific case, it's very likely you found a Streamlit bug so please
[file a bug report here.]
(https://github.com/streamlit/streamlit/issues/new/choose)**

In the meantime, you can try bypassing this error by registering a custom
hash function via the `hash_funcs` keyword in @st.cache(). For example:

```
@st.cache(hash_funcs={%(failed_obj_type_str)s: my_hash_func})
def my_func(...):
    ...
```

If you don't know where the object of type `%(failed_obj_type_str)s` is coming
from, try looking at the hash chain below for an object that you do recognize,
then pass that to `hash_funcs` instead:

```
%(hash_stack)s
```

Please see the `hash_funcs` [documentation]
(https://streamlit.io/docs/advanced_caching.html)
for more details.
            """
            % args
        ).strip("\n")


def _get_error_message_args(orig_exc, failed_obj):
    hash_reason = hash_stacks.current.hash_reason
    hash_source = hash_stacks.current.hash_source

    failed_obj_type_str = type_util.get_fqn_type(failed_obj)

    if hash_source is None or hash_reason is None:
        object_desc = "something"
        object_part = ""
        additional_explanation = ""

    elif hash_reason is HashReason.CACHING_BLOCK:
        object_desc = "a code block"
        object_part = ""
        additional_explanation = ""

    else:
        if hasattr(hash_source, "__name__"):
            object_desc = "`%s()`" % hash_source.__name__
            object_desc_specific = object_desc
        else:
            object_desc = "a function"
            object_desc_specific = "that function"

        if hash_reason is HashReason.CACHING_FUNC_ARGS:
            object_part = "the arguments of"
        elif hash_reason is HashReason.CACHING_FUNC_BODY:
            object_part = "the body of"
        elif hash_reason is HashReason.CACHING_FUNC_OUTPUT:
            object_part = "the return value of"

    return {
        "orig_exception_desc": str(orig_exc),
        "failed_obj_type_str": failed_obj_type_str,
        "hash_stack": hash_stacks.current.pretty_print(),
        "object_desc": object_desc,
        "object_part": object_part,
    }


def _get_failing_lines(code, lineno):
    """Get list of strings (lines of code) from lineno to lineno+3.

    Ideally we'd return the exact line where the error took place, but there
    are reasons why this is not possible without a lot of work, including
    playing with the AST. So for now we're returning 3 lines near where
    the error took place.
    """
    source_lines, source_lineno = inspect.getsourcelines(code)

    start = lineno - source_lineno
    end = min(start + 3, len(source_lines))
    lines = source_lines[start:end]

    return lines
