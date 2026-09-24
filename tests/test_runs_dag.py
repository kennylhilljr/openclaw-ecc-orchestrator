import unittest

from openclaw_ecc_orchestrator.runs.dag import PlanError, analyze_plan, plan_sha256, validate_plan


def unit(uid, deps=(), files=("src/x.py",)):
    return {
        "id": uid,
        "depends_on": list(deps),
        "scope": {"files": list(files)},
        "acceptance": {"commands": ["python3 -c pass"]},
        "risk": "low",
    }


class DagTests(unittest.TestCase):
    def test_topological_layers(self):
        units = [unit("d", ["b", "c"]), unit("b", ["a"]), unit("c", ["a"]), unit("a")]
        result = analyze_plan(units)
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["layers"], [["a"], ["b", "c"], ["d"]])
        self.assertEqual(result["order"], ["a", "b", "c", "d"])

    def test_accepts_plan_dict(self):
        result = analyze_plan({"units": [unit("a"), unit("b", ["a"])]})
        self.assertEqual(result["layers"], [["a"], ["b"]])

    def test_duplicate_ids(self):
        result = analyze_plan([unit("a"), unit("a")])
        self.assertFalse(result["ok"])
        self.assertIn("duplicate_id", [e["code"] for e in result["errors"]])

    def test_missing_dependency(self):
        result = analyze_plan([unit("a", ["ghost"])])
        self.assertFalse(result["ok"])
        err = [e for e in result["errors"] if e["code"] == "missing_dependency"][0]
        self.assertEqual(err["unit_id"], "a")
        self.assertEqual(err["dependency"], "ghost")

    def test_cycle_reported(self):
        result = analyze_plan([unit("a", ["c"]), unit("b", ["a"]), unit("c", ["b"]), unit("z")])
        self.assertFalse(result["ok"])
        cycle = result["cycle"]
        self.assertEqual(cycle[0], cycle[-1])
        self.assertEqual(set(cycle), {"a", "b", "c"})
        self.assertEqual(result["layers"], [])

    def test_self_dependency_is_cycle(self):
        result = analyze_plan([unit("a", ["a"])])
        self.assertFalse(result["ok"])
        self.assertEqual(result["cycle"], ["a", "a"])

    def test_structural_errors(self):
        result = analyze_plan([{"id": "", "depends_on": []}, {"id": "b", "depends_on": "a"}])
        codes = {e["code"] for e in result["errors"]}
        self.assertIn("invalid_id", codes)
        self.assertIn("invalid_depends_on", codes)

    def test_validate_plan_raises(self):
        with self.assertRaises(PlanError) as ctx:
            validate_plan([unit("a", ["b"]), unit("b", ["a"])])
        self.assertTrue(ctx.exception.errors)
        self.assertEqual(validate_plan([unit("a")])["layers"], [["a"]])

    def test_plan_hash_is_canonical(self):
        a = [{"id": "a", "depends_on": [], "risk": "low", "scope": {"files": ["x"]}, "acceptance": {"commands": []}}]
        b = [{"risk": "low", "acceptance": {"commands": []}, "scope": {"files": ["x"]}, "depends_on": [], "id": "a"}]
        self.assertEqual(plan_sha256(a), plan_sha256(b))
        self.assertEqual(len(plan_sha256(a)), 64)
        b[0]["risk"] = "high"
        self.assertNotEqual(plan_sha256(a), plan_sha256(b))


if __name__ == "__main__":
    unittest.main()
