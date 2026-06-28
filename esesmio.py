import streamlit as st
import pandas as pd
import pdfplumber
import datetime
import re
import calendar
import unicodedata
from io import BytesIO
import zipfile
from fpdf import FPDF
import numpy as np


try:
    import fitz
except Exception:
    fitz = None




# ==========================================
# CONFIGURAÇÃO UI
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
DIAS_MES = 30
TASA_IPS = 0.09
TOLERANCIA_MIN = 5
MINUTOS_RECESSO = 45 # Constante para los 45 minutos de receso


ABSENCE_DEDUCTION_DAYS = {0: 0, 1: 1, 2: 4, 3: 5, 4: 6, 5: 7}




# ==========================================
# UTILIDADES
# ==========================================
def format_gs(monto):
    if monto is None or pd.isna(monto):
        return ""
    try:
        monto = float(monto)
    except Exception:
        return ""
    if monto <= 0:
        return ""
    return "Gs. " + f"{int(round(monto)):,}".replace(",", ".")




def safe_pdf_text(value):
    """
    FPDF con fuentes core no maneja bien algunos caracteres Unicode.
    Esta función deja el texto compatible con Latin-1.
    """
    if value is None:
        return ""
    txt = str(value)
    txt = txt.replace("—", "-")
    txt = txt.replace("→", "->")
    txt = txt.replace("✂", "")
    txt = txt.replace("•", "-")
    return txt.encode("latin-1", "replace").decode("latin-1")




def slugify_filename(text):
    text = unicodedata.normalize("NFKD", str(text))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.replace(" ", "_").replace("/", "-")
    text = re.sub(r"[^A-Za-z0-9_\-]+", "_", text)
    return text.strip("_") or "holerite"




def _normalize_text(text):
    if text is None:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "\n")




def _normalize_lines(text):
    text = _normalize_text(text)
    raw_lines = text.split("\n")
    final_lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw_lines if line.strip()]
    return final_lines




def calcular_dias_habiles(fecha_inicio, fecha_fin, feriados=None):
    holidays = []
    if feriados:
        holidays = [np.datetime64(f, "D") for f in feriados]
    fi = np.datetime64(fecha_inicio, "D")
    ff = np.datetime64(fecha_fin, "D")
    return int(np.busday_count(fi, ff + np.timedelta64(1, "D"), holidays=holidays))




def _page_text_from_words(page):
    words = page.extract_words(keep_blank_chars=False, use_text_flow=True)
    if not words:
        return ""


    lines_map = {}
    for w in words:
        y_key = int(round(w["top"] / 3.0) * 3)
        lines_map.setdefault(y_key, []).append((w["x0"], w["text"]))


    chunks = []
    for y in sorted(lines_map.keys()):
        parts = [t for _, t in sorted(lines_map[y], key=lambda x: x[0])]
        line = " ".join(parts).strip()
        if line:
            chunks.append(line)


    return "\n".join(chunks)




def extract_text_from_pdf(pdf_file):
    """
    Extrae texto usando fitz (PyMuPDF) si está disponible.
    Se fitz falhar ou não estiver disponível, tenta com pdfplumber.
    Se uma página vier vazia, tenta reconstruí-la con extract_words().
    """
    # Tentar com fitz primeiro (melhor para PDFs problemáticos)
    if fitz is not None:
        try:
            if isinstance(pdf_file, str):
                doc = fitz.open(pdf_file)
            else:
                # Ler bytes do arquivo
                if hasattr(pdf_file, 'read'):
                    pos = None
                    if hasattr(pdf_file, 'tell'):
                        try:
                            pos = pdf_file.tell()
                        except Exception:
                            pass
                    try:
                        data = pdf_file.read()
                        if hasattr(pdf_file, 'seek') and pos is not None:
                            pdf_file.seek(pos)
                    except Exception:
                        data = None
                    if data:
                        doc = fitz.open(stream=data, filetype="pdf")
                    else:
                        doc = None


                if doc is not None:
                    try:
                        pages_text = []
                        for page in doc:
                            text = page.get_text() or ""
                            pages_text.append(_normalize_text(text))
                        text = "\n".join([p for p in pages_text if p.strip()])
                        if text.strip():
                            doc.close()
                            return text
                    finally:
                        if doc:
                            doc.close()
        except Exception as e:
            st.warning(f"Falha na extração com fitz: {e}. Tentando pdfplumber.")


    # Fallback a pdfplumber
    try:
        pages_text = []
        with pdfplumber.open(pdf_file) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if not text.strip():
                    text = _page_text_from_words(page)
                pages_text.append(_normalize_text(text))
        final_text = "\n".join([p for p in pages_text if p.strip()])
        if final_text.strip():
            return final_text
        else:
            st.error("pdfplumber extraiu texto vazio ou apenas espaços.")
            return ""
    except Exception as e:
        st.error(f"Falha na extração com pdfplumber: {e}. Não foi possível ler o PDF.")
        return ""




# ==========================================
# PARSER — FUNCIONARIOS Y SUELDOS
#
# Formato real del archivo:
#   ID.
#   Nome
#   C.I.
#   Salarios
#   111
#   Ignacio
#   5083111
#   Gs.7.000.000
#   105
#   Ricardo Perelló
#   Gs.11.000.000
# ==========================================
def parse_funcionarios(text):
    lines = _normalize_lines(text)


    HEADERS = {"id.", "id", "nome", "nombre", "c.i.", "ci", "salarios", "salario"}
    ID_RE = re.compile(r"^\d{1,4}$")
    CI_RE = re.compile(r"^\d{4,12}$")
    SAL_RE = re.compile(r"^[Gg][Ss]\.\s*([\d.]+)(?:\s*-\s*\d+)?$") # Ajustado para ignorar "- 0" ou similar
    FULL_RE = re.compile(r"^(\d{1,4})\s+(.+?)\s+(?:(\d{4,12})\s+)?[Gg][Ss]\.\s*([\d.]+)(?:\s*-\s*\d+)?\s*$")


    funcionarios = []
    seen_ids = set()


    i = 0
    while i < len(lines):
        raw = lines[i]
        low = raw.lower()


        if low in HEADERS:
            i += 1
            continue


        # Caso 1: todo en una sola línea
        m_full = FULL_RE.match(raw)
        if m_full:
            emp_id = int(m_full.group(1))
            if emp_id not in seen_ids:
                nombre = m_full.group(2).strip()
                ci = m_full.group(3) or ""
                salary_str = m_full.group(4).replace(".", "")
                try:
                    salary = float(salary_str)
                except ValueError:
                    st.warning(f"Salário inválido '{salary_str}' para ID {emp_id} na linha '{raw}'. Pulando.")
                    i += 1
                    continue


                if salary >= 100_000 and nombre:
                    seen_ids.add(emp_id)
                    funcionarios.append({
                        "ID": emp_id,
                        "NOMBRE Y APELLIDO": nombre,
                        "CI Nº": ci,
                        "SALARIO REAJUSTADO": salary,
                    })
                else:
                    st.warning(f"Linha única - Dados incompletos ou salário baixo para '{raw}'. Pulando.")
            else:
                st.warning(f"ID duplicado '{emp_id}' encontrado na linha '{raw}'. Pulando.")
            i += 1
            continue


        # Caso 2: bloque por líneas
        if not ID_RE.fullmatch(raw):
            i += 1
            continue


        emp_id = int(raw)
        if emp_id in seen_ids:
            st.warning(f"ID duplicado '{emp_id}' encontrado em bloco. Pulando.")
            i += 1
            continue


        j = i + 1
        block = []


        while j < len(lines):
            cur = lines[j]
            cur_low = cur.lower()


            if cur_low in HEADERS:
                j += 1
                continue


            if ID_RE.fullmatch(cur):
                break


            block.append(cur)
            j += 1


        if not block:
            st.warning(f"Bloco vazio para ID {emp_id}. Pulando.")
            i = j
            continue


        salary_idx = None
        for k in range(len(block) - 1, -1, -1):
            if SAL_RE.match(block[k]):
                salary_idx = k
                break


        if salary_idx is None:
            st.warning(f"Salário não encontrado no bloco para ID {emp_id}: {block}. Pulando.")
            i = j
            continue


        m_sal = SAL_RE.match(block[salary_idx])
        salary_str = m_sal.group(1).replace(".", "")
        try:
            salary = float(salary_str)
        except ValueError:
            st.warning(f"Salário inválido '{salary_str}' para ID {emp_id} em bloco. Pulando.")
            i = j
            continue


        ci = ""
        if salary_idx > 0 and CI_RE.fullmatch(block[salary_idx - 1]):
            ci = block[salary_idx - 1]
            nombre = " ".join(block[:salary_idx - 1]).strip()
        else:
            nombre = " ".join(block[:salary_idx]).strip()


        if salary >= 100_000 and nombre:
            seen_ids.add(emp_id)
            funcionarios.append({
                "ID": emp_id,
                "NOMBRE Y APELLIDO": nombre,
                "CI Nº": ci,
                "SALARIO REAJUSTADO": salary,
            })
        else:
            st.warning(f"Bloco - Dados incompletos ou salário baixo para ID {emp_id}: {block}. Pulando.")


        i = j


    if not funcionarios:
        st.error("NENHUM funcionário válido foi extraído do PDF.")
    return pd.DataFrame(funcionarios)




# ==========================================
# PARSER — BOOK (BIOMÉTRICO)
#
# Formato do arquivo Registros_de_asistencia.txt:
#   ID.    Nombre  Depart. Tiempo  ID del dispositivo
#   26    luis alberto    montador steel   01/06/2026     07:05:00    1
# ==========================================
def parse_biometrico(text, df_funcionarios):
    if df_funcionarios is None or df_funcionarios.empty:
        st.error("DataFrame de funcionários vazio ou não fornecido.")
        return pd.DataFrame()


    id_to_nombre = dict(zip(
        df_funcionarios["ID"].astype(int),
        df_funcionarios["NOMBRE Y APELLIDO"]
    ))


    # Mapeamento de departamentos para nomes de cargos mais amigáveis
    DEPTOS_MAP = {
        "montador steel": "Montador Steel Frame",
        "montador drywall": "Montador DryWall",
        "zeladoria": "Zelador",
        "administrativo": "Administrativo",
        # Adicione outros mapeamentos conforme necessário
    }


    # Nova expressão regular para o formato do arquivo Registros_de_asistencia.txt
    # Captura: ID, Nome, Departamento, Data, Hora, ID do dispositivo
    # Os campos são separados por tabulações ou múltiplos espaços.
    PAT_MARCACION_TXT = re.compile(
        r"^(\d+)\s+" +                                  # Grupo 1: ID do funcionário
        r"(.+?)\s+" +                                   # Grupo 2: Nome (não guloso)
        r"(.+?)\s+" +                                   # Grupo 3: Departamento (não guloso)
        r"(\d{2}/\d{2}/\d{4})\s+" +                     # Grupo 4: Data
        r"(\d{2}:\d{2}:\d{2})\s+" +                     # Grupo 5: Hora
        r"(\d+)$"                                       # Grupo 6: ID do dispositivo
    )


    lines = _normalize_lines(text)
    marcaciones = []
    processed_lines_count = 0
    failed_lines_count = 0


    # Ignorar a linha de cabeçalho se presente
    if lines and lines[0].lower().startswith("id.\tnombre\tdepart.\ttiempo\tid del dispositivo"):
        # st.warning(f"Linha 1 não corresponde ao padrão esperado da regex: '{lines[0]}'") # Removido
        lines = lines[1:]


    for raw_line in lines:
        processed_lines_count += 1


        m = PAT_MARCACION_TXT.match(raw_line.lower())
        if not m:
            # st.warning(f"Linha {processed_lines_count} não corresponde ao padrão esperado da regex: '{raw_line}'") # Removido
            failed_lines_count += 1
            continue


        try:
            emp_id = int(m.group(1))
            nombre_raw = m.group(2).strip()
            departamento_raw = m.group(3).strip()
            fecha_str = m.group(4)
            hora_str = m.group(5)
            ts_str = f"{fecha_str} {hora_str}"
            # dev_id = m.group(6) # Não usado diretamente no cálculo


            if emp_id not in id_to_nombre:
                st.warning(f"ID '{emp_id}' da linha '{raw_line}' não encontrado na lista de funcionários. Pulando.")
                failed_lines_count += 1
                continue


            cargo_display = DEPTOS_MAP.get(departamento_raw, "Funcionario")
            # if departamento_raw not in DEPTOS_MAP: # Removido
                # st.warning(f"Departamento '{departamento_raw}' não mapeado para ID {emp_id}. Usando 'Funcionario'.") # Removido


            try:
                fecha_hora = pd.to_datetime(ts_str, format="%d/%m/%Y %H:%M:%S")
            except ValueError:
                st.warning(f"Data/Hora inválida '{ts_str}' na linha '{raw_line}' para ID {emp_id}. Pulando.")
                failed_lines_count += 1
                continue


            marcaciones.append({
                "ID": emp_id,
                "Nomb.": id_to_nombre[emp_id], # Usar o nome completo do df_funcionarios
                "Tiempo": fecha_hora,
                "Cargo": cargo_display,
            })


        except Exception as e:
            st.error(f"Erro inesperado ao processar linha '{raw_line}': {e}")
            failed_lines_count += 1
            continue


    if not marcaciones:
        if processed_lines_count == 0:
            st.error("ERRO: O arquivo biométrico está vazio ou não foi possível extrair nenhuma linha. Verifique o conteúdo do arquivo.")
        else:
            st.error(f"ERRO: NENHUMA marcación válida foi extraída do arquivo. Total de linhas analisadas: {processed_lines_count}, falhas: {failed_lines_count}.")
    else:
        st.success(f"{len(marcaciones)} marcações válidas extraídas. Total de linhas analisadas: {processed_lines_count}, falhas: {failed_lines_count}.")


    return pd.DataFrame(marcaciones)




# ==========================================
# HELPER — MINUTOS NOCTURNOS
# ==========================================
def _minutos_nocturnos(entrada: pd.Timestamp, salida: pd.Timestamp) -> int:
    if salida <= entrada:
        return 0


    total = 0
    dia = entrada.normalize()


    while dia < salida:
        sig = dia + pd.Timedelta(days=1)


        # Horas entre 00:00 e 06:00
        ov_s = max(entrada, dia)
        ov_e = min(salida, dia + pd.Timedelta(hours=6))
        if ov_e > ov_s:
            min_periodo = int((ov_e - ov_s).total_seconds() / 60)
            total += min_periodo


        # Horas entre 22:00 e 24:00
        ov_s = max(entrada, dia + pd.Timedelta(hours=22))
        ov_e = min(salida, sig)
        if ov_e > ov_s:
            min_periodo = int((ov_e - ov_s).total_seconds() / 60)
            total += min_periodo


        dia = sig


    return total




# ==========================================
# CLASIFICACIÓN DE MINUTOS POR DÍA
# ==========================================
def clasificar_minutos(marcaciones_dia, weekday, es_feriado=False):
    punches = sorted(marcaciones_dia)
    if len(punches) % 2 != 0:
        st.warning(f"Número ímpar de marcações ({len(punches)}) para o dia {punches[0].date()}. Ignorando a última.")
        punches = punches[:-1]
    if not punches:
        return 0, 0, 0, 0, 0, 0


    pairs = []
    total_min_bruto = 0


    for i in range(0, len(punches), 2):
        entrada = punches[i]
        salida = punches[i + 1]
        delta = int((salida - entrada).total_seconds() / 60)
        if delta > 0:
            total_min_bruto += delta
            pairs.append((entrada, salida))
        else:
            st.warning(f"Marcação de saída antes ou igual à entrada: {entrada} -> {salida}. Ignorando par.")


    # Aplicar desconto de 45 minutos de receso
    total_min_liquido = max(0, total_min_bruto - MINUTOS_RECESSO)


    atraso_min = 0
    ot_dia_min = 0
    ot_noche_min = 0
    bonus50_min = 0
    bonus100_min = 0


    if es_feriado or weekday == 6: # Domingo ou Feriado
        bonus100_min = total_min_liquido


    elif weekday == 5: # Sábado
        # Para sábado, a jornada normal é de 4 horas (240 minutos).
        # Se o total_min_liquido for menor ou igual a 240, tudo é bonus50.
        # Se for maior, 240 é bonus50 e o restante é bonus100.
        if total_min_liquido <= 240:
            bonus50_min = total_min_liquido
        else:
            bonus50_min = 240
            bonus100_min = total_min_liquido - 240


    else: # Dias úteis (segunda a sexta)
        if total_min_liquido < MINUTOS_JORNADA:
            diff = MINUTOS_JORNADA - total_min_liquido
            atraso_min = 0 if diff < TOLERANCIA_MIN else diff


        elif total_min_liquido > MINUTOS_JORNADA:
            overtime = total_min_liquido - MINUTOS_JORNADA
            rem_ot = overtime


            for entrada, salida in reversed(pairs):
                if rem_ot <= 0:
                    break


                par_dur = int((salida - entrada).total_seconds() / 60)
                ot_este_par = min(rem_ot, par_dur)
                ot_desde = salida - pd.Timedelta(minutes=ot_este_par)


                noc = min(_minutos_nocturnos(ot_desde, salida), ot_este_par)
                ot_noche_min += noc
                ot_dia_min += ot_este_par - noc
                rem_ot -= ot_este_par


    return (
        total_min_liquido, atraso_min,
        ot_dia_min, ot_noche_min,
        bonus50_min, bonus100_min
    )




# ==========================================
# CÁLCULO DE NÓMINA
# ==========================================
def calcular_nomina(df_func, df_bio, fecha_inicio, fecha_fin, periodo_tipo, feriados=None):
    if df_bio.empty:
        st.error("DataFrame de marcações biométricas está vazio.")
        return pd.DataFrame()
    if df_func.empty:
        st.error("DataFrame de funcionários está vazio.")
        return pd.DataFrame()


    feriados = feriados or []
    feriados_set = set(feriados)


    if isinstance(fecha_inicio, datetime.datetime):
        fecha_inicio = fecha_inicio.date()
    if isinstance(fecha_fin, datetime.datetime):
        fecha_fin = fecha_fin.date()


    dias_calendario = (fecha_fin - fecha_inicio).days + 1
    dias_hab = max(calcular_dias_habiles(fecha_inicio, fecha_fin, feriados), 1)


    # Cargo más frecuente por funcionario
    dict_cargos = (
        df_bio.groupby("Nomb.")["Cargo"]
        .agg(lambda s: s.mode().iat[0] if not s.mode().empty else s.iloc[0])
        .to_dict()
    )


    df_bio_p = df_bio[
        (df_bio["Tiempo"].dt.date >= fecha_inicio) &
        (df_bio["Tiempo"].dt.date <= fecha_fin)
    ].copy()


    resultados = []


    for _, row_func in df_func.iterrows():
        emp_id = row_func["ID"]
        nombre = row_func["NOMBRE Y APELLIDO"]
        ci = row_func["CI Nº"]
        salario_base = row_func["SALARIO REAJUSTADO"]
        cargo = dict_cargos.get(nombre, "Funcionario") # Usar o cargo detectado ou padrão


        marcaciones_empleado = df_bio_p[df_bio_p["ID"] == emp_id]


        # Agrupar marcações por dia
        marcaciones_por_dia = marcaciones_empleado.groupby(marcaciones_empleado["Tiempo"].dt.date)["Tiempo"].apply(list)


        total_dias_trabalhados = 0
        total_dias_ausentes = 0
        total_atraso = 0
        total_ot_dia = 0
        total_ot_noche = 0
        total_bonus50 = 0
        total_bonus100 = 0
        feriados_trabajados = 0


        current_date = fecha_inicio
        while current_date <= fecha_fin:
            weekday = current_date.weekday() # 0=Segunda, 6=Domingo
            es_feriado = current_date in feriados_set


            if current_date in marcaciones_por_dia:
                (
                    min_trabalhados, atraso_min,
                    ot_dia_min, ot_noche_min,
                    bonus50_min, bonus100_min
                ) = clasificar_minutos(marcaciones_por_dia[current_date], weekday, es_feriado)


                if min_trabalhados > 0:
                    total_dias_trabalhados += 1
                    total_atraso += atraso_min
                    total_ot_dia += ot_dia_min
                    total_ot_noche += ot_noche_min
                    total_bonus50 += bonus50_min
                    total_bonus100 += bonus100_min
                    if es_feriado and min_trabalhados > 0:
                        feriados_trabajados += 1
            else:
                # Se não é fim de semana e não é feriado, conta como ausência
                if weekday < 5 and not es_feriado:
                    total_dias_ausentes += 1


            current_date += datetime.timedelta(days=1)


        # Cálculo de descontos e bônus
        val_dia = salario_base / DIAS_MES
        val_minuto = val_dia / MINUTOS_JORNADA


        # Desconto por ausências
        total_dias_descuento = 0
        if total_dias_ausentes > 0:
            # Aplicar penalidade se houver
            if total_dias_ausentes in ABSENCE_DEDUCTION_DAYS:
                total_dias_descuento = ABSENCE_DEDUCTION_DAYS[total_dias_ausentes]
            else:
                # Se não houver regra específica, descontar o número de dias ausentes
                total_dias_descuento = total_dias_ausentes
        desc_ausencias = total_dias_descuento * val_dia


        # Desconto por atrasos
        desc_atrasos = total_atraso * val_minuto


        # Salário proporcional
        if periodo_tipo == "Semanal":
            sal_prop = salario_base * 7 / DIAS_MES
        else: # Mensal
            sal_prop = salario_base * dias_calendario / DIAS_MES


        # Montos de horas extras y bonificaciones
        monto_ot_dia = total_ot_dia * val_minuto * 1.5 # +50%
        monto_ot_noche = total_ot_noche * val_minuto * 2.0 # +100%
        monto_bonus50 = total_bonus50 * val_minuto * 0.5 # +50% sobre o valor minuto base
        monto_bonus100 = total_bonus100 * val_minuto * 1.0 # +100% sobre o valor minuto base


        imponible = max(0, round(
            sal_prop
            - desc_ausencias
            - desc_atrasos
            + monto_ot_dia
            + monto_ot_noche
            + monto_bonus50
            + monto_bonus100
        ))


        ips = round(imponible * TASA_IPS)
        liquido = max(0, imponible - ips)




        resultados.append({
            "ID": emp_id, # Adicionado ID para facilitar a depuração
            "Funcionario": nombre,
            "CI Nº": ci,
            "Cargo": cargo,
            "Período": periodo_tipo,
            "Fecha Inicio": fecha_inicio,
            "Fecha Fin": fecha_fin,
            "Días Calendario": dias_calendario,
            "Días Hábiles (ref)": dias_hab,
            "Salario Base": salario_base,
            "Salario Proporcional": sal_prop,
            "Días Ausentes": total_dias_ausentes,
            "Días a Descontar": total_dias_descuento,
            "Desc. Ausencias": desc_ausencias,
            "Atrasos (min)": total_atraso,
            "Desc. Atrasos": desc_atrasos,
            "OT Diurna (min)": total_ot_dia,
            "Monto OT Diurna": monto_ot_dia,
            "OT Nocturna (min)": total_ot_noche,
            "Monto OT Nocturna": monto_ot_noche,
            "Bonus Sáb Mañana (min)": total_bonus50,
            "Monto Bonus Sáb Mañana": monto_bonus50,
            "Bonus Descanso (min)": total_bonus100,
            "Monto Bonus Descanso": monto_bonus100,
            "Feriados Trabajados": feriados_trabajados,
            "Total Imponible": imponible,
            "IPS (9%)": ips,
            "Salario Líquido": liquido,
        })


    if not resultados:
        st.error("NENHUM resultado de nómina foi gerado.")
    return pd.DataFrame(resultados)




# ==========================================
# PDF — HOLERITE
# ==========================================
class PDFHolerite(FPDF):
    def __init__(self):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.set_auto_page_break(auto=False)


    def _half(self, data: dict, es_empresa: bool, oy: float):
        pdf_t = safe_pdf_text


        L = 8
        RW = 194
        C_COD = 12
        C_DESC = 88
        C_REF = 30
        C_REC = 32
        C_DESC2 = 32


        fi = data.get("Fecha Inicio")
        ff = data.get("Fecha Fin")


        if fi and ff:
            if isinstance(fi, datetime.datetime):
                fi = fi.date()
            if isinstance(ff, datetime.datetime):
                ff = ff.date()
            periodo_str = f"{fi.strftime('%d/%m/%Y')} al {ff.strftime('%d/%m/%Y')}"
        else:
            now = datetime.datetime.now()
            periodo_str = f"{now.strftime('%m')}/{now.year}"


        tipo_txt = "COMPROBANTE EMPRESA" if es_empresa else "COMPROBANTE FUNCIONARIO"


        self.set_xy(L, 4 + oy)
        self.set_font("Helvetica", "B", 9)
        self.cell(RW / 2, 5, pdf_t("Empresa"), 0, 0, "L")
        self.cell(RW / 2, 5, pdf_t(f"RECIBO DE PAGO DE SALARIO - {tipo_txt}"), 0, 1, "R")


        self.set_xy(L, 9 + oy)
        self.set_font("Helvetica", "B", 11)
        self.cell(RW / 2, 5, pdf_t("WIEGAND BRITO"), 0, 0, "L")


        self.set_xy(L + RW / 2, 9 + oy)
        self.set_font("Helvetica", "B", 7)
        self.cell(RW / 4, 5, pdf_t("RUC"), 0, 0, "C")
        self.cell(RW / 4, 5, pdf_t("Período"), 0, 1, "C")


        self.set_xy(L + RW / 2, 14 + oy)
        self.set_font("Helvetica", "", 8)
        self.cell(RW / 4, 5, pdf_t("9230303-0"), 0, 0, "C")
        self.cell(RW / 4, 5, pdf_t(periodo_str), 0, 1, "C")


        self.line(L, 20 + oy, L + RW, 20 + oy)


        self.set_xy(L, 21 + oy)
        self.set_font("Helvetica", "B", 7)
        self.cell(RW * 0.55, 4, pdf_t("Funcionario"), 0, 0)
        self.cell(RW * 0.25, 4, pdf_t("CI Nº"), 0, 0)
        self.cell(RW * 0.20, 4, pdf_t("CARGO"), 0, 1)


        self.set_xy(L, 25 + oy)
        self.set_font("Helvetica", "", 9)
        self.cell(RW * 0.55, 5, pdf_t(str(data.get("Funcionario", "")).upper()), 0, 0)
        self.cell(RW * 0.25, 5, pdf_t(str(data.get("CI Nº", ""))), 0, 0)
        self.cell(RW * 0.20, 5, pdf_t(str(data.get("Cargo", "")).upper()), 0, 1)


        self.line(L, 31 + oy, L + RW, 31 + oy)


        self.set_xy(L, 32 + oy)
        self.set_font("Helvetica", "B", 7)
        self.cell(C_COD,   6, pdf_t("Cód."),        1, 0, "C")
        self.cell(C_DESC,  6, pdf_t("Descripción"), 1, 0, "C")
        self.cell(C_REF,   6, pdf_t("Ref."),        1, 0, "C")
        self.cell(C_REC,   6, pdf_t("A RECIBIR"),   1, 0, "C")
        self.cell(C_DESC2, 6, pdf_t("DESCUENTOS"),  1, 1, "C")


        ROW_H = 6
        alt = False


        def fila(cod, desc, ref, recibir, descuento):
            nonlocal alt
            bg = (245, 245, 245) if alt else (255, 255, 255)
            self.set_fill_color(*bg)
            self.set_font("Helvetica", "", 7)
            self.cell(C_COD,   ROW_H, pdf_t(str(cod)),             1, 0, "C", True)
            self.cell(C_DESC,  ROW_H, pdf_t(f" {desc}"),          1, 0, "L", True)
            self.cell(C_REF,   ROW_H, pdf_t(str(ref)),            1, 0, "C", True)
            self.cell(C_REC,   ROW_H, pdf_t(format_gs(recibir)),  1, 0, "R", True)
            self.cell(C_DESC2, ROW_H, pdf_t(format_gs(descuento)), 1, 1, "R", True)
            alt = not alt


        dias_cal = data.get("Días Calendario", 30)
        ref_base = "7 días / 30" if data.get("Período") == "Semanal" else f"{dias_cal} días / 30"


        fila("1", "Salario Base (sáb/dom incluidos)",
             ref_base, data.get("Salario Proporcional", 0), 0)


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
            label = "Bonif. Sábado Tarde/Dom/Feriado (+100% s/val. min)"
            if data.get("Feriados Trabajados", 0) > 0:
                label += f"  [{data['Feriados Trabajados']} feriado(s)]"
            fila("5", label, f"{hrs:.1f} hrs", data["Monto Bonus Descanso"], 0)


        if data.get("Desc. Ausencias", 0) > 0:
            fila("6", "Faltas y Ausencias Injustificadas",
                 f"{data.get('Días a Descontar', 0)} días desc.",
                 0, data["Desc. Ausencias"])


        if data.get("Desc. Atrasos", 0) > 0:
            fila("7", "Atrasos",
                 f"{data.get('Atrasos (min)', 0)} mins",
                 0, data["Desc. Atrasos"])


        fila("8", "Descuento IPS", "9% s/ Imponible",
             0, data.get("IPS (9%)", 0))


        min_y = 95 + oy
        while self.get_y() < min_y:
            fila("", "", "", 0, 0)


        self.set_font("Helvetica", "B", 8)
        self.set_fill_color(220, 220, 220)
        self.cell(C_COD + C_DESC + C_REF, ROW_H,
                  pdf_t("SALARIO LÍQUIDO ->"), 1, 0, "R", True)
        self.cell(C_REC + C_DESC2, ROW_H,
                  pdf_t(format_gs(data.get("Salario Líquido", 0))), 1, 1, "R", True)


        y_obs = self.get_y() + 3
        self.set_xy(L, y_obs)
        self.set_font("Helvetica", "B", 7)
        self.cell(RW, 4, pdf_t("OBS: PAGO EFECTUADO EN LA CUENTA DE:"), 0, 1)


        self.set_font("Helvetica", "", 8)
        self.cell(RW, 4, pdf_t(str(data.get("Funcionario", "")).upper()), 0, 1)
        self.cell(RW, 4, pdf_t(f"CI: {data.get('CI Nº', '')}"), 0, 1)


        y_f = self.get_y() + 5
        self.set_xy(L, y_f)
        self.set_font("Helvetica", "", 7)
        self.cell(40, 5, pdf_t("FECHA: _____/_____/_______"), 0, 0)
        self.cell(5, 5, "", 0, 0)
        self.cell(65, 5, pdf_t("FIRMA EMPRESA: _______________________"), 0, 0)
        self.cell(5, 5, "", 0, 0)
        self.cell(60, 5, pdf_t("FIRMA FUNC.: ________________________"), 0, 1)


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
        self.cell(18, 4, safe_pdf_text("CORTE"), 0, 0, "C")


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


            pdf = PDFHolerite()
            pdf.generar(data)


            try:
                raw = pdf.output(dest="S")
            except TypeError:
                raw = pdf.output()


            if isinstance(raw, str):
                raw = raw.encode("latin-1", "replace")
            elif isinstance(raw, bytearray):
                raw = bytes(raw)


            nombre_safe = slugify_filename(data.get("Funcionario", "desconocido"))


            fi = data.get("Fecha Inicio")
            ff = data.get("Fecha Fin")


            fi_str = ""
            ff_str = ""
            if fi and isinstance(fi, datetime.date):
                fi_str = fi.strftime("%Y%m%d")
            if ff and isinstance(ff, datetime.date):
                ff_str = ff.strftime("%Y%m%d")


            file_name = f"Holerite_{nombre_safe}_{fi_str}_{ff_str}.pdf"
            zf.writestr(file_name, raw)


    return buf.getvalue()




# ==========================================
# CALENDÁRIO INTERATIVO
# ==========================================
def calendario_feriados(fecha_inicio: datetime.date,
                        fecha_fin: datetime.date,
                        key_prefix: str = "cal") -> list:
    for key, default in [
        (f"{key_prefix}_year", fecha_inicio.year),
        (f"{key_prefix}_month", fecha_inicio.month),
        (f"{key_prefix}_selected", set()),
    ]:
        if key not in st.session_state:
            st.session_state[key] = default


    selected: set = st.session_state[f"{key_prefix}_selected"]
    yr = st.session_state[f"{key_prefix}_year"]
    mo = st.session_state[f"{key_prefix}_month"]


    nav1, nav2, nav3 = st.columns([1, 3, 1])


    with nav1:
        if st.button("◀", key=f"{key_prefix}_prev"):
            if mo == 1:
                st.session_state[f"{key_prefix}_month"] = 12
                st.session_state[f"{key_prefix}_year"] = yr - 1
            else:
                st.session_state[f"{key_prefix}_month"] = mo - 1
            st.rerun()


    with nav2:
        meses_es = ["", "Enero", "Febrero", "Marzo", "Abril",
                    "Mayo", "Junio", "Julio", "Agosto",
                    "Septiembre", "Octubre", "Noviembre", "Diciembre"]
        st.markdown(
            f"<h4 style='text-align:center;margin:0'>{meses_es[mo]} {yr}</h4>",
            unsafe_allow_html=True
        )


    with nav3:
        if st.button("▶", key=f"{key_prefix}_next"):
            if mo == 12:
                st.session_state[f"{key_prefix}_month"] = 1
                st.session_state[f"{key_prefix}_year"] = yr + 1
            else:
                st.session_state[f"{key_prefix}_month"] = mo + 1
            st.rerun()


    dias_semana = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
    hcols = st.columns(7)


    for i, d in enumerate(dias_semana):
        color = "#cc0000" if i >= 5 else "#444444"
        hcols[i].markdown(
            f"<div style='text-align:center;font-weight:bold;color:{color};font-size:13px'>{d}</div>",
            unsafe_allow_html=True
        )


    for semana in calendar.monthcalendar(yr, mo):
        rcols = st.columns(7)
        for i, dia_num in enumerate(semana):
            with rcols[i]:
                if dia_num == 0:
                    st.markdown("<div style='height:38px'></div>", unsafe_allow_html=True)
                    continue


                fecha = datetime.date(yr, mo, dia_num)
                en_periodo = fecha_inicio <= fecha <= fecha_fin
                es_selected = fecha in selected
                es_finde = i >= 5


                if not en_periodo:
                    st.markdown(
                        f"<div style='text-align:center;padding:8px 0;color:#bbbbbb;font-size:12px;border:1px solid #eee;border-radius:4px;background:#fafafa'>{dia_num}</div>",
                        unsafe_allow_html=True
                    )
                else:
                    label = f"🔴 {dia_num}" if es_selected else (f"🔵 {dia_num}" if es_finde else str(dia_num))
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
        "Número = día hábil del período"
        "</div>",
        unsafe_allow_html=True
    )


    if selected:
        if st.button("🗑️ Limpiar todos los feriados", key=f"{key_prefix}_clear"):
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
        type="pdf",
        key="func"
    )


with col2:
    st.markdown("**Book (Biométrico)**")
    # Alterado para aceitar arquivos de texto (.txt) e PDFs
    pdf_bio = st.file_uploader(
        "Archivo con marcaciones del reloj biométrico (PDF o TXT)",
        type=["pdf", "txt"],
        key="bio"
    )


st.write("---")
st.write("### 2. Período de Nómina")
col_tipo, col_d1, col_d2 = st.columns([1, 2, 2])


with col_tipo:
    periodo_tipo = st.selectbox(
        "Tipo de período",
        ["Semanal", "Mensual"],
        help="Semanal: salario × 7 / 30. Mensual: salario × días / 30."
    )


with col_d1:
    fecha_inicio = st.date_input("Fecha de inicio", value=datetime.date(2026, 6, 1))


with col_d2:
    fecha_fin = st.date_input("Fecha de fin", value=datetime.date(2026, 6, 30))


if fecha_inicio > fecha_fin:
    st.error("La fecha de inicio no puede ser posterior a la fecha de fin.")
    st.stop()


st.write("---")
st.write("### 3. Feriados del Período")
st.markdown(
    "Hacé clic en un día para marcarlo como feriado 🔴. "
    "Los feriados **no generan ausencia** si el empleado no trabaja. "
    "Si trabaja en feriado recibe **+100% de bonus**."
)


tab_cal, tab_lista = st.tabs(["📅 Calendario interactivo", "⚡ Agregar por fecha"])


with tab_cal:
    calendario_feriados(fecha_inicio, fecha_fin, key_prefix="cal")


with tab_lista:
    col_f1, col_f2 = st.columns([2, 1])
    with col_f1:
        feriado_nuevo = st.date_input(
            "Agregar feriado",
            value=fecha_inicio,
            key="feriado_input"
        )
    with col_f2:
        st.write("")
        st.write("")
        if st.button("➕ Agregar al calendario"):
            if "cal_selected" not in st.session_state:
                st.session_state["cal_selected"] = set()
            st.session_state["cal_selected"].add(feriado_nuevo)
            st.rerun()


    FERIADOS_PY_2026 = [
        datetime.date(2026, 1, 1), datetime.date(2026, 3, 1),
        datetime.date(2026, 4, 2), datetime.date(2026, 4, 3),
        datetime.date(2026, 5, 1), datetime.date(2026, 5, 15),
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


feriados_lista = sorted(st.session_state.get("cal_selected", set()))
feriados_en_periodo = [f for f in feriados_lista if fecha_inicio <= f <= fecha_fin]


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
            + ", ".join(f.strftime("%d/%m/%Y") for f in feriados_en_periodo)
        )
    else:
        st.info("Ningún feriado marcado cae dentro del período.")
else:
    st.info("No hay feriados marcados aún.")


dias_cal_prev = (fecha_fin - fecha_inicio).days + 1
dias_hab_prev = calcular_dias_habiles(fecha_inicio, fecha_fin, feriados_en_periodo)


st.success(
    f"Período: **{fecha_inicio.strftime('%d/%m/%Y')}** al "
    f"**{fecha_fin.strftime('%d/%m/%Y')}** — "
    f"**{dias_cal_prev} días calendario** — "
    f"**{dias_hab_prev} días hábiles** — "
    f"Tipo: **{periodo_tipo}**"
)


with st.expander("🔢 Verificar lógica con ejemplo numérico"):
    sal_ej = st.number_input(
        "Salario base (Gs.)",
        min_value=0,
        value=3_850_000,
        step=100_000
    )
    if sal_ej > 0:
        vd = sal_ej / 30
        vm = vd / 480
        sp = sal_ej * dias_cal_prev / 30
        st.markdown(
            f"- Valor día: **{format_gs(vd)}**\n"
            f"- Valor minuto: **Gs. {vm:,.1f}**\n"
            f"- Sal. proporcional: **{format_gs(sp)}**\n"
            f"- Sáb mañana 4h: **{format_gs(4 * 60 * vm * 0.5)}** bonus\n"
            f"- Dom/feriado 8h: **{format_gs(8 * 60 * vm * 1.0)}** bonus\n"
            f"- Ausencia 1 día: descuento **{format_gs(vd)}**\n"
            f"- Ausencia 2 días: descuento **{format_gs(4 * vd)}** (penalidad)"
        )


st.write("---")




# ==========================================
# BOTÓN — PROCESAR
# ==========================================
if pdf_func and pdf_bio:
    if st.button("⚙️ Procesar y Calcular Nómina", type="primary", use_container_width=True):
        with st.spinner("Procesando documentos..."):
            try:
                # Processar PDF de funcionários
                pdf_func.seek(0)
                text_func = extract_text_from_pdf(pdf_func)
                if not text_func:
                    st.error("Erro: Não foi possível extrair texto do PDF de Funcionários e Salários. Verifique o PDF.")
                    st.stop()


                # Processar arquivo biométrico (PDF ou TXT)
                pdf_bio.seek(0)
                if pdf_bio.type == "application/pdf":
                    text_bio = extract_text_from_pdf(pdf_bio)
                elif pdf_bio.type == "text/plain":
                    text_bio = pdf_bio.read().decode("utf-8")
                else:
                    st.error("Erro: Tipo de arquivo biométrico não suportado. Por favor, use PDF ou TXT.")
                    st.stop()


                if not text_bio:
                    st.error("Erro: Não foi possível extrair texto do arquivo Biométrico. Verifique o arquivo.")
                    st.stop()


                with st.expander("🔍 Texto extraído PDF de funcionarios"):
                    st.text(text_func[:4000])
                    if len(text_func) > 4000:
                        st.caption("Mostrando apenas os primeiros 4000 caracteres.")


                with st.expander("🔍 Texto extraído biométrico"):
                    st.text(text_bio[:4000])
                    if len(text_bio) > 4000:
                        st.caption("Mostrando apenas os primeiros 4000 caracteres.")


                df_func_data = parse_funcionarios(text_func)


                if df_func_data.empty:
                    st.error(
                        "Não se puderam extrair dados de funcionários. "
                        "Verifique se o PDF de funcionários tem o formato correto."
                    )
                    st.stop()


                with st.expander(f"✅ Funcionários detectados ({len(df_func_data)})"):
                    st.dataframe(df_func_data, use_container_width=True)


                df_bio_data = parse_biometrico(text_bio, df_func_data)


                if df_bio_data.empty:
                    st.error(
                        "Não se detectaram marcações válidas no Livro Biométrico. "
                        "Verifique o formato do arquivo biométrico."
                    )
                    st.stop()


                with st.expander(f"✅ Marcações ({len(df_bio_data)} registros)"):
                    st.dataframe(df_bio_data.head(100), use_container_width=True)
                    if len(df_bio_data) > 100:
                        st.caption("Mostrando apenas os primeiros 100 registros.")




                df_res = calcular_nomina(
                    df_func_data,
                    df_bio_data,
                    fecha_inicio,
                    fecha_fin,
                    periodo_tipo,
                    feriados=feriados_en_periodo,
                )


                if df_res.empty:
                    st.error("Não se geraram resultados de nómina.")
                    st.stop()


                st.session_state["resultados"] = df_res
                st.session_state["feriados_usados"] = feriados_en_periodo




                st.success(
                    f"✅ Cálculo completado — "
                    f"{len(df_res)} funcionários — "
                    f"{len(feriados_en_periodo)} feriado(s) aplicados."
                )




            except Exception as e:
                st.error(f"Erro inesperado durante o processamento: {e}")
                st.exception(e)
else:
    st.info("⬆️ Suba ambos os documentos para habilitar o cálculo.")




# ==========================================
# REVISIÓN E EXPORTACIÓN
# ==========================================
if ("resultados" in st.session_state and not st.session_state["resultados"].empty):
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
            "Salario Proporcional", "Desc. Ausencias",
            "Desc. Atrasos", "Monto OT Diurna", "Monto OT Nocturna",
            "Monto Bonus Sáb Mañana", "Monto Bonus Descanso",
            "Total Imponible", "IPS (9%)", "Salario Líquido",
        ]],
        use_container_width=True,
    )


    st.write("#### Totales del Período")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Imponible", format_gs(df_res["Total Imponible"].sum()))
    c2.metric("Total IPS Obrero", format_gs(df_res["IPS (9%)"].sum()))
    c3.metric("Total Líquido", format_gs(df_res["Salario Líquido"].sum()))
    c4.metric("Funcionarios", str(len(df_res)))


    st.write("---")
    st.write("### 5. Exportar Holerites")
    with st.spinner("Generando PDFs..."):
        try:
            zip_bytes = generar_zip(df_res)


            fi_str = fecha_inicio.strftime("%Y%m%d")
            ff_str = fecha_fin.strftime("%Y%m%d")


            st.download_button(
                label="⬇️ Descargar Holerites en PDF (ZIP)",
                data=zip_bytes,
                file_name=f"Holerites_WiegandBrito_{fi_str}_{ff_str}.zip",
                mime="application/zip",
                type="primary",
                use_container_width=True,
            )


            st.caption(
                "Cada PDF contiene dos copias (empresa / funcionario) "
                "separadas por línea de corte. "
                "Todos los montos en Guaraníes (Gs.)."
            )
        except Exception as e:
            st.error(f"Erro ao gerar o arquivo ZIP dos holerites: {e}")
            st.exception(e)
