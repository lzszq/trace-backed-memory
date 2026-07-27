# Schema and packaged-resource guide

- Files here are canonical authoring sources.
- Installed copies live under `src/trace_backed_memory/_resources/schemas/`
  and must be byte-identical.
- Update `resources.py`, `pyproject.toml`, examples, tests, and documentation
  when adding or removing a resource.
- Every external object is closed by default with
  `additionalProperties: false` unless extensibility is intentional.
- Keep size, cardinality, string, enum, duplicate, and finite-number rules
  aligned with Python validators.
- A snapshot or database version change requires a migration and compatibility
  plan; never edit a version constant in isolation.
- Run `python tools/verify.py --full` to verify wheel and sdist bytes.
