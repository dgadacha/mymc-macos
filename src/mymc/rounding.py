"""Simple integer rounding helpers.

Originally ``round.py`` by Ross Ridge (public domain).  The Python 3 port
uses floor division so that the results stay integers.
"""

__all__ = ["div_round_up", "round_up", "round_down"]


def div_round_up(a: int, b: int) -> int:
    """Divide ``a`` by ``b``, rounding the result up."""
    return (a + b - 1) // b


def round_up(a: int, b: int) -> int:
    """Round ``a`` up to the next multiple of ``b``."""
    return (a + b - 1) // b * b


def round_down(a: int, b: int) -> int:
    """Round ``a`` down to the previous multiple of ``b``."""
    return a // b * b
