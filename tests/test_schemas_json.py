"""The JSON Schema documents in schemas/ must agree with the Python validator."""

import json
import pathlib
import unittest

from openclaw_ecc_orchestrator import schemas

SCHEMA_DIR = pathlib.Path(__file__).resolve().parents[1] / "schemas"


def _walk(schema, dotted):
    """Resolve 'a.b[].c' through properties/items of a JSON Schema."""
    node = schema
    for part in dotted.split("."):
        is_array = part.endswith("[]")
        name = part[:-2] if is_array else part
        node = node["properties"][name]
        if is_array:
            node = node["items"]
    return node


class JsonSchemaConsistencyTests(unittest.TestCase):
    def test_every_document_has_a_schema_file(self):
        names = {spec["file"] for spec in schemas.DOCUMENT_SPECS.values()}
        self.assertEqual(len(names), 8)
        for name in names:
            self.assertTrue((SCHEMA_DIR / name).is_file(), name)

    def test_schema_files_are_draft_2020_12(self):
        for doc_type, spec in schemas.DOCUMENT_SPECS.items():
            with self.subTest(doc_type=doc_type):
                data = json.loads((SCHEMA_DIR / spec["file"]).read_text(encoding="utf-8"))
                self.assertEqual(data["$schema"], "https://json-schema.org/draft/2020-12/schema")
                self.assertEqual(data["type"], "object")
                self.assertEqual(data["properties"]["schema_version"]["const"],
                                 schemas.SCHEMA_VERSION)

    def test_required_lists_match(self):
        for doc_type, spec in schemas.DOCUMENT_SPECS.items():
            with self.subTest(doc_type=doc_type):
                data = json.loads((SCHEMA_DIR / spec["file"]).read_text(encoding="utf-8"))
                self.assertEqual(sorted(data["required"]), sorted(spec["required"]))
                for nested, req in spec.get("nested_required", {}).items():
                    self.assertEqual(sorted(_walk(data, nested)["required"]), sorted(req),
                                     nested)

    def test_enum_values_match(self):
        for doc_type, spec in schemas.DOCUMENT_SPECS.items():
            for dotted, values in spec.get("enums", {}).items():
                with self.subTest(doc_type=doc_type, field=dotted):
                    data = json.loads((SCHEMA_DIR / spec["file"]).read_text(encoding="utf-8"))
                    self.assertEqual(sorted(_walk(data, dotted)["enum"]), sorted(values))

    def test_id_pattern_matches(self):
        data = json.loads((SCHEMA_DIR / "work_unit.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(data["properties"]["id"]["pattern"], schemas.ID_PATTERN)


if __name__ == "__main__":
    unittest.main()
