from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from io_utils import atomic_write_text


class AtomicWriteCompatibility(unittest.TestCase):
    def test_atomic_write_text_uses_lf_and_replaces_target(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "result.md"
            atomic_write_text(target, "第一行\n第二行\n")
            self.assertEqual(target.read_bytes(), "第一行\n第二行\n".encode("utf-8"))
            atomic_write_text(target, "更新\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "更新\n")


if __name__ == "__main__":
    unittest.main()
