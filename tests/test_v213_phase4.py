"""Kiana Vnext Plus — v2.13 阶段 4：xlsx 导出回归"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestXlsxExport:
    def test_export_rows(self, tmp_path):
        from kiana_vnext_plus.enhancements import DataExporter
        rows = [
            {"url": "https://www.a.com/p1", "title": "标题一", "text": "内容" * 30},
            {"url": "https://www.a.com/p2", "title": "标题二", "text": "内容" * 30},
            {"url": "https://www.b.com/p1", "title": "B 站页", "text": "x"},
        ]
        out = tmp_path / "data.xlsx"
        n = DataExporter(tmp_path).export_xlsx(out, rows)
        assert n == 3 and out.exists() and out.stat().st_size > 0
        # 结构验证：汇总页 + 每域 sheet
        from openpyxl import load_workbook
        wb = load_workbook(str(out))
        assert "汇总" in wb.sheetnames
        assert any("a.com" in s for s in wb.sheetnames)
        sheet_a = [s for s in wb.sheetnames if "a.com" in s][0]
        ws = wb[sheet_a]
        assert ws.cell(row=1, column=1).value == "url"       # 表头
        assert ws.max_row == 3                                # 表头 + 2 行
        assert ws.freeze_panes == "A2"

    def test_empty_rows(self, tmp_path):
        from kiana_vnext_plus.enhancements import DataExporter
        assert DataExporter(tmp_path).export_xlsx(tmp_path / "e.xlsx", []) == 0

    def test_bad_data_no_crash(self, tmp_path):
        from kiana_vnext_plus.enhancements import DataExporter
        # 非 dict 行不应崩溃（返回 -1 或 0）
        n = DataExporter(tmp_path).export_xlsx(tmp_path / "b.xlsx", ["garbage", 42])
        assert n in (-1, 0, 2)
