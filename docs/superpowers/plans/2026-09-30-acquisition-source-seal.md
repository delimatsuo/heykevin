# Reviewed acquisition router registration seal

The final independent source review passes commit
785d6952e980282dbd0279697b62f6062afa9a76. The app/main.py diff adds exactly an
import of app.api.acquisition.router and app.include_router(acquisition_router).
It introduces authenticated, default-off acquisition endpoints; it does not
import or activate the offline voice bakeoff. Combined app qualification passes
87 tests. The Python suite correctly caught two stale main.py baseline hashes.

The existing seal comments permit repinning only alongside a reviewed main.py
change. Preserve those assertions and every other hash. Mechanical builder
gemini-3.7-flash-medium must update the main.py hash in exactly these files:

- tests/unit/test_voice_bakeoff_session_driver.py
- tests/unit/test_voice_bakeoff_turn_composition.py

Old SHA256: 06cd4871ed673137d0d86c6f3b1f9b0ed7d3c2998e23e15a886f2ac9ef39dd6e

Reviewed SHA256: 01400ead214cfa38caf4c621b3ded62f377bf902d91639874c3a9ad212aadcb1

Add a comment beside each pin identifying the reviewed September 30 acquisition
router registration, with voice bakeoff imports and activation still prohibited.
No production edits, other hash changes, tests/builds, Git mutations, provider
calls, credentials, .env reads or other worktrees. Parent owns tests and commits.
Return JSON changed_files and remaining_gaps. Do not edit this master brief.
