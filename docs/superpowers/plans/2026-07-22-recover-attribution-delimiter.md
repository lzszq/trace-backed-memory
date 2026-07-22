# Recover Attribution Delimiter Implementation Plan

1. Add end-to-end CLI regressions for decision IDs containing one and multiple
   `=` characters, including a suffix-like `=true` segment.
2. Confirm the current first-delimiter parser fails without replacing the
   snapshot.
3. Change attribution parsing to split on the final `=` while preserving all
   existing validation and error mapping.
4. Publish the grammar and unchanged persistence contract in README,
   architecture, usage policy, roadmap, and product documentation.
5. Run focused and full tests, compile sources, build wheel/sdist, verify
   packaged resources, and smoke-test the installed wheel.
6. Obtain independent code, test, and documentation reviews.
7. Fast-forward `main`, push, and require every GitHub Actions job to pass.
