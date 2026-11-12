import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
SVG_NS = "{http://www.w3.org/2000/svg}"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
DRAWING_TAGS = {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon"}
FORBIDDEN_TAGS = {"script", "foreignObject", "iframe", "object", "embed", "image", "use", "style"}


def local(tag):
    return tag.rsplit("}", 1)[-1]


def readme_lines():
    return README.read_text(encoding="utf-8").splitlines()


def header_image():
    lines = readme_lines()
    for index, line in enumerate(lines):
        match = IMAGE.match(line.strip())
        if match and not line.strip().startswith("[!["):
            return index, match.group(1), match.group(2)
    raise AssertionError("README.md has no header image")


def svg_root():
    _, _, target = header_image()
    return ET.parse(ROOT / target).getroot()


class ReadmeHeaderTest(unittest.TestCase):
    def test_image_sits_between_title_and_badges(self):
        lines = readme_lines()
        index, _, _ = header_image()
        title = next(i for i, line in enumerate(lines) if line.startswith("# "))
        badges = next(i for i, line in enumerate(lines) if line.startswith("[!["))
        self.assertEqual(lines[title], "# OpenClaw ECC Orchestrator")
        self.assertLess(title, index)
        self.assertLess(index, badges)
        self.assertTrue(all(not line.strip() for line in lines[title + 1:index]))

    def test_target_is_local_and_exists(self):
        _, _, target = header_image()
        self.assertEqual(target, "docs/assets/readme-header.svg")
        self.assertNotRegex(target, r"^[a-z]+:")
        self.assertTrue((ROOT / target).is_file())

    def test_alt_text_describes_illustration(self):
        _, alt, _ = header_image()
        self.assertGreaterEqual(len(alt.split()), 8)
        for word in ("OpenClaw", "ECC", "worktree", "review", "merge", "approval"):
            self.assertIn(word.lower(), alt.lower())

    def test_dimensions_and_viewbox(self):
        root = svg_root()
        self.assertEqual(root.tag, SVG_NS + "svg")
        self.assertEqual(root.get("viewBox"), "0 0 1600 480")
        self.assertEqual(root.get("width"), "1600")
        self.assertEqual(root.get("height"), "480")

    def test_accessible_metadata(self):
        root = svg_root()
        self.assertEqual(root.get("role"), "img")
        title = root.find(SVG_NS + "title")
        desc = root.find(SVG_NS + "desc")
        self.assertIsNotNone(title)
        self.assertIsNotNone(desc)
        self.assertEqual(title.text.strip(), "OpenClaw ECC Orchestrator")
        self.assertGreaterEqual(len(desc.text.split()), 15)
        labelled = root.get("aria-labelledby", "").split()
        self.assertEqual(labelled, [title.get("id"), desc.get("id")])

    def test_sources_recorded_in_metadata(self):
        metadata = svg_root().find(SVG_NS + "metadata")
        self.assertIsNotNone(metadata)
        text = "".join(metadata.itertext())
        self.assertIn("https://github.com/kennylhilljr/ecc-customization", text)
        self.assertIn("ECC", text)
        self.assertIn("OpenClaw", text)

    def test_visible_drawing_and_workflow_labels(self):
        root = svg_root()
        shapes = [el for el in root.iter() if local(el.tag) in DRAWING_TAGS]
        self.assertGreaterEqual(len(shapes), 20)
        texts = " ".join(
            "".join(el.itertext()) for el in root.iter() if local(el.tag) == "text"
        )
        self.assertIn("OpenClaw ECC Orchestrator", texts)
        for label in ("ECC", "PLAN", "WORKTREES", "VERIFY", "REVIEW", "MERGE QUEUE",
                      "main", "OPENCLAW", "EVENTS", "APPROVALS"):
            self.assertIn(label, texts)

    def test_no_executable_or_external_content(self):
        root = svg_root()
        for el in root.iter():
            name = local(el.tag)
            self.assertNotIn(name, FORBIDDEN_TAGS)
            for attr, value in el.attrib.items():
                attr_name = local(attr)
                self.assertFalse(attr_name.lower().startswith("on"), (name, attr))
                if attr_name == "href" or attr == XLINK_HREF:
                    self.assertTrue(value.startswith("#"), value)
                for ref in re.findall(r"url\(([^)]*)\)", value):
                    self.assertTrue(ref.strip("'\" ").startswith("#"), ref)
                self.assertNotIn("javascript:", value.lower())
        # URLs may appear only as plain text inside <metadata>.
        raw = (ROOT / header_image()[2]).read_text(encoding="utf-8")
        outside_metadata = re.sub(r"<metadata>.*?</metadata>", "", raw, flags=re.DOTALL)
        for url in re.findall(r"https?://[^\s\"'<)]+", outside_metadata):
            self.assertEqual(url, "http://www.w3.org/2000/svg")
        self.assertNotIn("@import", raw)


if __name__ == "__main__":
    unittest.main()
