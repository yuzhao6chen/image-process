import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
PARSER_ROOT = ROOT / "tender_parser"
sys.path.insert(0, str(PARSER_ROOT))

from src.fields import EXTENSIONS, RENAMES  # noqa: E402
from src.qualification_types import QUALIFICATION_TYPES  # noqa: E402
from src.qualifications import classify  # noqa: E402
from src.schema import schema  # noqa: E402
from run import has_business_value  # noqa: E402


class TenderParserContract(unittest.TestCase):
    def setUp(self):
        definitions = json.loads(
            (PARSER_ROOT / "resources" / "fields.json").read_text(encoding="utf-8")
        )
        self.names = list(
            dict.fromkeys(
                RENAMES.get(item["field_name"], item["field_name"])
                for item in definitions["fields"]
            )
        ) + EXTENSIONS

    def test_schema_034_has_42_distinct_fields(self):
        self.assertEqual(len(self.names), 42)
        self.assertEqual(len(set(self.names)), 42)
        self.assertNotIn("预审文件获取方式", self.names)
        self.assertNotIn("评分细则", self.names)
        self.assertEqual(schema(self.names)["properties"]["schema_version"]["const"], "0.3.4")

    def test_fixed_qualification_taxonomy(self):
        self.assertEqual(len(QUALIFICATION_TYPES), 13)
        self.assertEqual(
            set(QUALIFICATION_TYPES),
            {
                "BASIC", "CONSORTIUM", "CREDIT", "FINANCIAL",
                "REGIONAL_ACCESS", "AUTHORIZATION", "BANK_ACCOUNT",
                "PERFORMANCE", "PERSONNEL", "PROFESSIONAL_QUALIFICATION",
                "LICENCE", "RELATIONSHIP", "OTHER",
            },
        )

    def test_business_categories_remain_separate(self):
        self.assertEqual(classify("提供安全生产许可证")[0], "LICENCE")
        self.assertEqual(classify("提供基本存款账户信息")[0], "BANK_ACCOUNT")
        self.assertEqual(classify("提供法定代表人授权委托书")[0], "AUTHORIZATION")
        self.assertEqual(classify("项目投融资能力不低于三亿元")[0], "FINANCIAL")

    def test_empty_qualification_container_is_not_a_business_value(self):
        self.assertFalse(
            has_business_value(
                {"field": "资格要求结构化项", "value": {"raw_text": "", "items": []}}
            )
        )
        self.assertTrue(
            has_business_value(
                {"field": "资格要求结构化项", "value": {"raw_text": "明确资格", "items": []}}
            )
        )


if __name__ == "__main__":
    unittest.main()
