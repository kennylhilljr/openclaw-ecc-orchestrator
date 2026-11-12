"""The JSON Schema documents in schemas/ must agree with the Python validator."""

import json
import pathlib
import sys
import unittest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from openclaw_ecc_orchestrator import schemas  # noqa: E402

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


class GeneratedSchemaTests(unittest.TestCase):
    """schemas.json_schemas() is the source of the committed schemas/ files."""

    def test_generated_keys_match_document_specs(self):
        expected = {spec["file"] for spec in schemas.DOCUMENT_SPECS.values()}
        self.assertEqual(set(schemas.json_schemas()), expected)

    def test_generated_content_matches_committed_files(self):
        for name, generated in schemas.json_schemas().items():
            with self.subTest(file=name):
                committed = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
                self.assertEqual(generated, committed)

    def test_committed_files_are_canonical(self):
        for name, generated in schemas.json_schemas().items():
            with self.subTest(file=name):
                text = (SCHEMA_DIR / name).read_text(encoding="utf-8")
                self.assertEqual(text, json.dumps(generated, indent=2, sort_keys=True,
                                                  ensure_ascii=False) + "\n")

    def test_generated_values_come_from_document_specs(self):
        generated = schemas.json_schemas()
        for doc_type, spec in schemas.DOCUMENT_SPECS.items():
            with self.subTest(doc_type=doc_type):
                data = generated[spec["file"]]
                self.assertEqual(data["required"], spec["required"])
                self.assertEqual(data["properties"]["schema_version"]["const"],
                                 schemas.SCHEMA_VERSION)
                for nested, req in spec.get("nested_required", {}).items():
                    self.assertEqual(_walk(data, nested)["required"], req, nested)
                for dotted, values in spec.get("enums", {}).items():
                    self.assertEqual(_walk(data, dotted)["enum"], values, dotted)

    def test_each_call_returns_fresh_objects(self):
        first = schemas.json_schemas()
        second = schemas.json_schemas()
        self.assertEqual(first, second)
        work_unit = first["work_unit.schema.json"]
        work_unit["required"].append("mutated")
        work_unit["properties"]["risk"]["enum"].append("mutated")
        work_unit["properties"]["id"]["pattern"] = "mutated"
        first.clear()
        third = schemas.json_schemas()
        self.assertEqual(third, second)
        self.assertNotIn("mutated", third["work_unit.schema.json"]["required"])
        self.assertNotIn("mutated", schemas.DOCUMENT_SPECS["work_unit"]["required"])
        self.assertEqual(list(schemas.RISKS),
                         third["work_unit.schema.json"]["properties"]["risk"]["enum"])

    def test_generation_does_not_read_schema_files(self):
        import builtins
        import io

        real_open, real_read_text = builtins.open, pathlib.Path.read_text

        def guard_open(file, *args, **kwargs):
            if str(file).endswith(".schema.json"):
                raise AssertionError("json_schemas read %s" % file)
            return real_open(file, *args, **kwargs)

        def guard_read_text(path, *args, **kwargs):
            if str(path).endswith(".schema.json"):
                raise AssertionError("json_schemas read %s" % path)
            return real_read_text(path, *args, **kwargs)

        from unittest import mock
        with mock.patch.object(builtins, "open", guard_open), \
                mock.patch.object(io, "open", guard_open), \
                mock.patch.object(pathlib.Path, "read_text", guard_read_text):
            self.assertEqual(len(schemas.json_schemas()), 8)


if __name__ == "__main__":
    unittest.main()
