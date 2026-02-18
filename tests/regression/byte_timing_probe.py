#!/usr/bin/env python3
"""
Byte-timing probe for tmux send-keys investigation.

Reads stdin byte-by-byte in raw tty mode and prints each byte with a
relative timestamp from the previous byte. Used to measure the inter-byte
gap between text characters and the trailing \\r (0x0D) in two delivery
approaches:

  Atomic (broken):  tmux send-keys -t <pane> -- "text\\r"
  Two-call (fixed): tmux send-keys -t <pane> -- "text"
                    sleep 0.3
                    tmux send-keys -t <pane> Enter

Usage:
  Run this script in a tmux pane, then from another pane send input
  using each approach and observe the delta before the 0x0d byte.

  tmux new-session -d -s probe
  tmux send-keys -t probe "python3 tests/regression/byte_timing_probe.py" Enter

  # Atomic test:
  tmux send-keys -t probe -- $'hello\\r'

  # Two-call test:
  tmux send-keys -t probe -- "world"
  sleep 0.3
  tmux send-keys -t probe Enter

Press Ctrl-C to exit the probe.

Expected results (from #178 investigation):
  Atomic:   \\r arrives 0.0ms after last text char  (same burst)
  Two-call: \\r arrives ~300ms after last text char (separate event)
"""
import sys
import os
import time
import tty
import termios

fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
try:
    tty.setraw(fd)
    sys.stdout.write("READY — send input, Ctrl-C to exit\r\n")
    sys.stdout.flush()
    t_prev = None
    while True:
        b = os.read(fd, 1)
        ts = time.time()
        if b == b'\x03':  # Ctrl-C
            sys.stdout.write("\r\nEXIT\r\n")
            sys.stdout.flush()
            break
        delta = (ts - t_prev) * 1000 if t_prev is not None else 0.0
        t_prev = ts
        sys.stdout.write(f"t+{delta:7.1f}ms  0x{b.hex()}  {repr(b)}\r\n")
        sys.stdout.flush()
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
