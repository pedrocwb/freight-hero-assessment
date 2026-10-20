"""`SopIndex` over the published sections in Postgres (pgvector cosine plus full text,
fused by reciprocal rank).

Owner: card W3-KNW (stub from C0).

Contract with the composition root: define `knowledge_index(settings: Settings) ->
SopIndex` in this module, under that name. `bootstrap.build_knowledge` loads it by name
for `PERSISTENCE=postgres` and, while it does not exist, reads the `sops/` files instead
(`MemorySopIndex.from_files`), as before wave 3. Migration 0004 puts the `vector`
extension in `public` (scratch schemas set `search_path` to their own schema only, F9):
columns are `public.vector(1536)` and the operator `OPERATOR(public.<=>)`.
"""
