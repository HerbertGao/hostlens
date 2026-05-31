from __future__ import annotations

# Probe scripts under tests/manual/ are runnable diagnostics, not pytest cases.
# They are named *_probe.py so pytest's default test_*/*_test discovery skips
# them, but this guard keeps that invariant explicit if a file is ever renamed.
collect_ignore_glob = ["*_probe.py"]
