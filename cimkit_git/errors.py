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
