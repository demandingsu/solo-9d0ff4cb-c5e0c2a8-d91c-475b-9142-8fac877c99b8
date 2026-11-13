"""rowspan/colspan 跨行还原解析测试。"""
import sys
import unittest

sys.path.insert(0, "/tmp/pydeps")
sys.path.insert(0, "/workspace")

from app.parser import parse_detail_html

HTML = """
<html><body>
<p>无关的说明表格：</p>
<table><tr><th>公告号</th><td>2026-001</td></tr></table>
<table>
  <thead>
    <tr>
      <th>样本编号</th><th>采样点</th><th>检测项目</th>
      <th>单位</th><th>检测结果</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td rowspan="2">S100</td>
      <td rowspan="2">一厂出水口</td>
      <td>砷</td><td>mg/L</td><td>0.005</td>
    </tr>
    <tr><td>铅</td><td>μg/L</td><td>&lt;2.5</td></tr>
    <tr>
      <td rowspan="2">S101</td>
      <td rowspan="2">二厂末梢</td>
      <td>浊度</td><td>mg/L</td><td>1.2</td>
    </tr>
    <tr>
      <td colspan="2">砷（复测） mg/L</td><td>0.001</td>
    </tr>
  </tbody>
</table>
</body></html>
"""


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.records = parse_detail_html(HTML)

    def test_row_count(self):
        self.assertEqual(len(self.records), 4)

    def test_rowspan_restored(self):
        r0, r1 = self.records[0], self.records[1]
        self.assertEqual(r0.sample_code, "S100")
        self.assertEqual(r1.sample_code, "S100")
        self.assertEqual(r0.sampling_point, "一厂出水口")
        self.assertEqual(r1.sampling_point, "一厂出水口")
        self.assertEqual(r1.indicator, "铅")
        self.assertEqual(r1.raw_value, "<2.5")

    def test_second_sample(self):
        r2, r3 = self.records[2], self.records[3]
        self.assertEqual(r2.sample_code, "S101")
        self.assertEqual(r3.sample_code, "S101")
        self.assertEqual(r3.sampling_point, "二厂末梢")
        self.assertEqual(r2.indicator, "浊度")

    def test_no_table_raises(self):
        with self.assertRaises(ValueError):
            parse_detail_html("<html><body><p>没有表格</p></body></html>")


if __name__ == "__main__":
    unittest.main()
