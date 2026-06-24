import streamlit as st
import pandas as pd
import pdfplumber
import datetime
from io import BytesIO
import zipfile
from fpdf import FPDF

# ==========================================
# CONFIGURACIÓN DE LA UI (UX/UI)
# ==========================================
st.set_page_config(page_title="Generador de Nómina y Holerites", layout="wide")
st.title("💸 Sistema Automatizado de Nómina - Paraguay")
st.markdown("Procesa sueldos, calcula horas extras/faltas bajo la ley laboral y genera Holerites (Recibos).")

# ==========================================
# CONSTANTES Y REGLAS DE NEGOCIO
# ==========================================
HORAS_MENSUALES = 190.66 # Promedio mensual para 44h semanales ((44 * 52) / 12)
MINUTOS_JORNADA = 9 * 60 # 9 horas diarias
TASA_IPS = 0.09

# ==========================================
# FUNCIONES DE EXTRACCIÓN DE DATOS
# ==========================================
def extraer_datos_funcionarios(pdf_file):
    """Extrae y limpia datos del PDF de funcionarios."""
    datos = []
    with pdfplumber.open(pdf_file) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if table:
                for row in table[1:]: # Saltar cabecera
                    datos.append(row)
    
    # Asumiendo el orden exacto de columnas o buscándolas por índice
    # Para robustez, requeriremos que el PDF genere este DF básico:
    df = pd.DataFrame(datos, columns=['NOMBRE Y APELLIDO', 'CI Nº', 'CARGO', 'SALARIO REAJUSTADO', 'OBSERVACIÓN'])
    
    # Limpieza
    df = df.dropna(subset=['NOMBRE Y APELLIDO'])
    df = df[~df['OBSERVACIÓN'].str.contains('FORA', na=False, case=False)]
    df['SALARIO REAJUSTADO'] = df['SALARIO REAJUSTADO'].replace({r'\.': '', ',': '.'}, regex=True).astype(float)
    return df

def extraer_datos_biometrico(pdf_file):
    """Extrae marcaciones del reloj biométrico."""
    datos = []
    with pdfplumber.open(pdf_file) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if table:
                for row in table[1:]:
                    datos.append(row)
                    
    df = pd.DataFrame(datos, columns=['Nomb.', 'Tiempo'])
    df['Tiempo'] = pd.to_datetime(df['Tiempo'], format='%d/%m/%Y %H:%M:%S', errors='coerce')
    df = df.dropna(subset=['Tiempo'])
    return df

# ==========================================
# MOTOR DE CÁLCULO DE TIEMPO
# ==========================================
def clasificar_minutos(marcaciones_dia):
    """
    Recibe lista de datetimes ordenados para UN empleado en UN día.
    Simula el día minuto a minuto para clasificar faltas y extras exactas.
    """
    if len(marcaciones_dia) % 2 != 0:
        marcaciones_dia = marcaciones_dia[:-1] # Ignorar última marcación sin salida
        
    minutos_trabajados = []
    for i in range(0, len(marcaciones_dia), 2):
        inicio = marcaciones_dia[i]
        fin = marcaciones_dia[i+1]
        actual = inicio
        while actual < fin:
            minutos_trabajados.append(actual)
            actual += pd.Timedelta(minutes=1)
            
    total_minutos = len(minutos_trabajados)
    faltas_mins, extra50_mins, extra100_mins = 0, 0, 0
    
    if total_minutos < MINUTOS_JORNADA:
        faltas_mins = MINUTOS_JORNADA - total_minutos
    elif total_minutos > MINUTOS_JORNADA:
        # Solo los minutos que exceden las 9 horas
        minutos_excedentes = minutos_trabajados[MINUTOS_JORNADA:]
        for m in minutos_excedentes:
            dia_semana = m.weekday() # 0=Lunes, 6=Domingo
            hora = m.hour
            
            if dia_semana == 6 or (dia_semana == 5 and hora >= 12):
                extra100_mins += 1
            elif dia_semana < 5 and hora >= 22:
                extra100_mins += 1
            elif dia_semana < 5 and 18 <= hora < 22:
                extra50_mins += 1
            else:
                extra50_mins += 1 # Default para extras diurnas
                
    return faltas_mins, extra50_mins, extra100_mins

def calcular_nomina(df_func, df_bio):
    resultados = []
    
    for _, empleado in df_func.iterrows():
        nombre = empleado['NOMBRE Y APELLIDO']
        salario_base = empleado['SALARIO REAJUSTADO']
        valor_minuto = salario_base / (HORAS_MENSUALES * 60)
        
        # Filtrar marcaciones del empleado
        marcaciones = df_bio[df_bio['Nomb.'] == nombre]['Tiempo'].dt.date.unique()
        
        total_faltas, total_e50, total_e100 = 0, 0, 0
        
        for fecha in marcaciones:
            # Marcaciones del día específico para el empleado
            marcs_dia = df_bio[(df_bio['Nomb.'] == nombre) & (df_bio['Tiempo'].dt.date == fecha)]['Tiempo'].sort_values().tolist()
            f_mins, e50_mins, e100_mins = clasificar_minutos(marcs_dia)
            total_faltas += f_mins
            total_e50 += e50_mins
            total_e100 += e100_mins
            
        # Cálculos Monetarios
        desc_faltas = total_faltas * valor_minuto
        monto_e50 = total_e50 * valor_minuto * 1.5
        monto_e100 = total_e100 * valor_minuto * 2.0
        
        salario_imponible = salario_base - desc_faltas + monto_e50 + monto_e100
        ips = salario_imponible * TASA_IPS
        salario_liquido = salario_imponible - ips
        
        resultados.append({
            'Funcionario': nombre,
            'CI Nº': empleado['CI Nº'],
            'Cargo': empleado['CARGO'],
            'Salario Base': salario_base,
            'Faltas/Tardanzas (Mins)': total_faltas,
            'Descuento Faltas': round(desc_faltas),
            'Extras 50% (Mins)': total_e50,
            'Monto Extra 50%': round(monto_e50),
            'Extras 100% (Mins)': total_e100,
            'Monto Extra 100%': round(monto_e100),
            'IPS (9%)': round(ips),
            'Salario Líquido Final': round(salario_liquido)
        })
        
    return pd.DataFrame(resultados)

# ==========================================
# GENERADOR DE PDF (DISEÑO ESTRICTO HOLERITE)
# ==========================================
class PDFHolerite(FPDF):
    def crear_recibo(self, data, y_offset=0):
        # Datos Empresa
        self.set_xy(10, 10 + y_offset)
        self.set_font('helvetica', 'B', 12)
        self.cell(100, 6, 'WIEGAND BRITO', 0, 1)
        self.set_font('helvetica', '', 9)
        self.set_x(10)
        self.cell(100, 5, 'RUC: 80000000-1 (Ejemplo)', 0, 0)
        
        # Fecha de Ejecución
        fecha_actual = datetime.datetime.now().strftime("%d/%m/%Y")
        self.set_xy(140, 10 + y_offset)
        self.cell(60, 5, f'Período / Fecha: {fecha_actual}', 0, 1, 'R')
        
        self.line(10, 22 + y_offset, 200, 22 + y_offset)
        
        # Datos Empleado
        self.set_xy(10, 25 + y_offset)
        self.set_font('helvetica', 'B', 10)
        self.cell(100, 5, f"Funcionario: {data['Funcionario']}", 0, 1)
        self.set_font('helvetica', '', 9)
        self.cell(100, 5, f"C.I. Nº: {data['CI Nº']}   |   Cargo: {data['Cargo']}", 0, 1)
        
        self.line(10, 37 + y_offset, 200, 37 + y_offset)
        
        # Cabecera de Tabla
        self.set_xy(10, 40 + y_offset)
        self.set_font('helvetica', 'B', 8)
        self.cell(15, 6, 'Cód', 1)
        self.cell(85, 6, 'Descrição', 1)
        self.cell(20, 6, 'Ref.', 1)
        self.cell(35, 6, 'A Recibir (Gs.)', 1)
        self.cell(35, 6, 'Descuentos (Gs.)', 1, 1)
        
        self.set_font('helvetica', '', 8)
        
        # Fila: Salario Base
        self.cell(15, 6, '001', 'L')
        self.cell(85, 6, 'Salario Mensual', 'L')
        self.cell(20, 6, '30 días', 'L')
        self.cell(35, 6, f"{data['Salario Base']:,.0f}".replace(',','_').replace('.',',').replace('_','.'), 'L')
        self.cell(35, 6, '', 'L, R', 1)
        
        # Fila: Horas Extras 50%
        if data['Monto Extra 50%'] > 0:
            hrs = data['Extras 50% (Mins)'] / 60
            self.cell(15, 6, '002', 'L')
            self.cell(85, 6, 'Horas Extras 50%', 'L')
            self.cell(20, 6, f"{hrs:.1f} hrs", 'L')
            self.cell(35, 6, f"{data['Monto Extra 50%']:,.0f}".replace(',','_').replace('.',',').replace('_','.'), 'L')
            self.cell(35, 6, '', 'L, R', 1)
            
        # Fila: Horas Extras 100%
        if data['Monto Extra 100%'] > 0:
            hrs = data['Extras 100% (Mins)'] / 60
            self.cell(15, 6, '003', 'L')
            self.cell(85, 6, 'Horas Extras 100%', 'L')
            self.cell(20, 6, f"{hrs:.1f} hrs", 'L')
            self.cell(35, 6, f"{data['Monto Extra 100%']:,.0f}".replace(',','_').replace('.',',').replace('_','.'), 'L')
            self.cell(35, 6, '', 'L, R', 1)
            
        # Fila: Faltas y Tardanzas
        if data['Descuento Faltas'] > 0:
            hrs = data['Faltas/Tardanzas (Mins)'] / 60
            self.cell(15, 6, '010', 'L')
            self.cell(85, 6, 'Faltas y Atrasos', 'L')
            self.cell(20, 6, f"{hrs:.1f} hrs", 'L')
            self.cell(35, 6, '', 'L')
            self.cell(35, 6, f"{data['Descuento Faltas']:,.0f}".replace(',','_').replace('.',',').replace('_','.'), 'L, R', 1)
            
        # Fila: IPS
        self.cell(15, 6, '020', 'L, B')
        self.cell(85, 6, 'Aporte Obrero IPS (9%)', 'L, B')
        self.cell(20, 6, '9%', 'L, B')
        self.cell(35, 6, '', 'L, B')
        self.cell(35, 6, f"{data['IPS (9%)']:,.0f}".replace(',','_').replace('.',',').replace('_','.'), 'L, R, B', 1)
        
        # Totales
        self.set_y(85 + y_offset)
        self.set_font('helvetica', 'B', 10)
        self.cell(120, 8, 'LÍQUIDO A COBRAR:', 1)
        self.cell(70, 8, f"Gs. {data['Salario Líquido Final']:,.0f}".replace(',','_').replace('.',',').replace('_','.'), 1, 1, 'R')
        
        # Firma
        self.set_xy(100, 110 + y_offset)
        self.line(100, 120 + y_offset, 190, 120 + y_offset)
        self.set_xy(100, 122 + y_offset)
        self.set_font('helvetica', '', 8)
        self.cell(90, 5, 'Firma del Funcionario', 0, 1, 'C')

def generar_holerites_zip(df_resultados):
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
        for _, row in df_resultados.iterrows():
            pdf = PDFHolerite()
            pdf.add_page()
            
            # Dibujar mitad superior (Empresa)
            pdf.crear_recibo(row, y_offset=0)
            
            # Dibujar línea de corte punteada
            pdf.set_draw_color(150, 150, 150)
            for x in range(10, 200, 5):
                pdf.line(x, 148.5, x+2, 148.5)
            pdf.set_draw_color(0, 0, 0)
            
            # Dibujar mitad inferior (Empleado)
            pdf.crear_recibo(row, y_offset=148.5)
            
            # Guardar en buffer PDF
            pdf_buffer = pdf.output(dest='S').encode('latin1')
            
            # Añadir al ZIP
            nombre_archivo = f"Holerite_{row['Funcionario'].replace(' ', '_')}.pdf"
            zip_file.writestr(nombre_archivo, pdf_buffer)
            
    return zip_buffer.getvalue()

# ==========================================
# INTERFAZ DE USUARIO (STREAMLIT)
# ==========================================
st.write("### 1. Zona de Carga de Datos")
col1, col2 = st.columns(2)

with col1:
    pdf_func = st.file_uploader("Cargar PDF de Funcionarios y Sueldos", type="pdf")
with col2:
    pdf_bio = st.file_uploader("Cargar PDF de Horarios (Reloj Biométrico)", type="pdf")

if pdf_func and pdf_bio:
    if st.button("Procesar Archivos y Calcular"):
        with st.spinner("Extrayendo datos y procesando leyes laborales..."):
            try:
                df_funcionarios = extraer_datos_funcionarios(pdf_func)
                df_biometrico = extraer_datos_biometrico(pdf_bio)
                
                df_resultados = calcular_nomina(df_funcionarios, df_biometrico)
                st.session_state['resultados'] = df_resultados
                st.success("Cálculos completados con éxito.")
            except Exception as e:
                st.error(f"Error al procesar los PDFs. Revisa la estructura. Detalle: {e}")

# Revisión Previa
if 'resultados' in st.session_state:
    st.write("---")
    st.write("### 2. Pantalla de Revisión Previa (Obligatoria)")
    st.dataframe(
        st.session_state['resultados'][['Funcionario', 'Descuento Faltas', 'Monto Extra 50%', 'Monto Extra 100%', 'IPS (9%)', 'Salario Líquido Final']],
        use_container_width=True
    )
    
    st.write("---")
    st.write("### 3. Exportación")
    
    # Botón de Descarga
    zip_data = generar_holerites_zip(st.session_state['resultados'])
    st.download_button(
        label="✅ Confirmar y Generar Holerites en PDF (ZIP)",
        data=zip_data,
        file_name=f"Holerites_Nomina_{datetime.datetime.now().strftime('%Y%m%d')}.zip",
        mime="application/zip",
    )