import streamlit as st
import pandas as pd
import pdfplumber
import datetime
import re
from io import BytesIO
import zipfile
from fpdf import FPDF
import numpy as np

# ==========================================
# CONFIGURACIÓN DE LA UI
# ==========================================
st.set_page_config(page_title="Nómina y Holerites - Wiegand Brito", layout="wide")
st.title("💸 Sistema Automatizado de Nómina y Holerites")
st.markdown("Procesa sueldos, calcula horas extras/faltas bajo la ley laboral y genera recibos oficiales.")

# ==========================================
# CONSTANTES LABORALES
# ==========================================
HORAS_MENSUALES = 190.66  # Promedio mensual (44h semanales)
MINUTOS_JORNADA = 8 * 60  # 8 horas pagadas diarias
WORKING_DAYS_MONTHLY = 26  # Días hábiles estándar en mes paraguayo
ABSENCE_DEDUCTION_DAYS = {
    0: 0,
    1: 1,
    2: 4,
    3: 5,
    4: 6,
    5: 7
}
TASA_IPS = 0.09

# ==========================================
# FUNCIONES DE UTILIDAD - FECHAS Y CÁLCULOS
# ==========================================
def calcular_dias_habiles(fecha_inicio, fecha_fin):
    """
    Calcula el número de días hábiles (lunes a viernes) en un período.
    
    Args:
        fecha_inicio: datetime.date o datetime.datetime
        fecha_fin: datetime.date o datetime.datetime
    
    Returns:
        int: Número de días hábiles
    """
    # Convertir a numpy datetime64 para usar busday_count
    fecha_inicio_np = np.datetime64(fecha_inicio, 'D')
    fecha_fin_np = np.datetime64(fecha_fin, 'D')
    # busday_count cuenta desde fecha_inicio (inclusive) hasta fecha_fin (exclusive)
    # por lo que sumamos 1 para incluir la fecha_fin
    dias_habiles = int(np.busday_count(fecha_inicio_np, fecha_fin_np + np.timedelta64(1, 'D')))
    return dias_habiles

# ==========================================
# MOTOR DE EXTRACCIÓN AVANZADO
# ==========================================
def extract_text_from_pdf(pdf_file):
    text = ""
    with pdfplumber.open(pdf_file) as pdf:
        for page in pdf.pages:
            extracted = page.extract_text()
            if extracted:
                text += extracted + "\n"
    return text

def parse_funcionarios(text):
    """Extrae datos de funcionarios de forma híbrida (en línea o apilados)."""
    funcionarios = []
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    
    for i, line in enumerate(lines):
        if "PYG" in line:
            # Limpiar fila para análisis de una sola línea
            cleaned = line.replace('"', '').replace("'", '').strip()
            # Actualizar regex para incluir commas en el formato de salario (3.850.000,00)
            match_inline = re.search(r'(.+?)\s+(\d+(?:-\d+)?)\s+PYG\s+([\d.,]+)', cleaned, re.IGNORECASE)
            
            if match_inline:
                nombre = match_inline.group(1).strip()
                ci = match_inline.group(2).strip()
                # Extraer salario: remover puntos de miles y convertir coma decimal a punto
                salario_str = match_inline.group(3).replace('.', '').replace(',', '.')
                salario = float(salario_str) if salario_str else 0
                funcionarios.append({
                    'NOMBRE Y APELLIDO': nombre,
                    'CI Nº': ci,
                    'SALARIO REAJUSTADO': salario
                })
            else:
                # Fallback: Buscar hacia arriba si están en líneas separadas
                try:
                    salario_str = line.replace("PYG", "").strip().split(',')[0].replace('.', '').replace(',', '.')
                    salario = float(salario_str)
                    ci = lines[i-1].replace('"', '').replace(',', '').strip()
                    nombre = lines[i-2].replace('"', '').replace(',', '').strip()
                    
                    if not re.search(r'\d', ci) and i >= 3:
                        ci = lines[i-2].replace('"', '').replace(',', '').strip()
                        nombre = lines[i-3].replace('"', '').replace(',', '').strip()
                        
                    if "FUNC" not in nombre.upper() and "FORA" not in nombre.upper():
                        funcionarios.append({
                            'NOMBRE Y APELLIDO': nombre,
                            'CI Nº': ci,
                            'SALARIO REAJUSTADO': salario
                        })
                except Exception:
                    continue
                    
    return pd.DataFrame(funcionarios)

def parse_biometrico(text, df_funcionarios):
    """Agrupa las marcaciones usando límites estrictos para evitar solapamiento de nombres."""
    # Si no hay datos válidos de funcionarios, no intentar parsear el biométrico
    if df_funcionarios is None or 'NOMBRE Y APELLIDO' not in df_funcionarios.columns or df_funcionarios.empty:
        return pd.DataFrame()

    roster = df_funcionarios['NOMBRE Y APELLIDO'].tolist()
    date_pattern = r'\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}'
    
    matches = list(re.finditer(date_pattern, text))
    marcaciones = []
    
    for idx, match in enumerate(matches):
        fecha_str = match.group(0)
        start_pos = match.start()
        end_pos = match.end()
        
        # Límites estrictos para que no lea contexto del registro vecino
        prev_bound = matches[idx-1].end() if idx > 0 else 0
        next_bound = matches[idx+1].start() if idx < len(matches) - 1 else len(text)
        
        left_chunk = text[max(prev_bound, start_pos - 80):start_pos]
        right_chunk = text[end_pos:min(next_bound, end_pos + 80)]
        contexto = (left_chunk + " " + right_chunk).lower().replace('"', '').replace('\n', ' ')
        
        cargo = "Funcionario"
        if "drywall" in contexto:
            cargo = "104-Montador DryWall"
        elif "steel" in contexto:
            cargo = "105-Montador Steel"
            
        mejor_nombre = None
        max_score = 0
        
        for emp in roster:
            parts = [p for p in emp.lower().split() if len(p) > 2]
            score = 0
            
            # Prioridad a coincidencias combinadas de nombre + apellido
            if len(parts) >= 2:
                for i in range(len(parts)-1):
                    if f"{parts[i]} {parts[i+1]}" in contexto:
                        score += 5
                        
            for p in parts:
                if p in contexto:
                    score += 2
                    
            if "guillermo" in emp.lower() and "guilhermo" in contexto:
                score += 4
                
            if score > max_score and score > 0:
                max_score = score
                mejor_nombre = emp
                
        if mejor_nombre:
            marcaciones.append({
                'Nomb.': mejor_nombre,
                'Tiempo': pd.to_datetime(fecha_str, format='%d/%m/%Y %H:%M:%S'),
                'Cargo': cargo
            })
            
    return pd.DataFrame(marcaciones)

# ==========================================
# LÓGICA LABORAL
# ==========================================
def clasificar_minutos(marcaciones_dia, weekday):
    if len(marcaciones_dia) % 2 != 0:
        marcaciones_dia = marcaciones_dia[:-1]
        
    minutos_trabajados = []
    for i in range(0, len(marcaciones_dia), 2):
        inicio = marcaciones_dia[i]
        fin = marcaciones_dia[i+1]
        while inicio < fin:
            minutos_trabajados.append(inicio)
            inicio += pd.Timedelta(minutes=1)
            
    total_minutos = len(minutos_trabajados)
    lateness_mins, e50_mins, e100_mins = 0, 0, 0

    if weekday in (5, 6):
        # Todo el tiempo de fin de semana es extra según reglas
        for m in minutos_trabajados:
            if weekday == 6 or (weekday == 5 and m.hour >= 12):
                e100_mins += 1
            else:
                e50_mins += 1
    else:
        if total_minutos < MINUTOS_JORNADA:
            lateness_mins = MINUTOS_JORNADA - total_minutos
            if lateness_mins < 5:
                lateness_mins = 0
        elif total_minutos > MINUTOS_JORNADA:
            for m in minutos_trabajados[MINUTOS_JORNADA:]:
                dia_semana, hora = m.weekday(), m.hour
                if dia_semana == 6 or (dia_semana == 5 and hora >= 12) or (dia_semana < 5 and hora >= 22):
                    e100_mins += 1
                else:
                    e50_mins += 1

    return total_minutos, lateness_mins, e50_mins, e100_mins


def calcular_nomina(df_func, df_bio, fecha_inicio=None, fecha_fin=None):
    """
    Calcula la nómina de empleados.
    
    Args:
        df_func: DataFrame de funcionarios
        df_bio: DataFrame de biometría
        fecha_inicio: datetime.date - Inicio del período (opcional, usa default si no se proporciona)
        fecha_fin: datetime.date - Fin del período (opcional, usa default si no se proporciona)
    
    Returns:
        pd.DataFrame: Resultados de cálculos
    """
    resultados = []
    if df_bio.empty:
        return pd.DataFrame()
    
    # Si no se proporcionan fechas, usar las fechas del biométrico
    if fecha_inicio is None or fecha_fin is None:
        if not df_bio.empty:
            fecha_inicio = df_bio['Tiempo'].min().date()
            fecha_fin = df_bio['Tiempo'].max().date()
        else:
            return pd.DataFrame()
    
    # Convertir a date si son datetime
    if isinstance(fecha_inicio, datetime.datetime):
        fecha_inicio = fecha_inicio.date()
    if isinstance(fecha_fin, datetime.datetime):
        fecha_fin = fecha_fin.date()
    
    # Calcular días hábiles en el período
    dias_habiles_periodo = calcular_dias_habiles(fecha_inicio, fecha_fin)
    
    # Asegurar un mínimo de 1 día hábil para evitar división por cero
    dias_habiles_periodo = max(dias_habiles_periodo, 1)
    
    dict_cargos = df_bio.groupby('Nomb.')['Cargo'].first().to_dict()
    
    # Filtrar biometría por el período seleccionado
    df_bio_periodo = df_bio[(df_bio['Tiempo'].dt.date >= fecha_inicio) & 
                             (df_bio['Tiempo'].dt.date <= fecha_fin)]
    
    for _, empleado in df_func.iterrows():
        nombre = empleado['NOMBRE Y APELLIDO']
        salario_base = empleado['SALARIO REAJUSTADO']
        
        period_days = (fecha_fin - fecha_inicio).days + 1
        is_one_week = period_days == 7 and dias_habiles_periodo == 5

        if is_one_week:
            salario_proporcional = salario_base / 4
            valor_minuto = salario_proporcional / (7 * 8 * 60)
        else:
            salario_proporcional = salario_base * (dias_habiles_periodo / WORKING_DAYS_MONTHLY)
            valor_minuto = salario_proporcional / (dias_habiles_periodo * 8 * 60)

        cargo_final = dict_cargos.get(nombre, "Funcionario")

        # Filtrar marcaciones del empleado solo para el período seleccionado
        marcaciones_empleado = df_bio_periodo[df_bio_periodo['Nomb.'] == nombre]
        total_lateness, total_e50, total_e100 = 0, 0, 0
        total_absence_hours = 0

        if not marcaciones_empleado.empty:
            fechas_unicas = sorted(marcaciones_empleado['Tiempo'].dt.date.unique())
            day_summary = {}
            for fecha in fechas_unicas:
                marcs_dia = marcaciones_empleado[marcaciones_empleado['Tiempo'].dt.date == fecha]['Tiempo'].sort_values().tolist()
                total_minutos, lateness, e50, e100 = clasificar_minutos(marcs_dia, pd.Timestamp(fecha).weekday())
                total_lateness += lateness
                total_e50 += e50
                total_e100 += e100
                day_summary[fecha] = {
                    'total_minutos': total_minutos,
                    'lateness': lateness
                }
        else:
            day_summary = {}

        # Calcular el pago base por días trabajados y los fines de semana según ausencia
        salary_base_pay = 0
        weekend_pay = 0
        current_week_dt = pd.Timestamp(fecha_inicio) - pd.Timedelta(days=pd.Timestamp(fecha_inicio).weekday())
        end_period_dt = pd.Timestamp(fecha_fin)
        
        while current_week_dt.date() <= fecha_fin:
            week_worked = 0
            week_expected = 0
            for d in range(5):
                current_day = (current_week_dt + pd.Timedelta(days=d)).date()
                if current_day < fecha_inicio or current_day > fecha_fin:
                    continue
                week_expected += 1
                if current_day in day_summary:
                    week_worked += 1
                    salary_base_pay += valor_minuto * 8 * 60
            absent_days = week_expected - week_worked
            deduction_days = ABSENCE_DEDUCTION_DAYS.get(absent_days, 7)
            total_absence_hours += deduction_days * 8

            weekend_days_in_period = 0
            for d in range(5, 7):
                weekend_day = (current_week_dt + pd.Timedelta(days=d)).date()
                if weekend_day >= fecha_inicio and weekend_day <= fecha_fin:
                    weekend_days_in_period += 1
            if absent_days < 2:
                weekend_pay += weekend_days_in_period * valor_minuto * 8 * 60

            current_week_dt += pd.Timedelta(days=7)

        salary_base_pay += weekend_pay
        total_absence_minutes = total_absence_hours * 60
        desc_absences = total_absence_minutes * valor_minuto
        desc_lateness = total_lateness * valor_minuto
        monto_e50 = total_e50 * valor_minuto * 1.5
        monto_e100 = total_e100 * valor_minuto * 2.0

        imponible = salary_base_pay - desc_absences - desc_lateness + monto_e50 + monto_e100
        ips = imponible * TASA_IPS
        liquido = imponible - ips

        # Reemplazar valores negativos con 0
        desc_absences = max(0, desc_absences)
        desc_lateness = max(0, desc_lateness)
        monto_e50 = max(0, monto_e50)
        monto_e100 = max(0, monto_e100)
        ips = max(0, ips)
        liquido = max(0, liquido)
        imponible = max(0, imponible)
        
        resultados.append({
            'Funcionario': nombre,
            'CI Nº': empleado['CI Nº'],
            'Cargo': cargo_final,
            'Salario Base': salario_base,
            'Salario Proporcional': salario_proporcional,
            'Ausencias (Días)': round(total_absence_hours / 8, 2),
            'Atrasos (Hrs)': round(total_lateness / 60, 2),
            'Descuento Ausencias': round(desc_absences),
            'Descuento Atrasos': round(desc_lateness),
            'Extras 50% (Mins)': total_e50,
            'Monto Extra 50%': round(monto_e50),
            'Extras 100% (Mins)': total_e100,
            'Monto Extra 100%': round(monto_e100),
            'IPS (9%)': round(ips),
            'Salario Líquido Final': round(liquido),
            'Fecha Inicio': fecha_inicio,
            'Fecha Fin': fecha_fin,
            'Días Hábiles': dias_habiles_periodo
        })
        
    return pd.DataFrame(resultados)

# ==========================================
# MOTOR PDF (RÉPLICA EXACTA DE LA PLANTILLA)
# ==========================================
class PDFHolerite(FPDF):
    def __init__(self):
        super().__init__(orientation='P', unit='mm', format='A4')
        self.set_auto_page_break(auto=False)
        
    def format_gs(self, monto):
        if monto <= 0: return ""
        return "Gs. " + f"{monto:,.0f}".replace(',','_').replace('.',',').replace('_','.')

    def draw_receipt_half(self, data, is_empresa=False, offset_y=0):
        # Construir el período a partir de fecha_inicio y fecha_fin si existen
        if 'Fecha Inicio' in data and 'Fecha Fin' in data:
            fecha_inicio = data['Fecha Inicio']
            fecha_fin = data['Fecha Fin']
            if isinstance(fecha_inicio, datetime.datetime):
                fecha_inicio = fecha_inicio.date()
            if isinstance(fecha_fin, datetime.datetime):
                fecha_fin = fecha_fin.date()
            periodo_actual = f"{fecha_inicio.strftime('%d/%m/%Y')} a {fecha_fin.strftime('%d/%m/%Y')}"
        else:
            # Fallback al comportamiento anterior
            meses = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
            periodo_actual = f"{meses[datetime.datetime.now().month]}/{datetime.datetime.now().year}"
        
        tipo_comprobante = "COMPROVANTE EMPRESA" if is_empresa else "COMPROVANTE FUNCIONARIO"
        
        # ENCABEZADO
        self.set_xy(10, 10 + offset_y)
        self.set_font('helvetica', 'B', 8)
        self.cell(50, 4, 'Empresa', 0, 1)
        self.set_font('helvetica', '', 10)
        self.cell(50, 5, 'WIEGAND BRITO', 0, 0)
        
        self.set_xy(100, 10 + offset_y)
        self.set_font('helvetica', 'B', 9)
        self.cell(100, 5, f'RECIBO DE PAGO DE SALÁRIO - {tipo_comprobante}', 0, 1, 'R')
        
        self.set_xy(130, 16 + offset_y)
        self.set_font('helvetica', 'B', 8)
        self.cell(35, 4, 'RUC', 0, 0)
        self.cell(35, 4, 'Período', 0, 1, 'R')
        self.set_xy(130, 20 + offset_y)
        self.set_font('helvetica', '', 9)
        self.cell(35, 5, '9230303-0', 0, 0)
        self.cell(35, 5, periodo_actual, 0, 1, 'R')
        
        self.line(10, 27 + offset_y, 200, 27 + offset_y)
        
        # EMPLEADO
        self.set_xy(10, 29 + offset_y)
        self.set_font('helvetica', 'B', 8)
        self.cell(100, 4, 'Funcionário', 0, 1)
        self.set_font('helvetica', '', 10)
        self.cell(100, 5, str(data['Funcionario']).upper(), 0, 1)
        
        self.set_xy(130, 29 + offset_y)
        self.set_font('helvetica', 'B', 8)
        self.cell(70, 4, 'CARGO', 0, 1)
        self.set_xy(130, 33 + offset_y)
        self.set_font('helvetica', '', 10)
        self.cell(70, 5, str(data['Cargo']).upper(), 0, 1)
        
        self.line(10, 40 + offset_y, 200, 40 + offset_y)
        
        # TABLA DE HABERES
        self.set_xy(10, 42 + offset_y)
        self.set_font('helvetica', 'B', 8)
        self.cell(10, 6, 'Cód.', 1, 0, 'C')
        self.cell(85, 6, 'Descrição', 1, 0, 'C')
        self.cell(25, 6, 'Ref.', 1, 0, 'C')
        self.cell(35, 6, 'A RECIBIR', 1, 0, 'C')
        self.cell(35, 6, 'DESCUENTOS', 1, 1, 'C')
        
        self.set_font('helvetica', '', 8)
        
        def add_row(cod, desc, ref, recibir, descuento):
            self.cell(10, 6, str(cod), 'L,R', 0, 'C')
            self.cell(85, 6, desc, 'L,R')
            self.cell(25, 6, str(ref), 'L,R', 0, 'C')
            self.cell(35, 6, self.format_gs(recibir), 'L,R', 0, 'R')
            self.cell(35, 6, self.format_gs(descuento), 'L,R', 1, 'R')

        # Determinar el reference para el salario base
        dias_ref = f"{data.get('Días Hábiles', 26)} días" if 'Días Hábiles' in data else "30 días"
        add_row('1', 'Salario Base', dias_ref, data['Salario Proporcional'] if 'Salario Proporcional' in data else data['Salario Base'], 0)
        
        if data['Monto Extra 50%'] > 0:
            add_row('2', 'Horas Extras 50%', f"{data['Extras 50% (Mins)']/60:.1f} hrs", data['Monto Extra 50%'], 0)
        if data['Monto Extra 100%'] > 0:
            add_row('3', 'Horas Extras 100%', f"{data['Extras 100% (Mins)']/60:.1f} hrs", data['Monto Extra 100%'], 0)
        if data['Descuento Ausencias'] > 0:
            add_row('4', 'Ausencias', f"{data['Ausencias (Días)']:.2f} días", 0, data['Descuento Ausencias'])
        if data['Descuento Atrasos'] > 0:
            add_row('5', 'Atrasos', f"{data['Atrasos (Hrs)']:.2f} hrs", 0, data['Descuento Atrasos'])
        add_row('7', 'Descuento IPS', '9% s/ Imponible', 0, data['IPS (9%)'])
        
        while self.get_y() < 90 + offset_y:
            add_row('', '', '', 0, 0)
            
        # FOOTER TABLA
        self.set_font('helvetica', 'B', 9)
        self.cell(155, 8, 'SALARIO LÍQUIDO ->', 1, 0, 'R')
        self.cell(35, 8, self.format_gs(data['Salario Líquido Final']), 1, 1, 'R')
        
        # SECCIÓN OBS
        self.set_xy(10, 102 + offset_y)
        self.set_font('helvetica', 'B', 8)
        self.cell(100, 4, 'OBS: PAGO EFECTUADO EN LA CUENTA DE:', 0, 1)
        self.set_font('helvetica', '', 8)
        self.cell(100, 4, str(data['Funcionario']).upper(), 0, 1)
        self.cell(100, 4, f"CI: {data['CI Nº']}", 0, 1)
        
        # FIRMAS
        y_firma = 125 + offset_y
        self.set_xy(10, y_firma)
        self.cell(15, 5, 'FECHA:', 0, 0)
        self.cell(35, 5, '_____/_____/_______', 0, 0)
        
        self.set_xy(75, y_firma)
        self.cell(30, 5, 'FIRMA EMPRESA:', 0, 0)
        self.line(105, y_firma+4, 135, y_firma+4)
        
        self.set_xy(145, y_firma)
        self.cell(25, 5, 'FIRMA FUNC.:', 0, 0)
        self.line(168, y_firma+4, 198, y_firma+4)

def generar_holerites_zip(df_resultados):
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
        for _, row in df_resultados.iterrows():
            pdf = PDFHolerite()
            pdf.add_page()
            
            # Mitad 1: Empresa
            pdf.draw_receipt_half(row, is_empresa=True, offset_y=0)
            
            # Línea Punteada de Recorte
            pdf.set_draw_color(150, 150, 150)
            for x in range(10, 200, 5):
                pdf.line(x, 148.5, x+2, 148.5)
            pdf.set_draw_color(0, 0, 0)
            
            # Mitad 2: Funcionario
            pdf.draw_receipt_half(row, is_empresa=False, offset_y=148.5)
            
            # Compatibilidad universal de FPDF2 para retornar Bytes
            try:
                pdf_buffer = pdf.output()
                if isinstance(pdf_buffer, str):
                    pdf_buffer = pdf_buffer.encode('latin1')
            except TypeError:
                pdf_buffer = pdf.output(dest='S').encode('latin1')
                
            nombre_archivo = f"Holerite_{row['Funcionario'].replace(' ', '_')}.pdf"
            zip_file.writestr(nombre_archivo, pdf_buffer)
            
    return zip_buffer.getvalue()

# ==========================================
# INTERFAZ STREAMLIT
# ==========================================
st.write("### 1. Zona de Carga de Datos")
col1, col2 = st.columns(2)

with col1:
    pdf_func = st.file_uploader("Cargar PDF de Funcionarios y Sueldos", type="pdf")
with col2:
    pdf_bio = st.file_uploader("Cargar PDF de Horarios (Reloj Biométrico)", type="pdf")

# Sección de Período de Nómina
st.write("### 1b. Período de Nómina")
col_date1, col_date2 = st.columns(2)

with col_date1:
    fecha_inicio = st.date_input("Fecha de Inicio del Período", value=datetime.date.today() - datetime.timedelta(days=30))
with col_date2:
    fecha_fin = st.date_input("Fecha de Fin del Período", value=datetime.date.today())

if pdf_func and pdf_bio:
    if st.button("Procesar Archivos y Calcular", type="primary"):
        with st.spinner("Leyendo estructura libre y calculando..."):
            try:
                text1 = extract_text_from_pdf(pdf_func)
                text2 = extract_text_from_pdf(pdf_bio)

                # Detectar si los archivos fueron cargados en el orden equivocado
                df_func1 = parse_funcionarios(text1)
                df_func2 = parse_funcionarios(text2)

                if df_func1.empty and not df_func2.empty:
                    text_func, text_bio = text2, text1
                    df_funcionarios = df_func2
                else:
                    text_func, text_bio = text1, text2
                    df_funcionarios = df_func1

                if df_funcionarios.empty:
                    st.error("No se pudieron extraer datos de la lista de funcionarios. Verifica el PDF de funcionarios y sueldos.")
                else:
                    df_biometrico = parse_biometrico(text_bio, df_funcionarios)
                    if df_biometrico.empty:
                        st.error("No se detectaron marcaciones válidas en el biométrico. Verifica el PDF de horarios.")
                    else:
                        df_resultados = calcular_nomina(df_funcionarios, df_biometrico, fecha_inicio, fecha_fin)
                        st.session_state['resultados'] = df_resultados
                        st.success(f"Cálculos completados con éxito. Se procesaron {len(df_resultados)} funcionarios.")
            except Exception as e:
                st.error(f"Error durante el procesamiento. Detalle: {e}")

if 'resultados' in st.session_state and not st.session_state['resultados'].empty:
    st.write("---")
    st.write("### 2. Pantalla de Revisión Previa (Obligatoria)")
    
    # Crear una copia para mostrar con formato de guaranies
    df_display = st.session_state['resultados'].copy()
    
    # Columnas monetarias a formatear
    columnas_monetarias = ['Salario Base', 'Salario Proporcional', 'Descuento Ausencias', 'Descuento Atrasos', 
                           'Monto Extra 50%', 'Monto Extra 100%', 'IPS (9%)', 'Salario Líquido Final']
    
    # Formatear como guaranies
    for col in columnas_monetarias:
        if col in df_display.columns:
            df_display[col] = df_display[col].apply(lambda x: f"Gs. {int(x):,}".replace(',', '.'))
    
    st.dataframe(
        df_display[['Funcionario', 'Cargo', 'Ausencias (Días)', 'Atrasos (Hrs)', 'Descuento Ausencias', 'Descuento Atrasos', 'Monto Extra 50%', 'Monto Extra 100%', 'IPS (9%)', 'Salario Líquido Final']],
    )
    
    st.write("---")
    st.write("### 3. Exportación")
    zip_data = generar_holerites_zip(st.session_state['resultados'])
    st.download_button(
        label="✅ Confirmar y Generar Holerites en PDF (ZIP)",
        data=zip_data,
        file_name=f"Holerites_Nomina_{datetime.datetime.now().strftime('%Y%m%d')}.zip",
        mime="application/zip",
        type="primary"
    )
    