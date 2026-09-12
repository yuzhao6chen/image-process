"""不调用外部API的本地规则测试。"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from extract_employee_images import company_match, derive_status, normalize_extraction


class EmployeeExtractionTests(unittest.TestCase):
    def test_status(self):
        self.assertEqual(derive_status((date.today() - timedelta(days=1)).isoformat(), 90), "EXPIRED")
        self.assertEqual(derive_status((date.today() + timedelta(days=10)).isoformat(), 90), "EXPIRING")
        self.assertEqual(derive_status((date.today() + timedelta(days=180)).isoformat(), 90), "VALID")
        self.assertEqual(derive_status(None, 90), "UNKNOWN")

    def test_company_match(self):
        target = "北京城建六建设集团有限公司"
        self.assertEqual(company_match(target, target), "MATCHED")
        self.assertEqual(company_match(None, target), "UNKNOWN")
        self.assertEqual(company_match("其他公司", target), "OTHER_COMPANY")

    def test_normalize_employee_certificate(self):
        raw = {
            "document_type": "employee_certificate",
            "document_title": "一级建造师注册证书",
            "raw_text": "姓名 张三 证书编号 京111",
            "person": {"name": "张三", "company_name": "北京城建六建设集团有限公司"},
            "certificates": [{"cert_name": "一级建造师注册证书", "cert_no": "京111", "expiry_date": "2099年1月1日"}],
            "confidence": 0.95,
            "review_required": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            record = normalize_extraction(raw, Path(tmp) / "a.jpg", "北京城建六建设集团有限公司", 0.8, 90)
        self.assertEqual(record["company_match"], "MATCHED")
        self.assertEqual(record["certificates"][0]["expiry_date"], "2099-01-01")
        self.assertEqual(record["certificates"][0]["status"], "VALID")
        self.assertFalse(record["review_required"])

    def test_normalize_chinese_employee_certificate_type(self):
        raw = {
            "document_type": "员工证书",
            "document_title": "北京市职称证书",
            "raw_text": "姓名 李四 证书编号 ZC001",
            "person": {"name": "李四"},
            "certificates": [{"cert_name": "北京市职称证书", "cert_no": "ZC001"}],
            "confidence": 1.0,
            "review_required": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            record = normalize_extraction(raw, Path(tmp) / "b.jpg", "", 0.8, 90)
        self.assertEqual(record["document_type"], "employee_certificate")
        self.assertEqual(record["certificates"][0]["cert_no"], "ZC001")
        self.assertNotIn("文档类型不受支持", "；".join(record["warnings"]))

    def test_normalize_unknown_alias(self):
        raw = {"document_type": "其他文档", "confidence": 0.9, "review_required": False}
        with tempfile.TemporaryDirectory() as tmp:
            record = normalize_extraction(raw, Path(tmp) / "c.jpg", "", 0.8, 90)
        self.assertEqual(record["document_type"], "unknown")
        self.assertNotIn("文档类型不受支持", "；".join(record["warnings"]))


if __name__ == "__main__":
    unittest.main()
