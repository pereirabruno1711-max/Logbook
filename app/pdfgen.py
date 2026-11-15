"""PDF do diário de bordo (pré-visualização ou assinado)."""
import base64
import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Flowable, Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import config
from .utils import from_iso

COL = {"d": colors.HexColor("#2563eb"), "w": colors.HexColor("#f59e0b"), "r": colors.HexColor("#cbd5e1"),
       "-": colors.HexColor("#f1f5f9")}
NAME = {"driving": "Condução", "working": "Trabalho", "resting": "Descanso"}
INK, MUTE, LINE = colors.HexColor("#0f172a"), colors.HexColor("#64748b"), colors.HexColor("#e5e9f0")
GREEN = colors.HexColor(config.BRAND_COLOR if config.BRAND_COLOR.startswith("#") else "#16a34a")


def dur(m):
    m = int(round(m or 0))
    return f"{m // 60}h{m % 60:02d}"


def pt_date(day):
    y, m, d = day.split("-")
    return f"{d}/{m}/{y}"


class Timeline(Flowable):
    """Barra das 24 horas com um retângulo por período."""

    def __init__(self, bar, width, height=16 * mm):
        super().__init__()
        self.bar, self.width, self.height = bar, width, height

    def wrap(self, aw, ah):
        return self.width, self.height

    def draw(self):
        c, n = self.canv, max(1, len(self.bar))
        w = self.width / n
        bar_h = self.height - 6 * mm
        for i, ch in enumerate(self.bar):
            c.setFillColor(COL.get(ch, COL["r"]))
            c.setStrokeColor(COL.get(ch, COL["r"]))
            c.rect(i * w, 6 * mm, w + 0.2, bar_h, stroke=0, fill=1)
        c.setFillColor(MUTE)
        c.setFont("Helvetica", 7)
        for h in range(0, 25, 3):
            x = self.width * h / 24
            c.setStrokeColor(MUTE)
            c.line(x, 5 * mm, x, 6 * mm)
            c.drawCentredString(min(max(x, 4), self.width - 4), 1.8 * mm, f"{h:02d}")


class Watermark:
    def __init__(self, text):
        self.text = text

    def __call__(self, canv, doc):
        canv.saveState()
        canv.setFont("Helvetica-Bold", 60)
        canv.setFillColor(colors.Color(0.8, 0.1, 0.1, alpha=0.10))
        canv.translate(A4[0] / 2, A4[1] / 2)
        canv.rotate(40)
        canv.drawCentredString(0, 0, self.text)
        canv.restoreState()


def render(snap, signature_dataurl, preview=False):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm,
                            bottomMargin=14 * mm, title=f"Diário de bordo {snap['day']}", author=config.APP_NAME)
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=18, leading=22, alignment=0, textColor=INK, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=11.5, leading=14, textColor=INK, spaceBefore=10, spaceAfter=4)
    p = ParagraphStyle("p", parent=ss["BodyText"], fontSize=9, leading=12, textColor=INK)
    small = ParagraphStyle("s", parent=p, fontSize=8, leading=10, textColor=MUTE)
    W = A4[0] - 32 * mm
    co = snap.get("company") or {}
    v = snap.get("vehicle") or {}
    sm = snap["summary"]

    story = [Paragraph("Diário de Bordo", h1),
             Paragraph(f"{co.get('name', '')}" + (f" · NIF {co['nif']}" if co.get("nif") else ""), small), Spacer(1, 6)]

    info = [["Motorista", snap["driver"]["name"], "Data", pt_date(snap["day"])],
            ["Viatura", f"{v.get('plate') or '—'}" + (f" ({v['name']})" if v.get("name") else ""), "Distância GPS", f"{sm['km']:.1f} km".replace(".", ",")],
            ["Conta-km inicial", v.get("odo_start") or "—", "Conta-km final", v.get("odo_end") or "—"]]
    t = Table(info, colWidths=[28 * mm, W / 2 - 28 * mm, 28 * mm, W / 2 - 28 * mm])
    t.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 9), ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 9),
                           ("FONT", (2, 0), (2, -1), "Helvetica-Bold", 9), ("TEXTCOLOR", (0, 0), (-1, -1), INK),
                           ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE), ("TOPPADDING", (0, 0), (-1, -1), 4),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story += [t, Paragraph("Resumo do dia", h2)]

    boxes = [[Paragraph(f"<b>{dur(sm['driving'])}</b><br/><font size=7 color='#64748b'>Condução</font>", p),
              Paragraph(f"<b>{dur(sm['working'])}</b><br/><font size=7 color='#64748b'>Trabalho</font>", p),
              Paragraph(f"<b>{dur(sm['resting'])}</b><br/><font size=7 color='#64748b'>Descanso</font>", p)]]
    bt = Table(boxes, colWidths=[W / 3] * 3)
    bt.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.4, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE),
                            ("LINEABOVE", (0, 0), (0, 0), 2, COL["d"]), ("LINEABOVE", (1, 0), (1, 0), 2, COL["w"]),
                            ("LINEABOVE", (2, 0), (2, 0), 2, COL["r"]), ("TOPPADDING", (0, 0), (-1, -1), 6),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("LEFTPADDING", (0, 0), (-1, -1), 8)]))
    story += [bt, Spacer(1, 8), Timeline(snap["bar"], W)]
    story.append(Paragraph("<font color='#2563eb'><b>Azul</b></font> condução &nbsp;|&nbsp; "
                           "<font color='#d97706'><b>Amarelo</b></font> trabalho &nbsp;|&nbsp; "
                           "<font color='#64748b'><b>Cinzento</b></font> descanso", small))

    story.append(Paragraph("Estados do dia", h2))
    rows = [["Início", "Fim", "Estado", "Duração"]] + [
        [s["start"], s["end"], NAME[s["state"]], dur(s["minutes"])]
        for s in snap["segments"]]
    st = Table(rows, colWidths=[22 * mm, 22 * mm, W - 66 * mm, 22 * mm], repeatRows=1)
    st.setStyle(TableStyle([("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5), ("FONT", (0, 1), (-1, -1), "Helvetica", 8.5),
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f4f6fa")), ("LINEBELOW", (0, 0), (-1, -1), 0.3, LINE),
                            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story.append(st)

    story.append(Paragraph("Viagens", h2))
    if snap["trips"]:
        trows = [["#", "Início", "Fim", "Km", "Origem", "Destino", "Nota"]]
        for tr in snap["trips"]:
            fr, to = tr.get("from") or {}, tr.get("to") or {}
            def place(x):
                return x.get("name") or (f"{x['lat']:.4f}, {x['lon']:.4f}" if x.get("lat") is not None else "—")
            trows.append([str(tr["n"]), tr["start"], tr["end"], f"{tr['km']:.1f}".replace(".", ","),
                          Paragraph(place(fr), small), Paragraph(place(to), small), Paragraph(tr.get("note") or "", small)])
        tt = Table(trows, colWidths=[8 * mm, 15 * mm, 15 * mm, 14 * mm, (W - 52 * mm) * .34, (W - 52 * mm) * .34, (W - 52 * mm) * .32], repeatRows=1)
        tt.setStyle(TableStyle([("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8), ("FONT", (0, 1), (3, -1), "Helvetica", 8),
                                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f4f6fa")), ("LINEBELOW", (0, 0), (-1, -1), 0.3, LINE),
                                ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story.append(tt)
    else:
        story.append(Paragraph("Sem viagens registadas neste dia.", small))
    if v.get("notes"):
        story += [Paragraph("Observações", h2), Paragraph(v["notes"], p)]

    # assinatura
    sig = []
    if signature_dataurl and not preview:
        raw = base64.b64decode(signature_dataurl.split(",", 1)[1])
        ir = ImageReader(io.BytesIO(raw))
        iw, ih = ir.getSize()
        w = 55 * mm
        sig.append(Image(io.BytesIO(raw), width=w, height=w * ih / iw))
        when = from_iso(snap["signed_at"]).astimezone(__import__("zoneinfo").ZoneInfo(config.LOCAL_TZ)).strftime("%d/%m/%Y %H:%M")
        sig.append(Paragraph(f"Assinado por <b>{snap.get('signed_name') or snap['driver']['name']}</b> em {when}", small))
    else:
        sig.append(Paragraph("<b>Pré-visualização</b> — este relatório ainda não está assinado.", small))
    story.append(KeepTogether([Spacer(1, 10), Paragraph("Assinatura do motorista", h2)] + sig))

    def footer(canv, d):
        canv.saveState()
        canv.setFont("Helvetica", 7.5)
        canv.setFillColor(MUTE)
        canv.drawString(16 * mm, 8 * mm, f"{config.APP_NAME} · gerado a {datetime.now().strftime('%d/%m/%Y %H:%M')}")
        canv.drawRightString(A4[0] - 16 * mm, 8 * mm, f"Página {d.page}")
        canv.restoreState()

    wm = Watermark("PRÉ-VISUALIZAÇÃO") if preview else None

    def first(canv, d):
        if wm:
            wm(canv, d)
        footer(canv, d)

    doc.build(story, onFirstPage=first, onLaterPages=first)
    return buf.getvalue()
