"""Define FIXTURE_DIR as an absolute POSIX path.

Interpolating a path into build_flags does not survive Windows: the
backslashes in `C:\\Users\\...` are eaten as C escape sequences and the macro
silently compiles to a path that cannot be opened. Computing it here and
normalising to forward slashes is the portable way to say the same thing.
"""

Import("env")

from pathlib import Path

fixtures = (Path(env["PROJECT_DIR"]) / "test" / "fixtures").as_posix()
env.Append(CPPDEFINES=[("FIXTURE_DIR", env.StringifyMacro(fixtures))])
