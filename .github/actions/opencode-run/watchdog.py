"""Run OpenCode under a liveness watchdog instead of a fixed wall-clock timeout.

  watchdog.py --events FILE --stats FILE --idle-seconds N --hard-seconds N -- CMD...

CMD's stdout (the `--format json` event stream) goes to FILE; its stderr is passed
through to ours. Any new byte on either counts as progress. The command is killed
(its whole process group: TERM, then KILL after a grace period) when:

- it made no progress for --idle-seconds (exit STALLED), or
- it ran for --hard-seconds in total, however chatty (exit HARD_CAP).

Otherwise the command's own exit status is returned. Every --heartbeat-seconds a
line with the elapsed time and event count is printed, never any event content,
because the event stream itself is only printed (by extract.py) after the run.

The stats file (JSON) records the outcome, duration, event count and the longest
silence, which is the number to tune the idle budget from. extract.py, not this
script, records the cause: it reads the exit status and these stats.
"""

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any

HARD_CAP = 124  # same meaning as coreutils `timeout`: the wall-clock budget ran out
STALLED = 125  # no progress for the idle budget


def decide(now: float, start: float, last_progress: float, idle_s: float, hard_s: float) -> str:
    """The watchdog's whole policy: "hard_cap", "stalled", or "" (keep running)."""
    if now - start >= hard_s:
        return "hard_cap"
    if now - last_progress >= idle_s:
        return "stalled"
    return ""


def _clock(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 60}m{whole % 60:02d}s"


def _kill_group(proc: "subprocess.Popen[bytes]", grace_s: float) -> None:
    """TERM the command and everything it started, then KILL what is left.

    On POSIX the group is KILLed even when the leader has already exited: what it
    started (and left behind) would otherwise keep running and hold stderr open.
    """
    if os.name == "posix":
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=grace_s)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    elif proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            proc.kill()
    proc.wait()


def run(
    cmd: list[str],
    events_path: str,
    stats_path: str,
    idle_s: float,
    hard_s: float,
    heartbeat_s: float = 60.0,
    poll_s: float = 1.0,
    grace_s: float = 30.0,
) -> int:
    start = time.monotonic()
    progress_at = [start]  # last progress time, shared with the stderr thread
    longest_gap = 0.0
    events = 0
    seen = 0  # bytes of the event file already counted
    outcome = "exited"
    with open(events_path, "wb") as out:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )

    def on_signal(signum: int, _frame: Any) -> None:
        # The step was cancelled: never leave OpenCode running on its own.
        _kill_group(proc, min(grace_s, 5.0))
        sys.exit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, on_signal)

    def pump_stderr() -> None:
        stream = proc.stderr
        assert stream is not None
        for chunk in iter(lambda: stream.read1(4096), b""):
            progress_at[0] = time.monotonic()
            sys.stderr.buffer.write(chunk)
            sys.stderr.flush()

    reader = threading.Thread(target=pump_stderr, daemon=True)
    reader.start()
    next_beat = start + heartbeat_s
    while True:
        exited = proc.poll() is not None
        now = time.monotonic()
        with open(events_path, "rb") as fh:
            fh.seek(seen)
            fresh = fh.read()
        if fresh:
            seen += len(fresh)
            events += fresh.count(b"\n")
            progress_at[0] = now
        gap = now - progress_at[0]
        longest_gap = max(longest_gap, gap)
        if exited:
            break
        verdict = decide(now, start, progress_at[0], idle_s, hard_s)
        if verdict:
            outcome = verdict
            reason = (
                f"no progress for {_clock(gap)}"
                if verdict == "stalled"
                else f"hard cap of {_clock(hard_s)} reached"
            )
            print(f"::warning::OpenCode watchdog: {reason}; stopping it.", flush=True)
            _kill_group(proc, grace_s)
            break
        if now >= next_beat:
            print(
                f"heartbeat: {_clock(now - start)} elapsed, {events} events, "
                f"last progress {_clock(gap)} ago",
                flush=True,
            )
            next_beat += heartbeat_s
        time.sleep(poll_s)
    _kill_group(proc, grace_s)  # leftovers of a clean exit, too (POSIX)
    reader.join(timeout=5)
    elapsed = time.monotonic() - start
    status = proc.returncode
    if status < 0:  # killed by a signal (POSIX): report it the way a shell would
        status = 128 - status
    if outcome == "stalled":
        status = STALLED
    elif outcome == "hard_cap":
        status = HARD_CAP
    stats = {
        "outcome": outcome,
        "status": status,
        "elapsed_seconds": round(elapsed, 1),
        "events": events,
        "longest_silence_seconds": round(longest_gap, 1),
        "idle_seconds": idle_s,
        "hard_seconds": hard_s,
    }
    with open(stats_path, "w", encoding="utf-8") as fh:
        json.dump(stats, fh)
    print(
        f"watchdog: {outcome} (status {status}) after {_clock(elapsed)}, {events} events, "
        f"longest silence {_clock(longest_gap)}",
        flush=True,
    )
    return status


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--events", required=True)
    parser.add_argument("--stats", required=True)
    parser.add_argument("--idle-seconds", type=float, required=True)
    parser.add_argument("--hard-seconds", type=float, required=True)
    parser.add_argument("--heartbeat-seconds", type=float, default=60.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--grace-seconds", type=float, default=30.0)
    parser.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd or args.idle_seconds <= 0 or args.hard_seconds <= 0:
        parser.error("needs a command after -- and positive budgets")
    return run(
        cmd,
        args.events,
        args.stats,
        args.idle_seconds,
        args.hard_seconds,
        args.heartbeat_seconds,
        args.poll_seconds,
        args.grace_seconds,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
