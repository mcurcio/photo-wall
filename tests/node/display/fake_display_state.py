"""The scripted display's state file, shared by the display tests and the fake tools they run.

A fake tool is its own process, run with `-I`, so it cannot import this module: it embeds this
file's source (`SOURCE`) ahead of its own. A change is a read-modify-write under an exclusive
`flock` on a side lock file, and the new state replaces the file atomically (`os.replace`), so a
reader never sees a torn or empty file and two writers never lose each other's change.
"""
import contextlib
import fcntl
import json
import os


def read_state(path):
    with open(path) as file:
        return json.load(file)


@contextlib.contextmanager
def changing_state(path):
    """Yield the current state (empty if none yet) to change in place; it is saved on exit."""
    path = os.fspath(path)
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state(path) if os.path.exists(path) else {}
        yield state
        temporary = "%s.%d.%d.tmp" % (path, os.getpid(), id(state))
        with open(temporary, "w") as file:
            json.dump(state, file)
        os.replace(temporary, path)


if __name__ != "__main__":                  # imported by a test, not embedded in a fake tool
    with open(__file__) as _self:
        SOURCE = _self.read()
