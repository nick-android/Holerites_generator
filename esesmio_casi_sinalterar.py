import streamlit as st
import pandas as pd
import pdfplumber
import datetime
import re
from io import BytesIO
import zipfile
from fpdf import FPDF

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
            match_inline = re.search(r'(.+?)\s+(\d+(?:-\d+)?)\s+PYG\s+([\d.]+)', cleaned, re.IGNORECASE)
            
            if match_inline:
                nombre = match_inline.group(1).strip()
                ci = match_inline.group(2).strip()
                salario = float(match_inline.group(3).replace('.', ''))
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


def calcular_nomina(df_func, df_bio):
    resultados = []
    if df_bio.empty:
        return pd.DataFrame()
        
    dict_cargos = df_bio.groupby('Nomb.')['Cargo'].first().to_dict()
    
    for _, empleado in df_func.iterrows():
        nombre = empleado['NOMBRE Y APELLIDO']
        salario_base = empleado['SALARIO REAJUSTADO']
        valor_minuto = salario_base / (30 * 8 * 60)
        cargo_final = dict_cargos.get(nombre, "Funcionario")
        
        marcaciones_empleado = df_bio[df_bio['Nomb.'] == nombre]
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

            if fechas_unicas:
                week_starts = {fecha - pd.Timedelta(days=fecha.weekday()) for fecha in fechas_unicas}
                for week_start in sorted(week_starts):
                    absent_days = 0
                    for d in range(5):
                        current_day = week_start + pd.Timedelta(days=d)
                        if current_day not in day_summary:
                            absent_days += 1
                    deduction_days = ABSENCE_DEDUCTION_DAYS.get(absent_days, 7)
                    total_absence_hours += deduction_days * 8
        else:
            # If no biometric records at all, treat the full week(s) as absent
            total_absence_hours = 0
            if not df_bio.empty:
                dates = df_bio['Tiempo'].dt.date.unique()
                if len(dates) > 0:
                    first_date = min(dates)
                    last_date = max(dates)
                    current = first_date - pd.Timedelta(days=first_date.weekday())
                    end = last_date
                    while current <= end:
                        absent_days = 5
                        deduction_days = ABSENCE_DEDUCTION_DAYS.get(absent_days, 7)
                        total_absence_hours += deduction_days * 8
                        current += pd.Timedelta(days=7)

        total_absence_minutes = total_absence_hours * 60
        desc_absences = total_absence_minutes * valor_minuto
        desc_lateness = total_lateness * valor_minuto
        monto_e50 = total_e50 * valor_minuto * 1.5
        monto_e100 = total_e100 * valor_minuto * 2.0
        
        imponible = salario_base - desc_absences - desc_lateness + monto_e50 + monto_e100
        ips = imponible * TASA_IPS
        liquido = imponible - ips
        
        resultados.append({
            'Funcionario': nombre,
            'CI Nº': empleado['CI Nº'],
            'Cargo': cargo_final,
            'Salario Base': salario_base,
            'Ausencias (Hrs)': round(total_absence_hours, 2),
            'Atrasos (Hrs)': round(total_lateness / 60, 2),
            'Descuento Ausencias': round(desc_absences),
            'Descuento Atrasos': round(desc_lateness),
            'Extras 50% (Mins)': total_e50,
            'Monto Extra 50%': round(monto_e50),
            'Extras 100% (Mins)': total_e100,
            'Monto Extra 100%': round(monto_e100),
            'IPS (9%)': round(ips),
            'Salario Líquido Final': round(liquido)
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

        add_row('1', 'Salario Base', '30 días', data['Salario Base'], 0)
        
        if data['Monto Extra 50%'] > 0:
            add_row('2', 'Horas Extras 50%', f"{data['Extras 50% (Mins)']/60:.1f} hrs", data['Monto Extra 50%'], 0)
        if data['Monto Extra 100%'] > 0:
            add_row('3', 'Horas Extras 100%', f"{data['Extras 100% (Mins)']/60:.1f} hrs", data['Monto Extra 100%'], 0)
        if data['Descuento Ausencias'] > 0:
            add_row('4', 'Ausencias', f"{data['Ausencias (Hrs)']:.2f} hrs", 0, data['Descuento Ausencias'])
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
                        df_resultados = calcular_nomina(df_funcionarios, df_biometrico)
                        st.session_state['resultados'] = df_resultados
                        st.success(f"Cálculos completados con éxito. Se procesaron {len(df_resultados)} funcionarios.")
            except Exception as e:
                st.error(f"Error durante el procesamiento. Detalle: {e}")

if 'resultados' in st.session_state and not st.session_state['resultados'].empty:
    st.write("---")
    st.write("### 2. Pantalla de Revisión Previa (Obligatoria)")
    st.dataframe(
        st.session_state['resultados'][['Funcionario', 'Cargo', 'Ausencias (Hrs)', 'Atrasos (Hrs)', 'Descuento Ausencias', 'Descuento Atrasos', 'Monto Extra 50%', 'Monto Extra 100%', 'IPS (9%)', 'Salario Líquido Final']],
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
    