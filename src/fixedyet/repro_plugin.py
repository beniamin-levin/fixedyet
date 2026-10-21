"""A pytest plugin for the test-writing agent's own runs (`agent.ReproAgent._run_test`): a failed
``==`` between values of unrelated types gets a note.

``assert tokens.target == [['x']]`` fails when ``tokens.target`` is a pyparsing ``ParseResults``,
whatever it holds, because a ``ParseResults`` never equals a list. pytest's own explanation shows
two lists that look alike, and a model reads that as the bug. The note says the types differ, and
whether the values are equal once both sides are converted to plain lists and dicts.

It runs inside the sandbox with the project's Python (3.6 and newer), so it uses the standard
library and the pytest hook only, and FixedYet never imports it. The side runs that decide the
verdict do not load it: it only changes what the agent reads.
"""

import collections.abc
import numbers

MARKER = "FIXEDYET NOTE"
_MAX_DEPTH = 20
_SCALARS = (str, bytes, bytearray, numbers.Number, type(None))


def _type_name(value):
    kind = type(value)
    module = getattr(kind, "__module__", "") or ""
    name = getattr(kind, "__qualname__", kind.__name__)
    return name if module == "builtins" else module + "." + name


def _unrelated(left, right):
    """True when == between the two cannot be about their values: neither type is the other's,
    they are not two numbers, and neither is None (comparing with None is a real check)."""
    if left is None or right is None:
        return False
    if isinstance(left, type(right)) or isinstance(right, type(left)):
        return False
    if isinstance(left, numbers.Number) and isinstance(right, numbers.Number):
        return False
    return True


def _plain(value, depth=0):
    """Lists for sized sequences, dicts for mappings, all the way down. Raises on anything odd."""
    if depth > _MAX_DEPTH:
        raise ValueError("too deep")
    if isinstance(value, _SCALARS):
        return value
    if isinstance(value, collections.abc.Mapping):
        return {key: _plain(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)) or (
        isinstance(value, collections.abc.Sized) and isinstance(value, collections.abc.Iterable)
        and not isinstance(value, collections.abc.Set)
    ):
        return [_plain(item, depth + 1) for item in value]
    return value


def _equal_as_plain(left, right):
    try:
        return bool(_plain(left) == _plain(right))
    except Exception:  # a value that cannot be walked: say nothing about it
        return False


def _short(value, limit=80):
    try:
        text = repr(value)
    except Exception:
        text = f"<{type(value).__name__}>"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _pytest_explanation(config, op, left, right):
    """pytest's own lines for the comparison (its diff), when this pytest exposes them."""
    try:
        from _pytest.assertion import util

        lines = util.assertrepr_compare(config=config, op=op, left=left, right=right)
    except Exception:
        return []
    return list(lines or [])


def pytest_assertrepr_compare(config, op, left, right):
    if op != "==" or not _unrelated(left, right):
        return None
    try:
        names = (_type_name(left), _type_name(right))
        lines = [
            f"{_short(left)} == {_short(right)}",
            f"{MARKER}: == compares a {names[0]} with a {names[1]}. Values of unrelated types "
            "are not equal, whatever they hold.",
        ]
        if _equal_as_plain(left, right):
            lines.append(
                f"{MARKER}: converted to plain lists and dicts the two sides ARE equal, so this "
                "assertion fails because of the types alone, not the values."
            )
        else:
            lines.append(
                f"{MARKER}: if the issue is about the values, convert first (for example "
                "list(...)); if it is about the type, assert it with isinstance()."
            )
        return lines + _pytest_explanation(config, op, left, right)[1:]
    except Exception:  # never break the run: pytest falls back to its own explanation
        return None
