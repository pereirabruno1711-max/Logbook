"""PDF do diário de bordo (pré-visualização ou assinado) — com cabeçalho, cartões de resumo e ícones desenhados em vetor."""
import base64
import io
from datetime import datetime
from zoneinfo import ZoneInfo

from reportlab.graphics.shapes import Circle, Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import BaseDocTemplate, Flowable, Frame, Image, KeepTogether, PageTemplate, Paragraph, Spacer, Table, TableStyle

from . import config
from .utils import from_iso

H = colors.HexColor
NAVY, INK, MUTE, LINE, SOFT = H("#0d2a47"), H("#0f172a"), H("#64748b"), H("#e5e9f0"), H("#f4f6fa")
GREEN = H(config.BRAND_COLOR if config.BRAND_COLOR.startswith("#") else "#16a34a")
STATE = {  # cor forte, tom claro, ícone
    "driving": (H("#2563eb"), H("#e3ecff"), "wheel"), "working": (H("#d97706"), H("#fdf0d8"), "wrench"),
    "availability": (H("#7c3aed"), H("#eee8fd"), "clock"), "resting": (H("#64748b"), H("#e9eef4"), "bed")}
CHAR = {"d": "driving", "w": "working", "a": "availability", "r": "resting"}
NAME = {"driving": "Condução", "working": "Trabalho", "resting": "Descanso", "availability": "Disponibilidade"}
MONTHS = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]
WEEKDAYS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
W = A4[0] - 32 * mm


def dur(m):
    m = int(round(m or 0))
    return f"{m // 60}h{m % 60:02d}"


def pt_date(day):
    y, m, d = (int(x) for x in day.split("-"))
    wd = datetime(y, m, d).weekday()
    return WEEKDAYS[wd], f"{d} de {MONTHS[m - 1]} de {y}"


# ---------- ícones (vetoriais, centrados em cx, cy; h = meio lado) ----------
def icon(c, name, cx, cy, h, color, lw=1.5):
    c.saveState()
    c.setStrokeColor(color)
    c.setFillColor(color)
    c.setLineWidth(lw)
    c.setLineCap(1)
    c.setLineJoin(1)
    if name == "wheel":
        c.circle(cx, cy, h, stroke=1, fill=0)
        c.circle(cx, cy, h * .28, stroke=1, fill=0)
        c.line(cx - h, cy, cx - h * .28, cy)
        c.line(cx + h * .28, cy, cx + h, cy)
        c.line(cx, cy - h * .28, cx, cy - h)
    elif name == "clock":
        c.circle(cx, cy, h, stroke=1, fill=0)
        c.line(cx, cy, cx, cy + h * .55)
        c.line(cx, cy, cx + h * .42, cy - h * .25)
    elif name == "bed":
        c.line(cx - h, cy - h * .7, cx - h, cy + h * .7)
        c.line(cx - h, cy - h * .3, cx + h, cy - h * .3)
        c.line(cx + h, cy - h * .7, cx + h, cy - h * .3)
        c.roundRect(cx - h * .8, cy - h * .3, h * .6, h * .42, 2, stroke=1, fill=0)
        c.line(cx - h * .1, cy - h * .3, cx - h * .1, cy + h * .12)
        c.line(cx - h * .1, cy + h * .12, cx + h, cy + h * .12)
        c.line(cx + h, cy + h * .12, cx + h, cy - h * .3)
    elif name == "wrench":
        c.setLineWidth(lw * 1.9)
        c.line(cx - h * .75, cy - h * .75, cx + h * .1, cy + h * .1)
        c.setLineWidth(lw)
        c.circle(cx + h * .42, cy + h * .42, h * .42, stroke=1, fill=0)
        c.setFillColor(colors.white)
        c.setStrokeColor(colors.white)
        c.rect(cx + h * .3, cy + h * .55, h * .5, h * .3, stroke=0, fill=1)
    elif name == "user":
        c.circle(cx, cy + h * .4, h * .34, stroke=1, fill=0)
        c.arc(cx - h * .8, cy - h * 1.2, cx + h * .8, cy + h * .08, 0, 180)
    elif name == "car":
        c.roundRect(cx - h, cy - h * .4, 2 * h, h * .65, 2, stroke=1, fill=0)
        p = c.beginPath()
        p.moveTo(cx - h * .62, cy + h * .25)
        p.lineTo(cx - h * .36, cy + h * .75)
        p.lineTo(cx + h * .36, cy + h * .75)
        p.lineTo(cx + h * .62, cy + h * .25)
        c.drawPath(p, stroke=1, fill=0)
        for dx in (-.55, .55):
            c.circle(cx + h * dx, cy - h * .42, h * .2, stroke=1, fill=1)
    elif name == "route":
        c.circle(cx - h * .7, cy - h * .55, h * .2, stroke=1, fill=0)
        c.circle(cx + h * .7, cy + h * .55, h * .2, stroke=1, fill=1)
        c.setDash(2, 2)
        c.bezier(cx - h * .5, cy - h * .45, cx + h * .6, cy - h * .5, cx - h * .6, cy + h * .5, cx + h * .5, cy + h * .45)
    elif name == "note":
        c.roundRect(cx - h, cy - h * .65, 2 * h, h * 1.3, 3, stroke=1, fill=0)
        c.line(cx - h * .55, cy + h * .15, cx + h * .55, cy + h * .15)
        c.line(cx - h * .55, cy - h * .25, cx + h * .1, cy - h * .25)
    elif name == "cal":
        c.roundRect(cx - h, cy - h * .85, 2 * h, 1.7 * h, 3, stroke=1, fill=0)
        c.line(cx - h, cy + h * .35, cx + h, cy + h * .35)
        c.line(cx - h * .5, cy + h * .7, cx - h * .5, cy + h * 1.05)
        c.line(cx + h * .5, cy + h * .7, cx + h * .5, cy + h * 1.05)
    elif name == "check":
        c.setLineWidth(lw * 1.6)
        c.line(cx - h * .6, cy, cx - h * .15, cy - h * .45)
        c.line(cx - h * .15, cy - h * .45, cx + h * .65, cy + h * .5)
    elif name == "pen":
        c.line(cx - h * .8, cy - h * .8, cx + h * .5, cy + h * .5)
        c.line(cx + h * .5, cy + h * .5, cx + h * .85, cy + h * .85)
        c.line(cx - h * .8, cy - h * .8, cx - h * .85, cy - h * .35)
        c.line(cx - h * .8, cy - h * .8, cx - h * .35, cy - h * .85)
    c.restoreState()


class Section(Flowable):
    """Título de secção com ícone dentro de um círculo e linha de separação."""

    def __init__(self, ic, text):
        super().__init__()
        self.ic, self.text, self.width, self.height = ic, text, W, 9 * mm

    def wrap(self, aw, ah):
        return self.width, self.height

    def draw(self):
        c = self.canv
        c.setFillColor(SOFT)
        c.circle(4 * mm, 4.2 * mm, 3.6 * mm, stroke=0, fill=1)
        icon(c, self.ic, 4 * mm, 4.2 * mm, 2.1 * mm, NAVY, 1.1)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 11.5)
        c.drawString(10 * mm, 3 * mm, self.text)
        c.setStrokeColor(LINE)
        c.setLineWidth(.6)
        c.line(0, .2 * mm, self.width, .2 * mm)


class CardRow(Flowable):
    """Fila de cartões (ícone num círculo, rótulo, valor grande e linha de apoio)."""

    def __init__(self, cards, height=22 * mm, gap=3 * mm):
        super().__init__()
        self.cards, self.height, self.gap, self.width = cards, height, gap, W

    def wrap(self, aw, ah):
        return self.width, self.height

    def draw(self):
        c, n = self.canv, len(self.cards)
        cw = (self.width - self.gap * (n - 1)) / n
        for i, k in enumerate(self.cards):
            x, strong, tint = i * (cw + self.gap), k["color"], k["tint"]
            c.setFillColor(colors.white)
            c.setStrokeColor(LINE)
            c.setLineWidth(.7)
            c.roundRect(x, 0, cw, self.height, 3 * mm, stroke=1, fill=1)
            c.setFillColor(strong)
            c.roundRect(x + 3 * mm, self.height - 1.4 * mm, cw - 6 * mm, 1.4 * mm, .7 * mm, stroke=0, fill=1)
            c.setFillColor(tint)
            c.circle(x + 7.5 * mm, self.height / 2 - .5 * mm, 4.6 * mm, stroke=0, fill=1)
            icon(c, k["icon"], x + 7.5 * mm, self.height / 2 - .5 * mm, 2.7 * mm, strong, 1.3)
            tx = x + 14 * mm
            c.setFillColor(MUTE)
            c.setFont("Helvetica", 7.5)
            c.drawString(tx, self.height - 6.2 * mm, k["label"].upper())
            size = 15
            while size > 8 and c.stringWidth(k["value"], "Helvetica-Bold", size) > cw - 16 * mm:
                size -= 1
            c.setFillColor(INK)
            c.setFont("Helvetica-Bold", size)
            c.drawString(tx, self.height / 2 - 3.2 * mm, k["value"])
            c.setFillColor(MUTE)
            c.setFont("Helvetica", 7.5)
            c.drawString(tx, 3.4 * mm, k.get("sub", ""))


class Timeline(Flowable):
    """Barra das 24 horas (um retângulo por período) com horas e legenda."""

    def __init__(self, bar, height=24 * mm):
        super().__init__()
        self.bar, self.width, self.height = bar, W, height

    def wrap(self, aw, ah):
        return self.width, self.height

    def draw(self):
        c, n = self.canv, max(1, len(self.bar))
        top, bh = self.height, 9 * mm
        y = top - bh - 1 * mm
        c.saveState()
        p = c.beginPath()
        p.roundRect(0, y, self.width, bh, 2 * mm)
        c.clipPath(p, stroke=0, fill=0)
        w = self.width / n
        for i, ch in enumerate(self.bar):
            if ch == "-":
                c.setFillColor(H("#f1f5f9"))
            else:
                c.setFillColor(STATE[CHAR.get(ch, "resting")][0])
            c.rect(i * w, y, w + .25, bh, stroke=0, fill=1)
        c.restoreState()
        c.setFont("Helvetica", 7)
        c.setStrokeColor(MUTE)
        c.setLineWidth(.5)
        for h in range(0, 25, 3):
            x = self.width * h / 24
            c.line(x, y - .5 * mm, x, y - 1.8 * mm)
            c.setFillColor(MUTE)
            c.drawCentredString(min(max(x, 3), self.width - 3), y - 5 * mm, f"{h:02d}h")
        x = 0
        for k in ("driving", "working", "availability", "resting"):
            c.setFillColor(STATE[k][0])
            c.roundRect(x, 1 * mm, 3 * mm, 3 * mm, .8 * mm, stroke=0, fill=1)
            c.setFillColor(INK)
            c.setFont("Helvetica", 8)
            c.drawString(x + 4.5 * mm, 1.5 * mm, NAME[k])
            x += 6 * mm + c.stringWidth(NAME[k], "Helvetica", 8) + 5 * mm


def dot(color, d=3.2 * mm):
    dr = Drawing(d + 2, d + 2)
    dr.add(Circle((d + 2) / 2, (d + 2) / 2, d / 2, fillColor=color, strokeColor=color))
    return dr


def header(canv, snap, label, signed):
    """Faixa de topo (só na primeira página): marca, empresa, data e estado do relatório."""
    pw, ph = A4
    canv.saveState()
    canv.setFillColor(NAVY)
    canv.rect(0, ph - 36 * mm, pw, 36 * mm, stroke=0, fill=1)
    canv.setFillColor(GREEN)
    canv.rect(0, ph - 37.4 * mm, pw, 1.4 * mm, stroke=0, fill=1)
    # marca
    canv.setFillColor(GREEN)
    canv.roundRect(16 * mm, ph - 24 * mm, 13 * mm, 13 * mm, 3 * mm, stroke=0, fill=1)
    icon(canv, "wheel", 22.5 * mm, ph - 17.5 * mm, 4.3 * mm, colors.white, 1.7)
    canv.setFillColor(colors.white)
    canv.setFont("Helvetica-Bold", 19)
    canv.drawString(33 * mm, ph - 18 * mm, "Diário de Bordo")
    co = snap.get("company") or {}
    canv.setFillColor(H("#a9bdd3"))
    canv.setFont("Helvetica", 9.5)
    canv.drawString(33 * mm, ph - 24 * mm, co.get("name", "") + (f"   ·   NIF {co['nif']}" if co.get("nif") else ""))
    # data
    wd, long = pt_date(snap["day"])
    icon(canv, "cal", pw - 16 * mm - 3 * mm, ph - 14.5 * mm, 2.6 * mm, H("#a9bdd3"), 1.1)
    canv.setFillColor(H("#a9bdd3"))
    canv.setFont("Helvetica", 9)
    canv.drawRightString(pw - 16 * mm - 8 * mm, ph - 14 * mm, wd.capitalize())
    canv.setFillColor(colors.white)
    canv.setFont("Helvetica-Bold", 13)
    canv.drawRightString(pw - 16 * mm, ph - 21 * mm, long)
    # selo de estado
    text = "ASSINADO" if signed else (label or "PRÉ-VISUALIZAÇÃO")
    bg = GREEN if signed else H("#f59e0b")
    fw = canv.stringWidth(text, "Helvetica-Bold", 8) + (11 if signed else 6) * mm
    x0 = pw - 16 * mm - fw
    canv.setFillColor(bg)
    canv.roundRect(x0, ph - 31.5 * mm, fw, 5.6 * mm, 2.8 * mm, stroke=0, fill=1)
    canv.setFillColor(colors.white)
    canv.setFont("Helvetica-Bold", 8)
    if signed:
        icon(canv, "check", x0 + 4 * mm, ph - 28.7 * mm, 1.7 * mm, colors.white, 1.1)
        canv.drawString(x0 + 7.2 * mm, ph - 29.3 * mm, text)
    else:
        canv.drawCentredString(x0 + fw / 2, ph - 29.3 * mm, text)
    canv.restoreState()


def render(snap, signature_dataurl, preview=False, label=None):
    buf = io.BytesIO()
    signed = bool(signature_dataurl) and not preview
    doc = BaseDocTemplate(buf, pagesize=A4, title=f"Diário de bordo {snap['day']}", author=config.APP_NAME,
                          leftMargin=16 * mm, rightMargin=16 * mm, topMargin=16 * mm, bottomMargin=16 * mm)
    ss = getSampleStyleSheet()
    p = ParagraphStyle("p", parent=ss["BodyText"], fontSize=9, leading=12, textColor=INK)
    small = ParagraphStyle("s", parent=p, fontSize=8, leading=10, textColor=MUTE)
    bold = ParagraphStyle("b", parent=p, fontName="Helvetica-Bold")
    v, sm = snap.get("vehicle") or {}, snap["summary"]
    total = max(1, sm.get("total") or 1)

    story = []
    # --- motorista, viatura, distância ---
    n_trips = len(snap["trips"])
    odo = ""
    if v.get("odo_start") or v.get("odo_end"):
        odo = f"Conta-km {v.get('odo_start') or '—'} › {v.get('odo_end') or '—'}"
    story.append(CardRow([
        {"icon": "user", "color": NAVY, "tint": SOFT, "label": "Motorista", "value": snap["driver"]["name"], "sub": "Registo do dia"},
        {"icon": "car", "color": NAVY, "tint": SOFT, "label": "Viatura", "value": v.get("plate") or "—", "sub": (v.get("name") or odo or "")[:34]},
        {"icon": "route", "color": GREEN, "tint": H("#e8f7ee"), "label": "Distância GPS", "value": f"{sm['km']:.1f} km".replace(".", ","),
         "sub": f"{n_trips} viage{'m' if n_trips == 1 else 'ns'}"}], height=21 * mm))
    story.append(Spacer(1, 5 * mm))

    # --- resumo ---
    story.append(Section("clock", "Resumo do dia"))
    story.append(Spacer(1, 3 * mm))
    story.append(CardRow([{"icon": STATE[k][2], "color": STATE[k][0], "tint": STATE[k][1], "label": NAME[k], "value": dur(sm.get(k, 0)),
                           "sub": f"{round(100 * sm.get(k, 0) / total)}% do dia"} for k in ("driving", "working", "availability", "resting")],
                          height=22 * mm, gap=2.5 * mm))
    story += [Spacer(1, 5 * mm), Timeline(snap["bar"]), Spacer(1, 3 * mm)]

    # --- estados ---
    story.append(Section("clock", "Estados do dia"))
    rows = [["", "Estado", "Início", "Fim", "Duração"]]
    for s in snap["segments"]:
        rows.append([dot(STATE[s["state"]][0]), Paragraph(f"<b>{NAME[s['state']]}</b>" + (" <font color='#94a3b8' size=7>GPS</font>" if s.get("locked") else ""), p),
                     s["start"], s["end"], dur(s["minutes"])])
    t = Table(rows, colWidths=[8 * mm, W - 8 * mm - 66 * mm, 22 * mm, 22 * mm, 22 * mm], repeatRows=1)
    t.setStyle(TableStyle([("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8), ("TEXTCOLOR", (0, 0), (-1, 0), MUTE),
                           ("FONT", (2, 1), (-1, -1), "Helvetica", 9), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SOFT]),
                           ("LINEBELOW", (0, 0), (-1, 0), .8, LINE), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                           ("TOPPADDING", (0, 0), (-1, -1), 2.4), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.4),
                           ("ALIGN", (4, 0), (4, -1), "RIGHT")]))
    story += [Spacer(1, 1 * mm), t, Spacer(1, 3 * mm)]

    # --- viagens ---
    story.append(Section("route", "Viagens"))
    story.append(Spacer(1, 2 * mm))
    if snap["trips"]:
        def place(x):
            return x.get("name") or (f"{x['lat']:.4f}, {x['lon']:.4f}" if x.get("lat") is not None else "—")
        trows = [["#", "Horário", "Km", "Origem", "Destino", "Nota"]]
        for tr in snap["trips"]:
            fr, to = tr.get("from") or {}, tr.get("to") or {}
            trows.append([str(tr["n"]), f"{tr['start']} – {tr['end']}", f"{tr['km']:.1f}".replace(".", ","),
                          Paragraph(place(fr), small), Paragraph(place(to), small), Paragraph(tr.get("note") or "", small)])
        cw = W - 8 * mm - 29 * mm - 14 * mm
        tt = Table(trows, colWidths=[8 * mm, 29 * mm, 14 * mm, cw * .3, cw * .3, cw * .4], repeatRows=1)
        tt.setStyle(TableStyle([("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8), ("TEXTCOLOR", (0, 0), (-1, 0), MUTE),
                                ("FONT", (0, 1), (2, -1), "Helvetica", 8.5), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SOFT]),
                                ("LINEBELOW", (0, 0), (-1, 0), .8, LINE), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                ("TOPPADDING", (0, 0), (-1, -1), 2.4), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.4),
                                ("ALIGN", (2, 0), (2, -1), "RIGHT")]))
        story.append(tt)
    else:
        story.append(Paragraph("Sem viagens registadas neste dia.", small))
    story.append(Spacer(1, 3 * mm))

    # --- notas e observações ---
    if snap.get("notes") or v.get("notes"):
        story += [Section("note", "Notas"), Spacer(1, 2 * mm)]
        for n in snap.get("notes") or []:
            story.append(Paragraph(f"<font color='#0d2a47'><b>{n['time']}</b></font> &nbsp; {n['text']}", p))
        if v.get("notes"):
            story.append(Paragraph(f"<font color='#64748b'>Observações:</font> {v['notes']}", p))
        story.append(Spacer(1, 4 * mm))

    # --- assinatura ---
    sig = [Section("pen", "Assinatura do motorista"), Spacer(1, 3 * mm)]
    if signed:
        raw = base64.b64decode(signature_dataurl.split(",", 1)[1])
        iw, ih = ImageReader(io.BytesIO(raw)).getSize()
        w = 44 * mm
        when = from_iso(snap["signed_at"]).astimezone(ZoneInfo(config.LOCAL_TZ)).strftime("%d/%m/%Y às %H:%M")
        box = Table([[Image(io.BytesIO(raw), width=w, height=w * ih / iw),
                      Paragraph(f"Assinado digitalmente por<br/><b>{snap.get('signed_name') or snap['driver']['name']}</b><br/>"
                                f"<font color='#64748b'>{when}</font>", p)]], colWidths=[54 * mm, W - 54 * mm])
        box.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), .7, LINE), ("ROUNDEDCORNERS", [4, 4, 4, 4]), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                 ("BACKGROUND", (0, 0), (-1, -1), SOFT), ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 5),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        sig.append(box)
    else:
        sig.append(Paragraph("Este relatório ainda não está assinado.", small))
    story.append(KeepTogether(sig))

    def footer(canv, d):
        canv.saveState()
        canv.setStrokeColor(LINE)
        canv.setLineWidth(.6)
        canv.line(16 * mm, 12.5 * mm, A4[0] - 16 * mm, 12.5 * mm)
        canv.setFont("Helvetica", 7.5)
        canv.setFillColor(MUTE)
        canv.drawString(16 * mm, 8.5 * mm, f"{config.APP_NAME}  ·  gerado a {datetime.now().strftime('%d/%m/%Y %H:%M')}")
        canv.drawRightString(A4[0] - 16 * mm, 8.5 * mm, f"Página {d.page}")
        canv.restoreState()

    def wm(canv):
        if preview:
            canv.saveState()
            canv.setFont("Helvetica-Bold", 56)
            canv.setFillColor(colors.Color(.85, .1, .1, alpha=.08))
            canv.translate(A4[0] / 2, A4[1] / 2 - 10 * mm)
            canv.rotate(40)
            canv.drawCentredString(0, 0, label or "PRÉ-VISUALIZAÇÃO")
            canv.restoreState()

    def first(canv, d):
        header(canv, snap, label, signed)
        footer(canv, d)
        wm(canv)

    def later(canv, d):
        footer(canv, d)
        wm(canv)

    pad = dict(leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    f1 = Frame(16 * mm, 16 * mm, W, A4[1] - 45 * mm - 16 * mm, id="f1", **pad)
    f2 = Frame(16 * mm, 16 * mm, W, A4[1] - 32 * mm, id="f2", **pad)
    doc.addPageTemplates([PageTemplate(id="first", frames=[f1], onPage=first, autoNextPageTemplate="later"),
                          PageTemplate(id="later", frames=[f2], onPage=later)])
    doc.build(story)
    return buf.getvalue()
