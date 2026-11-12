---
name: sync-schemas
description: Regenerate and check the JSON Schema files in schemas/ after changing a document in schemas.py. Use whenever a versioned document's fields, enums or constraints change.
---

# Sync schemas

The JSON Schema files in `schemas/` are generated output. Their source is
`schemas.json_schemas()` in `src/openclaw_ecc_orchestrator/schemas.py`,
which builds each schema from the `_<document>_schema()` definitions there
and takes the shared values (`SCHEMA_VERSION`, `ID_PATTERN`, required lists
and enums) from `DOCUMENT_SPECS` and the module constants. Never edit a file
in `schemas/` by hand.

## Steps

1. Edit the validator and `DOCUMENT_SPECS` in `schemas.py`, then the matching
   `_<document>_schema()` definition (descriptions, types, constraints,
   `additionalProperties`).
2. Regenerate the files:

   ```sh
   PYTHONPATH=src python3.11 scripts/export_schemas.py
   ```

3. Confirm nothing drifted:

   ```sh
   PYTHONPATH=src python3.11 scripts/export_schemas.py --check
   ```

   Exit 0 means every file matches; exit 1 lists each missing or differing
   file and writes nothing.

4. Run the consistency tests:

   ```sh
   PYTHONPATH=src python3.11 -m unittest tests.test_schemas_json
   ```

5. Review `git diff schemas/` and commit the Python change with the
   regenerated JSON in the same commit.

Files are written as UTF-8 JSON with sorted keys, two space indentation and
one trailing newline, so the diff shows only real changes. Use
`--output-dir DIR` to write somewhere else, for example a temporary
directory.
