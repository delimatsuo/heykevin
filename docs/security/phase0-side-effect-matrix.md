# Phase 0 Side-Effect Matrix

This matrix is the human-readable companion to `app/services/side_effect_inventory.py`.

The canonical inventory lives in code so tests can enforce coverage. During Phase 0, each row must be verified against current code, assigned a backend gate where needed, and covered by a disabled-by-default test before v2 UI or copy can rely on it.

Run:

```bash
pytest tests/unit/test_phase0_side_effect_inventory.py -q
```

Expected: inventory completeness tests pass.
