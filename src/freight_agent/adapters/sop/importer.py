"""`agent sop import <dir> [--broker]`: load a `sops/` tree into the SOP store as
published versions (standard, northline, keystone), idempotently.

Owner: card W3-KNW (stub from C0). It reads the files with `files.load_tree` (the
checks of `FileSopRepository`), so `tests/unit/test_sop_seed.py` can run through it.
"""
