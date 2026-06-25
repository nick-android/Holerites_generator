import streamlit as st
import pandas as pd
import pdfplumber
import datetime
import re
import calendar
from io import BytesIO
import zipfile
from fpdf import FPDF
import numpy as np

# ==========================================
# CONFIGURACIÓN UI
# ==========================================
st.set_page_config(
    page_title="Nómina y Holerites — Wiegand Brito",
    layout="wide"
)
st.title("💸 Sistema Automatizado de Nómina y Holerites")
st.markdown(
    "Procesa sueldos, calcula horas extras, faltas y feriados "
    "bajo la ley laboral paraguaya. Emite recibos en Gs."
)

# ==========================================
# CONSTANTES LABORALES
# ==========================================
MINUTOS_JORNADA = 8 * 60
DIAS_MES        = 30
TASA_IPS        = 0.09
TOLERANCIA_MIN  = 5

ABSENCE_DEDUCTION_DAYS = {0: 0, 1: 1, 2: 4, 3: 5, 4: 6, 5: 7}

# ==========================================
# UTILIDADES
# ==========================================
def format_gs(monto):
    if not monto or monto <= 0:
        return ""
    return "Gs. " + f"{int(round(monto)):,}".replace(",", ".")


def calcular_dias_habiles(fecha_inicio, fecha_fin, feriados=None):
    holidays = []
    if feriados:
        holidays = [np.datetime64(f, "D") for f in feriados]
    fi = np.datetime64(fecha_inicio, "D")
    ff = np.datetime64(fecha_fin, "D")
    return int(np.busday_count(fi, ff + np.timedelta64(1, "D"),
                               holidays=holidays))


# ==========================================
# EXTRACCIÓN DE TEXTO
# Devuelve UN TOKEN POR LÍNEA.
# Maneja dos casos:
#   A) PDF de texto plano: cada fila ya viene en línea separada
#   B) PDF de tabla colapsada: "111 Ignacio Venialgo 5083111 Gs.7.000.000"
#      → se expande a un token por línea
# ==========================================
def extract_text_from_pdf(pdf_file):
    raw_lines = []
    with pdfplumber.open(pdf_file) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                for line in t.split("\n"):
                    line = line.strip()
                    if line:
                        raw_lines.append(line)

    # Expandir líneas que contienen un salario embebido
    # Ej: "111 Ignacio Venialgo Figueredo 5083111 Gs.7.000.000"
    # → ["111", "Ignacio Venialgo Figueredo", "5083111", "Gs.7.000.000"]
    salary_inline = re.compile(
        r'^(\d+)\s+(.+?)\s+(\d{6,12})\s+(Gs\.[\d.]+)\s*$', re.IGNORECASE)
    salary_inline_noci = re.compile(
        r'^(\d+)\s+(.+?)\s+(Gs\.[\d.]+)\s*$', re.IGNORECASE)

    # Patrón para detectar línea biométrica colapsada:
    # "26 luis alberto montador steel 01/06/2026 07:05:00 1"
    bio_inline = re.compile(
        r'^(\d+)\s+(.+?)\s+(montador\s+\w+|administrativo)\s+'
        r'(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})\s+\d+\s*$',
        re.IGNORECASE)

    tokens = []
    for line in raw_lines:
        # Intentar expandir línea de funcionario con CI
        m = salary_inline.match(line)
        if m:
            tokens.append(m.group(1))        # ID
            tokens.append(m.group(2).strip()) # Nombre
            tokens.append(m.group(3))        # CI
            tokens.append(m.group(4))        # Gs.X
            continue

        # Intentar expandir línea de funcionario sin CI
        m2 = salary_inline_noci.match(line)
        if m2:
            tokens.append(m2.group(1))        # ID
            tokens.append(m2.group(2).strip()) # Nombre
            tokens.append(m2.group(3))        # Gs.X
            continue

        # Intentar expandir línea biométrica
        m3 = bio_inline.match(line)
        if m3:
            tokens.append(f"{m3.group(1)} {m3.group(2).strip()}")  # "ID nombre"
            tokens.append(m3.group(3).strip())                      # depto
            tokens.append(m3.group(4).strip())                      # timestamp
            tokens.append("1")                                       # dispositivo
            continue

        # Línea normal: agregar tal cual
        tokens.append(line)

    return "\n".join(tokens)


# ==========================================
# PARSER — FUNCIONARIOS Y SUELDOS
#
# Después de extract_text_from_pdf, el texto queda
# normalizado a un token por línea:
#
#   111
#   Ignacio Venialgo Figueredo
#   5083111
#   Gs.7.000.000   ← ancla
#   105
#   Ricardo Perelló
#   Gs.11.000.000  ← ancla (sin CI)
# ==========================================
def parse_funcionarios(text):
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    HEADERS = {
        "id.", "nome", "c.i.", "salarios", "id", "nombre", "ci",
        "nombre y apellido", "funcionarios y sueldos",
        "funcionários y sueldos", "salario", "depart.",
        "tiempo", "id del dispositivo"
    }

    salary_pat = re.compile(r'^[Gg][Ss]\.?\s*([\d.]+)$')
    int_pat    = re.compile(r'^\d+$')

    funcionarios = []
    seen_ids     = set()

    for idx, line in enumerate(lines):
        m = salary_pat.match(line)
        if not m:
            continue

        raw = m.group(1).replace(".", "")
        try:
            salary = float(raw)
        except ValueError:
            continue
        if salary < 500_000:
            continue

        # Recolectar hasta 6 líneas previas ignorando headers
        preceding = []
        j = idx - 1
        while j >= 0 and len(preceding) < 6:
            c = lines[j].strip()
            if c and c.lower() not in HEADERS:
                preceding.insert(0, c)
            j -= 1

        if not preceding:
            continue

        emp_id = None
        ci     = ""

        # ¿El último token es una CI? (número puro, 6-12 dígitos, ≥ 100.000)
        last = preceding[-1]
        if (int_pat.match(last)
                and 6 <= len(last) <= 12
                and int(last) >= 100_000):
            ci        = last
            remaining = preceding[:-1]
        else:
            remaining = preceding[:]

        if not remaining:
            continue

        # Buscar ID (número pequeño ≤ 5 dígitos) de derecha a izquierda
        name_parts = []
        found_id   = False
        for k in range(len(remaining) - 1, -1, -1):
            tok = remaining[k]
            if int_pat.match(tok) and int(tok) < 100_000 and len(tok) <= 5:
                emp_id     = int(tok)
                name_parts = remaining[k + 1:]
                found_id   = True
                break
            else:
                name_parts.insert(0, tok)

        if not found_id:
            continue

        nombre = " ".join(name_parts).strip()
        if not nombre:
            continue

        if emp_id not in seen_ids:
            seen_ids.add(emp_id)
            funcionarios.append({
                "ID":                 emp_id,
                "NOMBRE Y APELLIDO":  nombre,
                "CI Nº":              ci,
                "SALARIO REAJUSTADO": salary,
            })

    return pd.DataFrame(funcionarios)


# ==========================================
# PARSER — BOOK (BIOMÉTRICO)
#
# Después de extract_text_from_pdf, el texto queda:
#
#   26 luis alberto        ← "ID nombre_parcial"
#   montador steel         ← departamento
#   01/06/2026 07:05:00    ← timestamp  ← ANCLA
#   1                      ← dispositivo
# ==========================================
def parse_biometrico(text, df_funcionarios):
    if df_funcionarios is None or df_funcionarios.empty:
        return pd.DataFrame()

    id_to_nombre = dict(zip(
        df_funcionarios["ID"],
        df_funcionarios["NOMBRE Y APELLIDO"]
    ))

    lines = [l.strip() for l in text.split("\n") if l.strip()]

    SKIP_H = {
        "id.", "nombre", "depart.", "tiempo", "id del dispositivo",
        "book", "reporte", "fecha", "hora", "1", "id",
        "departamento", "nome", "c.i.", "salarios", "salario",
        "funcionarios y sueldos", "funcionários y sueldos"
    }
    clean = [l for l in lines if l.lower() not in SKIP_H]

    ts_pat      = re.compile(r'^\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}$')
    id_name_pat = re.compile(r'^(\d+)\s+(.+)$')

    marcaciones = []

    for i, line in enumerate(clean):
        if not ts_pat.match(line):
            continue
        try:
            fecha_hora = pd.to_datetime(line, format="%d/%m/%Y %H:%M:%S")
        except Exception:
            continue

        dept_line = clean[i - 1].lower() if i >= 1 else ""
        name_line = clean[i - 2]         if i >= 2 else ""

        if   "drywall" in dept_line: cargo = "Montador DryWall"
        elif "steel"   in dept_line: cargo = "Montador Steel Frame"
        elif "admin"   in dept_line: cargo = "Administrativo"
        else:                        cargo = "Funcionario"

        m = id_name_pat.match(name_line)
        if not m:
            continue

        cid = int(m.group(1))
        if cid not in id_to_nombre:
            continue

        marcaciones.append({
            "ID":     cid,
            "Nomb.":  id_to_nombre[cid],
            "Tiempo": fecha_hora,
            "Cargo":  cargo,
        })

    return pd.DataFrame(marcaciones)


# ==========================================
# HELPER — MINUTOS NOCTURNOS (22:00–06:00)
# ==========================================
def _minutos_nocturnos(entrada: pd.Timestamp, salida: pd.Timestamp) -> int:
    if salida <= entrada:
        return 0
    total = 0
    dia = entrada.normalize()
    while dia < salida:
        sig = dia + pd.Timedelta(days=1)
        ov_s = max(entrada, dia)
        ov_e = min(salida,  dia + pd.Timedelta(hours=6))
        if ov_e > ov_s:
            total += int((ov_e - ov_s).total_seconds() / 60)
        ov_s = max(entrada, dia + pd.Timedelta(hours=22))
        ov_e = min(salida,  sig)
        if ov_e > ov_s:
            total += int((ov_e - ov_s).total_seconds() / 60)
        dia = sig
    return total


# ==========================================
# CLASIFICACIÓN DE MINUTOS POR DÍA
# ==========================================
def clasificar_minutos(marcaciones_dia, weekday, es_feriado=False):
    punches = sorted(marcaciones_dia)
    if len(punches) % 2 != 0:
        punches = punches[:-1]
    if not punches:
        return 0, 0, 0, 0, 0, 0

    pairs     = []
    total_min = 0
    for i in range(0, len(punches), 2):
        entrada = punches[i]
        salida  = punches[i + 1]
        delta   = int((salida - entrada).total_seconds() / 60)
        if delta > 0:
            total_min += delta
            pairs.append((entrada, salida))

    atraso_min   = 0
    ot_dia_min   = 0
    ot_noche_min = 0
    bonus50_min  = 0
    bonus100_min = 0

    if es_feriado or weekday == 6:
        bonus100_min = total_min

    elif weekday == 5:
        for (entrada, salida) in pairs:
            mediodia = entrada.replace(hour=12, minute=0,
                                       second=0, microsecond=0)
            ov_e = min(salida, mediodia)
            if ov_e > entrada:
                bonus50_min += int((ov_e - entrada).total_seconds() / 60)
            ov_s = max(entrada, mediodia)
            if salida > ov_s:
                bonus100_min += int((salida - ov_s).total_seconds() / 60)

    else:
        if total_min < MINUTOS_JORNADA:
            diff = MINUTOS_JORNADA - total_min
            atraso_min = 0 if diff < TOLERANCIA_MIN else diff
        elif total_min > MINUTOS_JORNADA:
            overtime = total_min - MINUTOS_JORNADA
            rem_ot   = overtime
            for (entrada, salida) in reversed(pairs):
                if rem_ot <= 0:
                    break
                par_dur     = int((salida - entrada).total_seconds() / 60)
                ot_este_par = min(rem_ot, par_dur)
                ot_desde    = salida - pd.Timedelta(minutes=ot_este_par)
                noc = min(_minutos_nocturnos(ot_desde, salida), ot_este_par)
                ot_noche_min += noc
                ot_dia_min   += ot_este_par - noc
                rem_ot -= ot_este_par

    return total_min, atraso_min, ot_dia_min, ot_noche_min, bonus50_min, bonus100_min


# ==========================================
# CÁLCULO DE NÓMINA
# ==========================================
def calcular_nomina(df_func, df_bio, fecha_inicio, fecha_fin,
                    periodo_tipo, feriados=None):
    if df_bio.empty:
        return pd.DataFrame()

    feriados     = feriados or []
    feriados_set = set(feriados)

    if isinstance(fecha_inicio, datetime.datetime):
        fecha_inicio = fecha_inicio.date()
    if isinstance(fecha_fin, datetime.datetime):
        fecha_fin = fecha_fin.date()

    dias_calendario = (fecha_fin - fecha_inicio).days + 1
    dias_hab        = max(calcular_dias_habiles(
                          fecha_inicio, fecha_fin, feriados), 1)
    dict_cargos     = df_bio.groupby("Nomb.")["Cargo"].first().to_dict()

    df_bio_p = df_bio[
        (df_bio["Tiempo"].dt.date >= fecha_inicio) &
        (df_bio["Tiempo"].dt.date <= fecha_fin)
    ].copy()

    resultados = []

    for _, emp in df_func.iterrows():
        nombre       = emp["NOMBRE Y APELLIDO"]
        salario_base = emp["SALARIO REAJUSTADO"]
        ci           = emp["CI Nº"]
        cargo        = dict_cargos.get(nombre, "Funcionario")

        val_dia    = salario_base / DIAS_MES
        val_minuto = val_dia / MINUTOS_JORNADA

        if periodo_tipo == "Semanal":
            sal_prop = round(salario_base * 7 / DIAS_MES)
        else:
            sal_prop = round(salario_base * dias_calendario / DIAS_MES)

        marcs_emp = df_bio_p[df_bio_p["Nomb."] == nombre]

        total_atraso        = 0
        total_ot_dia        = 0
        total_ot_noche      = 0
        total_bonus50       = 0
        total_bonus100      = 0
        feriados_trabajados = 0
        dias_hab_trabajados = {}

        if not marcs_emp.empty:
            for fecha in sorted(marcs_emp["Tiempo"].dt.date.unique()):
                punches_dia = (
                    marcs_emp[marcs_emp["Tiempo"].dt.date == fecha]
                    ["Tiempo"].sort_values().tolist()
                )
                es_feriado = fecha in feriados_set
                wd = pd.Timestamp(fecha).weekday()

                total_min, atraso, ot_dia, ot_noche, b50, b100 = \
                    clasificar_minutos(punches_dia, wd, es_feriado)

                if es_feriado and total_min > 0:
                    feriados_trabajados += 1

                total_atraso   += atraso
                total_ot_dia   += ot_dia
                total_ot_noche += ot_noche
                total_bonus50  += b50
                total_bonus100 += b100

                if wd < 5 and not es_feriado:
                    dias_hab_trabajados[fecha] = True

        total_dias_ausentes  = 0
        total_dias_descuento = 0

        semana_ini = (
            pd.Timestamp(fecha_inicio)
            - pd.Timedelta(days=pd.Timestamp(fecha_inicio).weekday())
        )

        while semana_ini.date() <= fecha_fin:
            dias_esperados  = 0
            dias_trabajados = 0

            for d in range(5):
                dia = (semana_ini + pd.Timedelta(days=d)).date()
                if dia < fecha_inicio or dia > fecha_fin:
                    continue
                if dia in feriados_set:
                    continue
                dias_esperados += 1
                if dia in dias_hab_trabajados:
                    dias_trabajados += 1

            dias_faltados = max(0, dias_esperados - dias_trabajados)
            dias_desc     = ABSENCE_DEDUCTION_DAYS.get(
                                min(dias_faltados, 5), 7)
            total_dias_ausentes  += dias_faltados
            total_dias_descuento += dias_desc
            semana_ini += pd.Timedelta(days=7)

        desc_ausencias = round(total_dias_descuento * val_dia)
        desc_atrasos   = round(total_atraso         * val_minuto)
        monto_ot_dia   = round(total_ot_dia   * val_minuto * 0.5)
        monto_ot_noche = round(total_ot_noche * val_minuto * 1.0)
        monto_bonus50  = round(total_bonus50  * val_minuto * 0.5)
        monto_bonus100 = round(total_bonus100 * val_minuto * 1.0)

        imponible = max(0, round(
            sal_prop
            - desc_ausencias
            - desc_atrasos
            + monto_ot_dia
            + monto_ot_noche
            + monto_bonus50
            + monto_bonus100
        ))
        ips     = round(imponible * TASA_IPS)
        liquido = max(0, imponible - ips)

        resultados.append({
            "Funcionario":            nombre,
            "CI Nº":                  ci,
            "Cargo":                  cargo,
            "Período":                periodo_tipo,
            "Fecha Inicio":           fecha_inicio,
            "Fecha Fin":              fecha_fin,
            "Días Calendario":        dias_calendario,
            "Días Hábiles (ref)":     dias_hab,
            "Salario Base":           salario_base,
            "Salario Proporcional":   sal_prop,
            "Días Ausentes":          total_dias_ausentes,
            "Días a Descontar":       total_dias_descuento,
            "Desc. Ausencias":        desc_ausencias,
            "Atrasos (min)":          total_atraso,
            "Desc. Atrasos":          desc_atrasos,
            "OT Diurna (min)":        total_ot_dia,
            "Monto OT Diurna":        monto_ot_dia,
            "OT Nocturna (min)":      total_ot_noche,
            "Monto OT Nocturna":      monto_ot_noche,
            "Bonus Sáb Mañana (min)": total_bonus50,
            "Monto Bonus Sáb Mañana": monto_bonus50,
            "Bonus Descanso (min)":   total_bonus100,
            "Monto Bonus Descanso":   monto_bonus100,
            "Feriados Trabajados":    feriados_trabajados,
            "Total Imponible":        imponible,
            "IPS (9%)":               ips,
            "Salario Líquido":        liquido,
        })

    return pd.DataFrame(resultados)


# ==========================================
# PDF — HOLERITE
# ==========================================
class PDFHolerite(FPDF):
    def __init__(self):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.set_auto_page_break(auto=False)

    def _half(self, data: dict, es_empresa: bool, oy: float):
        L       = 8
        RW      = 194
        C_COD   = 12
        C_DESC  = 88
        C_REF   = 30
        C_REC   = 32
        C_DESC2 = 32

        fi = data.get("Fecha Inicio")
        ff = data.get("Fecha Fin")
        if fi and ff:
            if isinstance(fi, datetime.datetime): fi = fi.date()
            if isinstance(ff, datetime.datetime): ff = ff.date()
            periodo_str = (f"{fi.strftime('%d/%m/%Y')} "
                           f"al {ff.strftime('%d/%m/%Y')}")
        else:
            now = datetime.datetime.now()
            meses = ["","enero","febrero","marzo","abril","mayo","junio",
                     "julio","agosto","septiembre","octubre",
                     "noviembre","diciembre"]
            periodo_str = f"{meses[now.month]}/{now.year}"

        tipo_txt = ("COMPROVANTE EMPRESA" if es_empresa
                    else "COMPROVANTE FUNCIONARIO")

        self.set_xy(L, 4 + oy)
        self.set_font("Helvetica", "B", 9)
        self.cell(RW / 2, 5, "Empresa", 0, 0, "L")
        self.cell(RW / 2, 5,
                  f"RECIBO DE PAGO DE SALÁRIO — {tipo_txt}", 0, 1, "R")
        self.set_xy(L, 9 + oy)
        self.set_font("Helvetica", "B", 11)
        self.cell(RW / 2, 5, "WIEGAND BRITO", 0, 0, "L")
        self.set_xy(L + RW / 2, 9 + oy)
        self.set_font("Helvetica", "B", 7)
        self.cell(RW / 4, 5, "RUC", 0, 0, "C")
        self.cell(RW / 4, 5, "Período", 0, 1, "C")
        self.set_xy(L + RW / 2, 14 + oy)
        self.set_font("Helvetica", "", 8)
        self.cell(RW / 4, 5, "9230303-0", 0, 0, "C")
        self.cell(RW / 4, 5, periodo_str, 0, 1, "C")
        self.line(L, 20 + oy, L + RW, 20 + oy)

        self.set_xy(L, 21 + oy)
        self.set_font("Helvetica", "B", 7)
        self.cell(RW * 0.55, 4, "Funcionário", 0, 0)
        self.cell(RW * 0.25, 4, "CI Nº", 0, 0)
        self.cell(RW * 0.20, 4, "CARGO", 0, 1)
        self.set_xy(L, 25 + oy)
        self.set_font("Helvetica", "", 9)
        self.cell(RW * 0.55, 5,
                  str(data["Funcionario"]).upper(), 0, 0)
        self.cell(RW * 0.25, 5, str(data["CI Nº"]), 0, 0)
        self.cell(RW * 0.20, 5,
                  str(data["Cargo"]).upper(), 0, 1)
        self.line(L, 31 + oy, L + RW, 31 + oy)

        self.set_xy(L, 32 + oy)
        self.set_font("Helvetica", "B", 7)
        self.cell(C_COD,   6, "Cód.",        1, 0, "C")
        self.cell(C_DESC,  6, "Descripción", 1, 0, "C")
        self.cell(C_REF,   6, "Ref.",        1, 0, "C")
        self.cell(C_REC,   6, "A RECIBIR",   1, 0, "C")
        self.cell(C_DESC2, 6, "DESCUENTOS",  1, 1, "C")

        ROW_H = 6
        alt   = False

        def fila(cod, desc, ref, recibir, descuento):
            nonlocal alt
            bg = (245, 245, 245) if alt else (255, 255, 255)
            self.set_fill_color(*bg)
            self.set_font("Helvetica", "", 7)
            self.cell(C_COD,   ROW_H, str(cod),            1, 0, "C", True)
            self.cell(C_DESC,  ROW_H, f" {desc}",          1, 0, "L", True)
            self.cell(C_REF,   ROW_H, str(ref),            1, 0, "C", True)
            self.cell(C_REC,   ROW_H, format_gs(recibir),  1, 0, "R", True)
            self.cell(C_DESC2, ROW_H, format_gs(descuento),1, 1, "R", True)
            alt = not alt

        dias_cal = data.get("Días Calendario", 30)
        ref_base = ("7 días / 30" if data["Período"] == "Semanal"
                    else f"{dias_cal} días / 30")
        fila("1", "Salario Base (sáb/dom incluidos)",
             ref_base, data["Salario Proporcional"], 0)

        if data.get("Monto OT Diurna", 0) > 0:
            hrs = data["OT Diurna (min)"] / 60
            fila("2", "Hora Extra Diurna (+50% s/valor minuto)",
                 f"{hrs:.1f} hrs", data["Monto OT Diurna"], 0)

        if data.get("Monto OT Nocturna", 0) > 0:
            hrs = data["OT Nocturna (min)"] / 60
            fila("3", "Hora Extra Nocturna (+100% s/valor minuto)",
                 f"{hrs:.1f} hrs", data["Monto OT Nocturna"], 0)

        if data.get("Monto Bonus Sáb Mañana", 0) > 0:
            hrs = data["Bonus Sáb Mañana (min)"] / 60
            fila("4", "Bonif. Sábado Mañana (+50% s/val. min)",
                 f"{hrs:.1f} hrs", data["Monto Bonus Sáb Mañana"], 0)

        if data.get("Monto Bonus Descanso", 0) > 0:
            hrs = data["Bonus Descanso (min)"] / 60
            label = "Bonif. Sáb Tarde/Dom/Feriado (+100% s/val. min)"
            if data.get("Feriados Trabajados", 0) > 0:
                label += f"  [{data['Feriados Trabajados']} feriado(s)]"
            fila("5", label, f"{hrs:.1f} hrs",
                 data["Monto Bonus Descanso"], 0)

        if data.get("Desc. Ausencias", 0) > 0:
            fila("6", "Faltas y Ausencias Injustificadas",
                 f"{data['Días a Descontar']} días desc.",
                 0, data["Desc. Ausencias"])

        if data.get("Desc. Atrasos", 0) > 0:
            fila("7", "Atrasos",
                 f"{data['Atrasos (min)']} mins",
                 0, data["Desc. Atrasos"])

        fila("8", "Descuento IPS", "9% s/ Imponible",
             0, data["IPS (9%)"])

        min_y = 95 + oy
        while self.get_y() < min_y:
            fila("", "", "", 0, 0)

        self.set_font("Helvetica", "B", 8)
        self.set_fill_color(220, 220, 220)
        self.cell(C_COD + C_DESC + C_REF, ROW_H,
                  "SALARIO LÍQUIDO →", 1, 0, "R", True)
        self.cell(C_REC + C_DESC2, ROW_H,
                  format_gs(data["Salario Líquido"]), 1, 1, "R", True)

        y_obs = self.get_y() + 3
        self.set_xy(L, y_obs)
        self.set_font("Helvetica", "B", 7)
        self.cell(RW, 4, "OBS: PAGO EFECTUADO EN LA CUENTA DE:", 0, 1)
        self.set_font("Helvetica", "", 8)
        self.cell(RW, 4, str(data["Funcionario"]).upper(), 0, 1)
        self.cell(RW, 4, f"CI: {data['CI Nº']}", 0, 1)

        y_f = self.get_y() + 5
        self.set_xy(L, y_f)
        self.set_font("Helvetica", "", 7)
        self.cell(40, 5, "FECHA: _____/_____/_______", 0, 0)
        self.cell(5,  5, "", 0, 0)
        self.cell(65, 5, "FIRMA EMPRESA: _______________________", 0, 0)
        self.cell(5,  5, "", 0, 0)
        self.cell(60, 5, "FIRMA FUNC.: ________________________", 0, 1)

    def generar(self, data: dict):
        self.add_page()
        self._half(data, es_empresa=True, oy=0)
        self.set_draw_color(160, 160, 160)
        self.set_line_width(0.15)
        x = 8
        while x < 202:
            self.line(x, 148.5, min(x + 3, 202), 148.5)
            x += 6
        self.set_xy(97, 146.5)
        self.set_font("Helvetica", "I", 6)
        self.set_text_color(140, 140, 140)
        self.cell(18, 4, "✂  CORTE", 0, 0, "C")
        self.set_text_color(0, 0, 0)
        self.set_draw_color(0, 0, 0)
        self.set_line_width(0.2)
        self._half(data, es_empresa=False, oy=148.5)


# ==========================================
# ZIP DE HOLERITES
# ==========================================
def generar_zip(df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for _, row in df.iterrows():
            data = row.to_dict()
            pdf  = PDFHolerite()
            pdf.generar(data)
            raw = pdf.output()
            if isinstance(raw, str):
                raw = raw.encode("latin-1")
            nombre_safe = (str(data["Funcionario"])
                           .replace(" ", "_").replace("/", "-"))
            fi = data["Fecha Inicio"]
            ff = data["Fecha Fin"]
            if isinstance(fi, datetime.datetime): fi = fi.date()
            if isinstance(ff, datetime.datetime): ff = ff.date()
            zf.writestr(
                f"Holerite_{nombre_safe}_{fi}_{ff}.pdf", raw)
    return buf.getvalue()


# ==========================================
# COMPONENTE: CALENDARIO INTERACTIVO DE FERIADOS
# ==========================================
def calendario_feriados(fecha_inicio: datetime.date,
                         fecha_fin: datetime.date,
                         key_prefix: str = "cal") -> list:
    if f"{key_prefix}_year" not in st.session_state:
        st.session_state[f"{key_prefix}_year"]  = fecha_inicio.year
    if f"{key_prefix}_month" not in st.session_state:
        st.session_state[f"{key_prefix}_month"] = fecha_inicio.month
    if f"{key_prefix}_selected" not in st.session_state:
        st.session_state[f"{key_prefix}_selected"] = set()

    selected: set = st.session_state[f"{key_prefix}_selected"]
    yr  = st.session_state[f"{key_prefix}_year"]
    mo  = st.session_state[f"{key_prefix}_month"]

    nav1, nav2, nav3 = st.columns([1, 3, 1])
    with nav1:
        if st.button("◀", key=f"{key_prefix}_prev"):
            if mo == 1:
                st.session_state[f"{key_prefix}_month"] = 12
                st.session_state[f"{key_prefix}_year"]  = yr - 1
            else:
                st.session_state[f"{key_prefix}_month"] = mo - 1
            st.rerun()
    with nav2:
        meses_es = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo",
                    "Junio", "Julio", "Agosto", "Septiembre",
                    "Octubre", "Noviembre", "Diciembre"]
        st.markdown(
            f"<h4 style='text-align:center;margin:0'>"
            f"{meses_es[mo]} {yr}</h4>",
            unsafe_allow_html=True
        )
    with nav3:
        if st.button("▶", key=f"{key_prefix}_next"):
            if mo == 12:
                st.session_state[f"{key_prefix}_month"] = 1
                st.session_state[f"{key_prefix}_year"]  = yr + 1
            else:
                st.session_state[f"{key_prefix}_month"] = mo + 1
            st.rerun()

    dias_semana = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
    header_cols = st.columns(7)
    for i, d in enumerate(dias_semana):
        color = "#cc0000" if i >= 5 else "#444444"
        header_cols[i].markdown(
            f"<div style='text-align:center;font-weight:bold;"
            f"color:{color};font-size:13px'>{d}</div>",
            unsafe_allow_html=True
        )

    for semana in calendar.monthcalendar(yr, mo):
        row_cols = st.columns(7)
        for i, dia_num in enumerate(semana):
            with row_cols[i]:
                if dia_num == 0:
                    st.markdown(
                        "<div style='height:38px'></div>",
                        unsafe_allow_html=True)
                    continue
                fecha       = datetime.date(yr, mo, dia_num)
                en_periodo  = fecha_inicio <= fecha <= fecha_fin
                es_selected = fecha in selected
                es_finde    = i >= 5

                if not en_periodo:
                    st.markdown(
                        f"<div style='text-align:center;padding:8px 0;"
                        f"color:#bbbbbb;font-size:12px;"
                        f"border:1px solid #eeeeee;"
                        f"border-radius:4px;background:#fafafa'>"
                        f"{dia_num}</div>",
                        unsafe_allow_html=True
                    )
                else:
                    if es_selected:
                        label = f"🔴 {dia_num}"
                    elif es_finde:
                        label = f"🔵 {dia_num}"
                    else:
                        label = str(dia_num)

                    if st.button(label, key=f"{key_prefix}_{fecha}"):
                        if fecha in selected:
                            selected.discard(fecha)
                        else:
                            selected.add(fecha)
                        st.rerun()

    st.markdown(
        "<div style='font-size:11px;color:#666;margin-top:6px'>"
        "🔴 Feriado marcado &nbsp;|&nbsp; "
        "🔵 Fin de semana &nbsp;|&nbsp; "
        "Número sin color = día hábil del período"
        "</div>",
        unsafe_allow_html=True
    )

    if selected:
        if st.button("🗑️ Limpiar todos los feriados",
                     key=f"{key_prefix}_clear"):
            st.session_state[f"{key_prefix}_selected"] = set()
            st.rerun()

    return sorted(list(selected))


# ==========================================
# INTERFAZ STREAMLIT
# ==========================================
st.write("---")
st.write("### 1. Carga de Documentos")
col1, col2 = st.columns(2)
with col1:
    st.markdown("**Funcionarios y Sueldos**")
    pdf_func = st.file_uploader(
        "PDF con lista de empleados y salarios",
        type="pdf", key="func")
with col2:
    st.markdown("**Book (Biométrico)**")
    pdf_bio = st.file_uploader(
        "PDF con marcaciones del reloj biométrico",
        type="pdf", key="bio")

st.write("---")
st.write("### 2. Período de Nómina")
col_tipo, col_d1, col_d2 = st.columns([1, 2, 2])
with col_tipo:
    periodo_tipo = st.selectbox(
        "Tipo de período", ["Semanal", "Mensual"],
        help="Semanal: salario × 7 / 30. "
             "Mensual: salario × días_calendario / 30.")
with col_d1:
    fecha_inicio = st.date_input(
        "Fecha de inicio", value=datetime.date(2026, 6, 1))
with col_d2:
    fecha_fin = st.date_input(
        "Fecha de fin", value=datetime.date(2026, 6, 30))

if fecha_inicio > fecha_fin:
    st.error("La fecha de inicio no puede ser posterior a la fecha de fin.")
    st.stop()

st.write("---")
st.write("### 3. Feriados del Período")
st.markdown(
    "Hacé clic en un día para marcarlo como feriado 🔴. "
    "Los feriados **no generan ausencia** si el empleado no trabaja "
    "(el día ya está pagado en el salario base). "
    "Si trabaja en feriado, recibe **+100% de bonus** sobre el valor minuto."
)

tab_cal, tab_lista = st.tabs(
    ["📅 Calendario interactivo", "⚡ Agregar por fecha"])

with tab_cal:
    feriados_calendario = calendario_feriados(
        fecha_inicio, fecha_fin, key_prefix="cal")

with tab_lista:
    col_f1, col_f2 = st.columns([2, 1])
    with col_f1:
        feriado_nuevo = st.date_input(
            "Agregar feriado", value=fecha_inicio, key="feriado_input")
    with col_f2:
        st.write("")
        st.write("")
        if st.button("➕ Agregar al calendario"):
            if "cal_selected" not in st.session_state:
                st.session_state["cal_selected"] = set()
            st.session_state["cal_selected"].add(feriado_nuevo)
            st.rerun()

    FERIADOS_PY_2026 = [
        datetime.date(2026, 1, 1),  datetime.date(2026, 3, 1),
        datetime.date(2026, 4, 2),  datetime.date(2026, 4, 3),
        datetime.date(2026, 5, 1),  datetime.date(2026, 5, 15),
        datetime.date(2026, 6, 12), datetime.date(2026, 8, 15),
        datetime.date(2026, 9, 29), datetime.date(2026, 12, 8),
        datetime.date(2026, 12, 25),
    ]
    if st.button("📅 Precargar feriados nacionales de Paraguay 2026"):
        if "cal_selected" not in st.session_state:
            st.session_state["cal_selected"] = set()
        for f in FERIADOS_PY_2026:
            st.session_state["cal_selected"].add(f)
        st.rerun()

feriados_lista      = sorted(st.session_state.get("cal_selected", set()))
feriados_en_periodo = [f for f in feriados_lista
                        if fecha_inicio <= f <= fecha_fin]

if feriados_lista:
    st.markdown("**Feriados marcados:**")
    cols_f = st.columns(6)
    for i, f in enumerate(feriados_lista):
        en_p = "✅" if fecha_inicio <= f <= fecha_fin else "⚠️ fuera"
        with cols_f[i % 6]:
            st.caption(f"{f.strftime('%d/%m/%Y')}  {en_p}")
    if feriados_en_periodo:
        st.info(
            f"{len(feriados_en_periodo)} feriado(s) en el período: "
            + ", ".join(f.strftime("%d/%m/%Y")
                        for f in feriados_en_periodo))
    else:
        st.info("Ningún feriado marcado cae dentro del período.")
else:
    st.info("No hay feriados marcados aún.")

dias_cal_prev = (fecha_fin - fecha_inicio).days + 1
dias_hab_prev = calcular_dias_habiles(
    fecha_inicio, fecha_fin, feriados_en_periodo)

st.success(
    f"Período: **{fecha_inicio.strftime('%d/%m/%Y')}** al "
    f"**{fecha_fin.strftime('%d/%m/%Y')}** — "
    f"**{dias_cal_prev} días calendario** (÷30 para salario) — "
    f"**{dias_hab_prev} días hábiles** (ref. ausencias) — "
    f"Tipo: **{periodo_tipo}**"
)

with st.expander("🔢 Verificar lógica de cálculo con ejemplo"):
    sal_ej = st.number_input(
        "Salario base de ejemplo (Gs.)",
        min_value=0, value=2_500_000, step=100_000)
    if sal_ej > 0:
        vd  = sal_ej / 30
        vm  = vd / 480
        sp  = sal_ej * dias_cal_prev / 30
        st.markdown(
            f"- Valor día: **{format_gs(vd)}** (salario / 30)\n"
            f"- Valor minuto: **Gs. {vm:,.1f}**\n"
            f"- Salario proporcional al período "
            f"({dias_cal_prev} días): **{format_gs(sp)}**\n"
            f"- Sábado/dom no trabajado: **Gs. 0** adicional\n"
            f"- Sábado mañana trabajado (p.ej. 4h): "
            f"**{format_gs(4*60*vm*0.5)}** de bonus\n"
            f"- Domingo/feriado trabajado (p.ej. 8h): "
            f"**{format_gs(8*60*vm*1.0)}** de bonus\n"
            f"- Ausencia 1 día hábil: descuento **{format_gs(vd)}**"
        )

st.write("---")

# ==========================================
# PROCESAMIENTO
# ==========================================
if pdf_func and pdf_bio:
    if st.button("⚙️  Procesar y Calcular Nómina",
                 type="primary", use_container_width=True):
        with st.spinner("Procesando documentos..."):
            try:
                text_func = extract_text_from_pdf(pdf_func)
                text_bio  = extract_text_from_pdf(pdf_bio)

                # ── Diagnóstico (descomentar si hay problemas) ──
                # with st.expander("🛠 RAW funcionarios"):
                #     st.text(text_func[:4000])
                # with st.expander("🛠 RAW book"):
                #     st.text(text_bio[:4000])

                df_f1 = parse_funcionarios(text_func)
                df_f2 = parse_funcionarios(text_bio)

                if not df_f1.empty and len(df_f1) >= len(df_f2):
                    df_func_data = df_f1
                    text_bio_use = text_bio
                elif not df_f2.empty:
                    df_func_data = df_f2
                    text_bio_use = text_func
                else:
                    st.error(
                        "No se pudieron extraer datos de empleados. "
                        "Descomentá las líneas de diagnóstico para ver "
                        "el texto raw extraído del PDF.")
                    st.stop()

                with st.expander("🔍 Funcionarios detectados"):
                    st.dataframe(df_func_data, use_container_width=True)

                df_bio_data = parse_biometrico(text_bio_use, df_func_data)

                if df_bio_data.empty:
                    st.error(
                        "No se detectaron marcaciones válidas en el Book.")
                    st.stop()

                with st.expander("🔍 Marcaciones biométricas (primeras 60)"):
                    st.dataframe(
                        df_bio_data.head(60), use_container_width=True)

                df_res = calcular_nomina(
                    df_func_data, df_bio_data,
                    fecha_inicio, fecha_fin,
                    periodo_tipo,
                    feriados=feriados_en_periodo,
                )

                if df_res.empty:
                    st.error("No se generaron resultados de nómina.")
                    st.stop()

                st.session_state["resultados"]      = df_res
                st.session_state["feriados_usados"] = feriados_en_periodo
                st.success(
                    f"✅ Cálculo completado — {len(df_res)} funcionarios — "
                    f"{len(feriados_en_periodo)} feriado(s) aplicado(s)."
                )

            except Exception as e:
                st.error(f"Error durante el procesamiento: {e}")
                st.exception(e)
else:
    st.info("⬆️  Suba ambos PDFs para habilitar el cálculo.")


# ==========================================
# REVISIÓN Y EXPORTACIÓN
# ==========================================
if ("resultados" in st.session_state
        and not st.session_state["resultados"].empty):
    df_res = st.session_state["resultados"]

    st.write("---")
    st.write("### 4. Revisión de Resultados")

    cols_gs = [
        "Salario Base", "Salario Proporcional",
        "Desc. Ausencias", "Desc. Atrasos",
        "Monto OT Diurna", "Monto OT Nocturna",
        "Monto Bonus Sáb Mañana", "Monto Bonus Descanso",
        "Total Imponible", "IPS (9%)", "Salario Líquido",
    ]
    df_view = df_res.copy()
    for c in cols_gs:
        if c in df_view.columns:
            df_view[c] = df_view[c].apply(format_gs)

    st.dataframe(
        df_view[[
            "Funcionario", "CI Nº", "Cargo", "Período",
            "Días Calendario", "Días Hábiles (ref)",
            "Días Ausentes", "Días a Descontar", "Atrasos (min)",
            "OT Diurna (min)", "OT Nocturna (min)",
            "Bonus Sáb Mañana (min)", "Bonus Descanso (min)",
            "Feriados Trabajados",
            "Salario Proporcional", "Desc. Ausencias", "Desc. Atrasos",
            "Monto OT Diurna", "Monto OT Nocturna",
            "Monto Bonus Sáb Mañana", "Monto Bonus Descanso",
            "Total Imponible", "IPS (9%)", "Salario Líquido",
        ]],
        use_container_width=True,
    )

    st.write("#### Totales del Período")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Imponible",
              format_gs(df_res["Total Imponible"].sum()))
    c2.metric("Total IPS Obrero",
              format_gs(df_res["IPS (9%)"].sum()))
    c3.metric("Total Líquido",
              format_gs(df_res["Salario Líquido"].sum()))
    c4.metric("Funcionarios", str(len(df_res)))

    st.write("---")
    st.write("### 5. Exportar Holerites")
    with st.spinner("Generando PDFs..."):
        zip_bytes = generar_zip(df_res)

    st.download_button(
        label="⬇️  Descargar Holerites en PDF (ZIP)",
        data=zip_bytes,
        file_name=(
            f"Holerites_WiegandBrito_"
            f"{fecha_inicio.strftime('%Y%m%d')}_"
            f"{fecha_fin.strftime('%Y%m%d')}.zip"
        ),
        mime="application/zip",
        type="primary",
        use_container_width=True,
    )
    st.caption(
        "Cada PDF contiene dos copias (empresa / funcionario) "
        "separadas por línea de corte. "
        "Todos los montos en Guaraníes (Gs.)."
    )
