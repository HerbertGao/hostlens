"""Test-support package for cassette recording (pytest-only).

Per design.md D-7 the ``RecordingBackend`` lives here — NOT in
``src/hostlens/agent/backends/`` — so the "non-production backend" boundary
is structural: it imports production code one-way (tests → src) but the
production runtime / daemon never imports it.
"""
