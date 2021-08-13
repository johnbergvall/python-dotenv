import io
import logging
import os
import shutil
import sys
import tempfile
from collections import OrderedDict
from contextlib import contextmanager
from typing import (IO, Dict, Iterable, Iterator, Mapping, Optional, Tuple,
                    Union)

from .parser import Binding, parse_stream
from .variables import parse_variables

logger = logging.getLogger(__name__)

if sys.version_info >= (3, 6):
    _PathLike = os.PathLike
else:
    _PathLike = str


def with_warn_for_invalid_lines(mappings: Iterator[Binding]) -> Iterator[Binding]:
    for mapping in mappings:
        if mapping.error:
            logger.warning(
                "Python-dotenv could not parse statement starting at line %s",
                mapping.original.line,
            )
        yield mapping


class DotEnv():
    def __init__(
        self,
        dotenv_path: Optional[Union[str, _PathLike]],
        stream: Optional[IO[str]] = None,
        verbose: bool = False,
        encoding: Union[None, str] = None,
        interpolate: bool = True,
        override: bool = True,
        base_env: Mapping[str, Optional[str]] = os.environ
    ) -> None:
        self.dotenv_path = dotenv_path  # type: Optional[Union[str, _PathLike]]
        self.stream = stream  # type: Optional[IO[str]]
        self._dict = None  # type: Optional[Dict[str, Optional[str]]]
        self.verbose = verbose  # type: bool
        self.encoding = encoding  # type: Union[None, str]
        self.interpolate = interpolate  # type: bool
        self.override = override  # type: bool
        self.base_env = base_env  # type: Mapping[str, Optional[str]]

    @contextmanager
    def _get_stream(self) -> Iterator[IO[str]]:
        if self.dotenv_path and os.path.isfile(self.dotenv_path):
            with io.open(self.dotenv_path, encoding=self.encoding) as stream:
                yield stream
        elif self.stream is not None:
            yield self.stream
        else:
            if self.verbose:
                logger.info(
                    "Python-dotenv could not find configuration file %s.",
                    self.dotenv_path or '.env',
                )
            yield io.StringIO('')

    def dict(self) -> Dict[str, Optional[str]]:
        """Return dotenv as dict"""
        if self._dict:
            return self._dict

        raw_values = self.parse()
        return self.update_dict(raw_values)

    def update_dict(
        self,
        raw_values: Union[Dict[str, Optional[str]], Iterator[Tuple[str, Optional[str]]]],
    ) -> Dict[str, Optional[str]]:
        """
        Update already parsed dict with new `raw_values`
        """
        target_dict = self._dict if self._dict is not None else OrderedDict()

        if isinstance(raw_values, dict):
            raw_values = iter(raw_values.items())

        if self.interpolate:
            if self.override:
                base_env = {**self.base_env, **(self._dict or {})}
            else:
                base_env = {**(self._dict or {}), **self.base_env}
            target_dict.update(
                resolve_variables(raw_values, override=self.override, base_env=base_env)
            )
        else:
            target_dict.update(raw_values)

        self._dict = target_dict
        return self._dict

    def parse(self) -> Iterator[Tuple[str, Optional[str]]]:
        with self._get_stream() as stream:
            for mapping in with_warn_for_invalid_lines(parse_stream(stream)):
                if mapping.key is not None:
                    yield mapping.key, mapping.value

    def set_as_environment_variables(self) -> bool:
        """
        Load the current dotenv as system environment variable.
        """
        for k, v in self.dict().items():
            if k in os.environ and not self.override:
                continue
            if v is not None:
                os.environ[k] = v

        return True

    def get(self, key: str) -> Optional[str]:
        """
        """
        data = self.dict()

        if key in data:
            return data[key]

        if self.verbose:
            logger.warning("Key %s not found in %s.", key, self.dotenv_path)

        return None


def get_key(dotenv_path: Union[str, _PathLike], key_to_get: str) -> Optional[str]:
    """
    Gets the value of a given key from the given .env

    If the .env path given doesn't exist, fails
    """
    return DotEnv(dotenv_path, verbose=True).get(key_to_get)


@contextmanager
def rewrite(path: Union[str, _PathLike]) -> Iterator[Tuple[IO[str], IO[str]]]:
    try:
        if not os.path.isfile(path):
            with io.open(path, "w+") as source:
                source.write("")
        with tempfile.NamedTemporaryFile(mode="w+", delete=False) as dest:
            with io.open(path) as source:
                yield (source, dest)  # type: ignore
    except BaseException:
        if os.path.isfile(dest.name):
            os.unlink(dest.name)
        raise
    else:
        shutil.move(dest.name, path)


def set_key(
    dotenv_path: Union[str, _PathLike],
    key_to_set: str,
    value_to_set: str,
    quote_mode: str = "always",
    export: bool = False,
) -> Tuple[Optional[bool], str, str]:
    """
    Adds or Updates a key/value to the given .env

    If the .env path given doesn't exist, fails instead of risking creating
    an orphan .env somewhere in the filesystem
    """
    if quote_mode not in ("always", "auto", "never"):
        raise ValueError("Unknown quote_mode: {}".format(quote_mode))

    quote = (
        quote_mode == "always"
        or (quote_mode == "auto" and not value_to_set.isalnum())
    )

    if quote:
        value_out = "'{}'".format(value_to_set.replace("'", "\\'"))
    else:
        value_out = value_to_set
    if export:
        line_out = 'export {}={}\n'.format(key_to_set, value_out)
    else:
        line_out = "{}={}\n".format(key_to_set, value_out)

    with rewrite(dotenv_path) as (source, dest):
        replaced = False
        for mapping in with_warn_for_invalid_lines(parse_stream(source)):
            if mapping.key == key_to_set:
                dest.write(line_out)
                replaced = True
            else:
                dest.write(mapping.original.string)
        if not replaced:
            dest.write(line_out)

    return True, key_to_set, value_to_set


def unset_key(
    dotenv_path: Union[str, _PathLike],
    key_to_unset: str,
    quote_mode: str = "always",
) -> Tuple[Optional[bool], str]:
    """
    Removes a given key from the given .env

    If the .env path given doesn't exist, fails
    If the given key doesn't exist in the .env, fails
    """
    if not os.path.exists(dotenv_path):
        logger.warning("Can't delete from %s - it doesn't exist.", dotenv_path)
        return None, key_to_unset

    removed = False
    with rewrite(dotenv_path) as (source, dest):
        for mapping in with_warn_for_invalid_lines(parse_stream(source)):
            if mapping.key == key_to_unset:
                removed = True
            else:
                dest.write(mapping.original.string)

    if not removed:
        logger.warning("Key %s not removed from %s - key doesn't exist.", key_to_unset, dotenv_path)
        return None, key_to_unset

    return removed, key_to_unset


def resolve_variables(
    values: Iterable[Tuple[str, Optional[str]]],
    override: bool,
    base_env: Mapping[str, Optional[str]] = os.environ,
) -> Mapping[str, Optional[str]]:
    new_values = {}  # type: Dict[str, Optional[str]]

    for (name, value) in values:
        if value is None:
            result = None
        else:
            atoms = parse_variables(value)
            env = {}  # type: Dict[str, Optional[str]]
            if override:
                env.update(base_env)  # type: ignore
                env.update(new_values)
            else:
                env.update(new_values)
                env.update(base_env)  # type: ignore
            result = "".join(atom.resolve(env) for atom in atoms)

        new_values[name] = result

    return new_values


def _walk_to_root(path: str) -> Iterator[str]:
    """
    Yield directories starting from the given directory up to the root
    """
    if not os.path.exists(path):
        raise IOError('Starting path not found')

    if os.path.isfile(path):
        path = os.path.dirname(path)

    last_dir = None
    current_dir = os.path.abspath(path)
    while last_dir != current_dir:
        yield current_dir
        parent_dir = os.path.abspath(os.path.join(current_dir, os.path.pardir))
        last_dir, current_dir = current_dir, parent_dir


def find_dotenv(
    filename: str = '.env',
    raise_error_if_not_found: bool = False,
    usecwd: bool = False,
) -> str:
    """
    Search in increasingly higher folders for the given file

    Returns path to the file if found, or an empty string otherwise
    """

    def _is_interactive():
        """ Decide whether this is running in a REPL or IPython notebook """
        main = __import__('__main__', None, None, fromlist=['__file__'])
        return not hasattr(main, '__file__')

    if usecwd or _is_interactive() or getattr(sys, 'frozen', False):
        # Should work without __file__, e.g. in REPL or IPython notebook.
        path = os.getcwd()
    else:
        # will work for .py files
        frame = sys._getframe()
        current_file = __file__

        while frame.f_code.co_filename == current_file:
            assert frame.f_back is not None
            frame = frame.f_back
        frame_filename = frame.f_code.co_filename
        path = os.path.dirname(os.path.abspath(frame_filename))

    for dirname in _walk_to_root(path):
        check_path = os.path.join(dirname, filename)
        if os.path.isfile(check_path):
            return check_path

    if raise_error_if_not_found:
        raise IOError('File not found')

    return ''


def load_dotenv(
    dotenv_path: Union[str, _PathLike, None] = None,
    stream: Optional[IO[str]] = None,
    verbose: bool = False,
    override: bool = False,
    interpolate: bool = True,
    encoding: Optional[str] = "utf-8",
) -> bool:
    """Parse a .env file and then load all the variables found as environment variables.

    - *dotenv_path*: absolute or relative path to .env file.
    - *stream*: Text stream (such as `io.StringIO`) with .env content, used if
      `dotenv_path` is `None`.
    - *verbose*: whether to output a warning the .env file is missing. Defaults to
      `False`.
    - *override*: whether to override the system environment variables with the variables
      in `.env` file.  Defaults to `False`.
    - *encoding*: encoding to be used to read the file.

    If both `dotenv_path` and `stream`, `find_dotenv()` is used to find the .env file.
    """
    if dotenv_path is None and stream is None:
        dotenv_path = find_dotenv()

    dotenv = DotEnv(
        dotenv_path=dotenv_path,
        stream=stream,
        verbose=verbose,
        interpolate=interpolate,
        override=override,
        encoding=encoding,
    )
    return dotenv.set_as_environment_variables()


def dotenv_values(
    dotenv_path: Union[str, _PathLike, None] = None,
    stream: Optional[IO[str]] = None,
    verbose: bool = False,
    override: bool = True,
    interpolate: bool = True,
    encoding: Optional[str] = "utf-8",
    base_env: Mapping[str, Optional[str]] = os.environ,
) -> Dict[str, Optional[str]]:
    """
    Parse a .env file and return its content as a dict.

    - *dotenv_path*: absolute or relative path to .env file.
    - *stream*: `StringIO` object with .env content, used if `dotenv_path` is `None`.
    - *verbose*: whether to output a warning the .env file is missing. Defaults to
      `False`.
    - *override*: whether to override the system environment/`base_env` variables with
      the variables in `.env` file. Defaults to `True` as opposed to `load_dotenv`.
    - *encoding*: encoding to be used to read the file.
    - *base_env*: dict with initial environment. Defaults to os.environ

    If both `dotenv_path` and `stream`, `find_dotenv()` is used to find the .env file.
    """
    if dotenv_path is None and stream is None:
        dotenv_path = find_dotenv()

    return DotEnv(
        dotenv_path=dotenv_path,
        stream=stream,
        verbose=verbose,
        interpolate=interpolate,
        override=override,
        encoding=encoding,
        base_env=base_env,
    ).dict()


def chained_dotenv_values(
    dotenv_paths_or_streams: Iterable[Union[str, _PathLike, IO[str], None]],
    verbose: bool = False,
    interpolate: bool = True,
    encoding: Optional[str] = "utf-8",
    base_env: Mapping[str, Optional[str]] = os.environ,
) -> Dict[str, Optional[str]]:
    """
    Parse multiple .env files/streams after each other and return the merged content
    as a dict.

    - *dotenv_paths_or_streams*: list of `dotenv_path` and `stream` arguments
      to `dotenv_values()`
    - *verbose*: whether to output a warning the .env file is missing. Defaults to
      `False`.
    - *encoding*: encoding to be used to read the file.
    - *base_env*: dict with initial environment. Defaults to os.environ
    """
    result = None

    if not dotenv_paths_or_streams or any(arg is None for arg in dotenv_paths_or_streams):
        raise ValueError(
            'Filenames and/or stream arguments are required for chained loading. Use '
            'find_dotenv() to auto detect env-file'
        )

    for argument in dotenv_paths_or_streams:
        dotenv_path = stream = None
        if isinstance(argument, (str, _PathLike)):
            dotenv_path = argument
        elif isinstance(argument, io.IOBase):
            stream = argument

        cur = DotEnv(
            dotenv_path=dotenv_path,
            stream=stream,
            verbose=verbose,
            interpolate=interpolate,
            override=True,
            encoding=encoding,
            base_env=base_env,
        )
        if not result:
            result = cur

        result.update_dict(cur.parse())

    assert result  # type check
    return result.dict()
