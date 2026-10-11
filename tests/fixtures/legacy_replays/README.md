# Archived demo replays

The current app does not use these files. The small catalogs retain the original raw-to-compact counts and hashes. The large replay JSON files are ignored by Git and preserved locally.

Two optional artifact tests read them when available. Current engine and API tests use their own configurations and verified history.

To rebuild the old artifacts from existing raw timelines, run from the repository root:

```bash
python -m kuru_backtester.compact_replays
python -m kuru_backtester.legacy.build_lp_frontend
```

These commands write here. They do not add demo data to the public frontend.
