#!/usr/bin/env python3
"""Run a command in a terminal and answer one debconf question when it appears.

The VM runbook's purge steps (docs/VM-RUNBOOK.md section 5.4) need a person to
answer "Delete the stored BioKids data for this family?". Typing ahead does not
work - the question is asked only after apt has finished removing files, and
the Readline frontend does not read what was typed before it asked. This waits
for the question, then types the answer, so the step can be run over ssh.

    sudo python3 dev/answer_debconf.py hayır env DEBIAN_FRONTEND=readline \\
        LANG=tr_TR.UTF-8 apt-get purge -y biokids-core

Everything the command printed is copied to standard output. Development only;
nothing ships this.
"""

from __future__ import annotations

import os
import pty
import select
import sys
import time

PROMPTS = (b"[evet/hay", b"[yes/no]")
QUIET_SECONDS = 300


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    answer = argv[1].encode("utf-8") + b"\n"
    command = argv[2:]

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(command[0], command)

    seen = b""
    answered = False
    while True:
        ready, _, _ = select.select([fd], [], [], QUIET_SECONDS)
        if not ready:
            print(f"\n[answer_debconf] nothing printed for {QUIET_SECONDS}s", file=sys.stderr)
            break
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        sys.stdout.buffer.write(chunk)
        sys.stdout.flush()
        seen = (seen + chunk)[-4096:]
        if not answered and any(prompt in seen for prompt in PROMPTS):
            time.sleep(0.5)
            os.write(fd, answer)
            answered = True

    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    print(f"\n[answer_debconf] answered={answered} exit={code}", file=sys.stderr)
    return code if answered else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
