"""生成一份用于自测的模拟对账单 PDF（并非真实银行格式，仅用于验证解析流程）。"""
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

styles = getSampleStyleSheet()
styles["Normal"].fontName = "STSong-Light"

doc = SimpleDocTemplate("samples/sample_statement.pdf", pagesize=A4)
elements = []

elements.append(Paragraph("中国示例银行 个人活期账户对账单", styles["Normal"]))
elements.append(Spacer(1, 8))
elements.append(Paragraph("户名：张三　账号：6222 0212 3456 7890　币种：人民币", styles["Normal"]))
elements.append(Paragraph("账单周期：2024-05-01 至 2024-05-31", styles["Normal"]))
elements.append(Paragraph("期初余额：10000.00　期末余额：12345.67", styles["Normal"]))
elements.append(Spacer(1, 12))

data = [
    ["交易日期", "摘要", "支出金额", "收入金额", "余额"],
    ["2024-05-02", "超市购物-沃尔玛", "350.00", "", "9650.00"],
    ["2024-05-05", "工资入账", "", "8000.00", "17650.00"],
    ["2024-05-08", "信用卡还款", "2000.00", "", "15650.00"],
    ["2024-05-15", "转账-房租", "3000.00", "", "12650.00"],
    ["2024-05-20", "退款-京东", "", "195.67", "12845.67"],
    ["2024-05-28", "水电费", "500.00", "", "12345.67"],
]

table = Table(data, colWidths=[70, 160, 70, 70, 70])
table.setStyle(
    TableStyle(
        [
            ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ]
    )
)
elements.append(table)

doc.build(elements)
print("generated samples/sample_statement.pdf")
