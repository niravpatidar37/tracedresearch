"""Record a real terminal session into an asciicast v2 file, without asciinema or a pty.

    python assets/demo/record.py assets/demo/demo.cast [--bash PATH] [-- session command...]

Runs the session (default: `bash assets/demo/session.sh` from the repo root) as a plain subprocess,
reads its combined stdout/stderr as it arrives and stamps every chunk with the real elapsed time.
Because there is no pty, lone "\\n" is written as "\\r\\n" (what a terminal's onlcr would do).
Nothing is edited, reordered or synthesised: the .cast is exactly the bytes the session printed.
"""

from __future__ import annotations

import codecs
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COLS, ROWS = 100, 30


def find_bash(explicit: str | None) -> str:
    if explicit:
        return explicit
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")  # avoid WSL's System32\bash.exe on Windows
    if git_bash.exists():
        return str(git_bash)
    found = shutil.which("bash")
    if not found:
        raise SystemExit("bash not found")
    return found


def main(argv: list[str]) -> None:
    out = Path(argv[0])
    bash = argv[argv.index("--bash") + 1] if "--bash" in argv else None
    cmd = argv[argv.index("--") + 1 :] if "--" in argv else [find_bash(bash), "assets/demo/session.sh"]
    env = dict(os.environ, TERM="xterm-256color", COLUMNS=str(COLS), LINES=str(ROWS), PYTHONUNBUFFERED="1")
    header = {
        "version": 2,
        "width": COLS,
        "height": ROWS,
        "timestamp": int(time.time()),
        "env": {"TERM": "xterm-256color", "SHELL": "/bin/bash"},
        "title": "tracedresearch - real session",
    }
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    events: list[list[object]] = []
    prev_cr = False
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    while True:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        text = decoder.decode(chunk)
        if not text:
            continue
        fixed = []
        for ch in text:
            if ch == "\n" and not prev_cr:
                fixed.append("\r")
            fixed.append(ch)
            prev_cr = ch == "\r"
        data = "".join(fixed)
        events.append([round(time.monotonic() - t0, 6), "o", data])
        sys.stdout.write(data)
        sys.stdout.flush()
    code = proc.wait()
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(header) + "\n")
        for ev in events:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    dur = events[-1][0] if events else 0
    print(f"\nwrote {out}: {len(events)} events, {dur:.1f} s real time, session exit code {code}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1:])
