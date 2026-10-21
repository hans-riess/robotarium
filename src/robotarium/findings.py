"""
What a check found: one sentence, how much it matters, and where.

Every check in this package -- the experiment's own fields, the static preflight over its files,
the simulator's verdict -- reports in this one shape, so `robotarium check` can print them
together and `submit` can refuse on any error whichever check raised it.
"""

from dataclasses import dataclass

ERROR = "error"
WARNING = "warning"
INFO = "info"

_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}


@dataclass(frozen=True)
class Finding:
    """
    One thing a check found.

    Args:
        level (str): ERROR blocks a submission; WARNING is shown and asked about; INFO is shown.
        message (str): What was found and what to do about it, as a sentence.
        file (str): The file it is about, by base name, when it is about one.
        line (int): The line in that file, when known.
    """

    level: str
    message: str
    file: str = None
    line: int = None

    def __str__(self) -> str:
        where = ""
        if self.file:
            where = "%s:%d: " % (self.file, self.line) if self.line else "%s: " % self.file
        return "%-7s %s%s" % (self.level.upper(), where, self.message)


def errors(findings: list) -> list:
    """The findings that block a submission."""
    return [each for each in findings if each.level == ERROR]


def ordered(findings: list) -> list:
    """Errors first, then warnings, then notes; otherwise in the order found."""
    return sorted(findings, key=lambda each: _ORDER.get(each.level, 3))
