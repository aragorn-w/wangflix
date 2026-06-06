"""media_stack — shared Python package for the host-side pipeline.

Replaces the old top-level helper-and-script mix:
  - media_paths.py    →  media_stack.paths
  - media_lang.py     →  media_stack.lang
  - consolidate-subs.py / normalize-audio.py extracted into focused modules
    (probe, tags, state, audio, subtitles, mux, loudness, locking) +
    media_stack.clients.{arr,bazarr,qbit,telegram}

The two hyphenated entrypoint scripts at the repo root remain as thin
CLI wrappers because cron and consolidate-watch.service reference them
by absolute path.  Tests can import everything from this package
without `importlib.util.spec_from_file_location` tricks.
"""
