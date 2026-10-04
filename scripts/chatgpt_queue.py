"""Process-safe FIFO admission for one saved ChatGPT lane."""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import time


@contextmanager
def lane_turn(lane, check):
    state = Path(os.environ.get("FLEET_STATE_DIR") or
                 (os.environ.get("XDG_STATE_HOME") or "~/.local/state") + "/orchestrator").expanduser()
    path = state / "chatgpt-queue" / hashlib.sha256(lane.encode()).hexdigest()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    ticket = None
    with (path / "lock").open("a") as gate:
        @contextmanager
        def locked():
            while True:
                check()
                try:
                    fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(.05)
            try:
                yield
            finally:
                fcntl.flock(gate, fcntl.LOCK_UN)

        try:
            with locked():
                tickets = sorted(path.glob("*.ticket"))
                number = int(tickets[-1].stem) + 1 if tickets else 1
                ticket = (path / f"{number:020d}.ticket").open("x")
                # The OS releases this lock on death, including SIGKILL. A stale
                # waiter is removed without trusting a potentially reused PID.
                fcntl.flock(ticket, fcntl.LOCK_EX | fcntl.LOCK_NB)
            while True:
                with locked():
                    for candidate in sorted(path.glob("*.ticket")):
                        if str(candidate) == ticket.name:
                            break
                        try:
                            waiter = candidate.open("r")
                        except FileNotFoundError:
                            continue  # A cancelled waiter removed its marker.
                        with waiter:
                            try:
                                fcntl.flock(waiter, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                break
                            candidate.unlink(missing_ok=True)  # Abandoned queue marker only.
                    else:
                        raise RuntimeError("lane queue lost its ticket")
                    first = str(candidate) == ticket.name
                if first:
                    check()
                    yield
                    return
                time.sleep(.05)
        finally:
            if ticket is not None:
                # Never wait for a gate holder to clean up a cancelled turn.
                Path(ticket.name).unlink(missing_ok=True)
                ticket.close()
