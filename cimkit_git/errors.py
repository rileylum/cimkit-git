"""Typed errors raised by library code. cli.py maps them to exit codes."""


class CimkitError(Exception):
    pass


class NotAZipError(CimkitError):
    pass


class Pro2ProjectError(CimkitError):
    pass


class DuplicateEntryError(CimkitError):
    pass


class EntryParseError(CimkitError):
    pass


class BadEntryNameError(CimkitError):
    pass


class SymlinkInSourceError(CimkitError):
    pass


class ConfigError(CimkitError):
    pass


class ValuesError(CimkitError):
    pass


class AmbiguousValueError(CimkitError):
    pass


class NotAGitRepoError(CimkitError):
    pass


class StateError(CimkitError):
    pass


class PlaceholderError(CimkitError):
    """Carries the problems from neutralise or resolve; cli.py words each one.

    target is set only when explode raised it: the target a registered value belongs to.
    A build's unregistered value is already in Source, so registering it fixes nothing.
    """

    def __init__(self, problems, target=None):
        super().__init__(f"{len(problems)} placeholder problem(s)")
        self.problems = problems
        self.target = target


class RefusedError(CimkitError):
    """A write that would lose an edit or can't run yet; .status says why."""

    def __init__(self, status):
        super().__init__(status.value)
        self.status = status


class NotAProjectError(CimkitError):
    pass


class LockedError(CimkitError):
    pass


class WriteError(CimkitError):
    pass


class RegisterError(CimkitError):
    """A key or value that can't be written safely; register it by hand."""
