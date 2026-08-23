from __future__ import annotations


class _CompatState:
    truecolor: bool = True
    force_ascii: bool = False


_compat = _CompatState()


def old(ascii_only: bool = False) -> None:
    _compat.truecolor = False
    _compat.force_ascii = ascii_only


def modern() -> None:
    _compat.truecolor = True
    _compat.force_ascii = False


def is_old() -> bool:
    return not _compat.truecolor
