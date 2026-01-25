from enum import Enum, auto


class Backend(Enum):
    """Available backends for remote execution."""

    SUBPROCESS = auto()
