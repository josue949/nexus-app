import io
import os
import re
import secrets
import streamlit as st
import pandas as pd
from datetime import date, datetime, timedelta
from streamlit_js_eval import get_geolocation, streamlit_js_eval

from database import (
    init_db, get_session, Student, Subject, Attendance,
    CaseTracker, Schedule, Justification, User, calculate_distance, ActaAsistencia,
    QRAttendanceToken, QRAttendanceCheckin, CicloEscolar, Matricula,
    hash_password, verify_password
)
from analytics import (
    evaluate_student_risk, get_institutional_semaphore, evaluate_student_risk_detailed,
    sugerir_acuerdos_compromisos, get_alertas_docente, sugerir_normativa_aplicable
)
from ai_recommender import generate_recommendation, generate_pedagogical_act

# Coordenadas de prueba del Instituto y radio máximo permitido (en metros)
INSTITUTE_LAT = 13.35054
INSTITUTE_LON = -88.34890
MAX_DISTANCE_METERS = 200.0

# --- IMPORTANTE: cambia esto por la URL real donde esté publicada tu app ---
# Se usa para construir el enlace que se codifica dentro del QR de asistencia.
# Ejemplos: "https://tu-app.streamlit.app" o "http://192.168.1.50:8501" en tu red local.
APP_BASE_URL = "http://localhost:8501"

QR_TOKEN_VALIDEZ_MINUTOS = 5

# Bloques de clase (sin recesos), compartidos por la detección de materia actual
# y la reconciliación de ausencias — mismos horarios que usa el módulo de Horarios.
MEDIOS_BLOQUES_HORARIO = [
    {"id": 1, "ini": "07:00", "fin": "07:45"}, {"id": 2, "ini": "07:45", "fin": "08:30"},
    {"id": 3, "ini": "08:40", "fin": "09:25"}, {"id": 4, "ini": "09:25", "fin": "10:10"},
    {"id": 5, "ini": "10:20", "fin": "11:05"}, {"id": 6, "ini": "11:05", "fin": "11:50"},
    {"id": 7, "ini": "13:00", "fin": "13:45"}, {"id": 8, "ini": "13:45", "fin": "14:30"},
    {"id": 9, "ini": "14:40", "fin": "15:25"}, {"id": 10, "ini": "15:25", "fin": "16:10"},
    {"id": 11, "ini": "16:20", "fin": "17:10"}, {"id": 12, "ini": "17:10", "fin": "17:50"},
]

# Módulo de Horarios integrado localmente para la nube
FASTAPI_HORARIOS_URL = "modo_integrado"


def generar_acta_asistencia_pdf(session, estudiante, docente_actual,
                                acuerdos_finales=None, compromisos_finales=None, acta_anterior=None):
    """Genera un Acta de Asistencia en PDF para un estudiante: resumen de asistencia,
    detalle de inasistencias/permisos/tardanzas con fecha, los patrones de riesgo
    detectados por analytics.evaluate_student_risk_detailed (mismo motor del semáforo
    institucional), acuerdos/compromisos, y comparación de tendencia contra el acta
    anterior de ese mismo estudiante (si existe)."""
    try:
        from fpdf import FPDF
    except ImportError:
        raise RuntimeError(
            "Falta instalar la librería 'fpdf2'. Ejecuta en tu terminal: pip install fpdf2"
        )

    def limpiar_texto_pdf(texto):
        """Los fonts base de fpdf2 (Helvetica) solo soportan Latin-1: tildes/ñ están
        bien, pero emojis u otros símbolos Unicode rompen la generación. Se reemplazan
        por '?' en vez de dejar que el PDF falle."""
        if texto is None:
            return "-"
        return str(texto).encode("latin-1", errors="replace").decode("latin-1")

    resultado_detallado = evaluate_student_risk_detailed(session, estudiante.id)
    status_riesgo = resultado_detallado['status']
    pct_asistencia = resultado_detallado['pct']
    patrones = resultado_detallado['patterns']

    mapa_riesgo_texto = {'🟢': 'BAJO', '🟡': 'MEDIO', '🔴': 'ALTO'}
    riesgo_texto = mapa_riesgo_texto.get(status_riesgo, 'N/D')

    thirty_days_ago = date.today() - timedelta(days=30)
    registros = session.query(Attendance).filter(
        Attendance.student_id == estudiante.id,
        Attendance.date >= thirty_days_ago
    ).order_by(Attendance.date.asc()).all()

    total = len(registros)
    presentes = sum(1 for r in registros if r.status == 'Presente')
    tardanzas = sum(1 for r in registros if r.status == 'Tardanza')
    ausentes = sum(1 for r in registros if r.status == 'Ausente')
    permisos = sum(1 for r in registros if r.status == 'Permiso')

    normativa_aplicable = sugerir_normativa_aplicable(resultado_detallado['tags'], ausentes, permisos)

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "ACTA DE ASISTENCIA Y SEGUIMIENTO ESTUDIANTIL", ln=True, align="C")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"Generada el {datetime.now().strftime('%d/%m/%Y %H:%M')}", ln=True, align="C")
    pdf.ln(6)

    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Datos del Estudiante", ln=True)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 7, f"Nombre: {limpiar_texto_pdf(estudiante.name)}", ln=True)
    pdf.cell(0, 7, f"NIE: {estudiante.id}", ln=True)
    pdf.cell(0, 7, f"Sección: {limpiar_texto_pdf(estudiante.section)}", ln=True)
    pdf.cell(0, 7, f"Docente Orientador: {limpiar_texto_pdf(getattr(docente_actual, 'username', '-'))}", ln=True)
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Resumen de Asistencia (Últimos 30 días)", ln=True)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 7, f"Porcentaje de Asistencia: {pct_asistencia}%", ln=True)
    pdf.cell(0, 7, f"Nivel de Riesgo: {riesgo_texto}", ln=True)
    pdf.cell(0, 7,
             f"Total de registros: {total}   |   Presentes: {presentes}   |   "
             f"Tardanzas: {tardanzas}   |   Ausencias: {ausentes}   |   Permisos: {permisos}",
             ln=True)
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Patrones Detectados", ln=True)
    pdf.set_font("Helvetica", "", 11)
    if patrones:
        for p in patrones:
            pdf.multi_cell(0, 6, f"- {limpiar_texto_pdf(p)}", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.cell(0, 7, "No se detectaron patrones de riesgo en el período evaluado.", ln=True)
    pdf.ln(4)

    if normativa_aplicable:
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, "Marco Normativo Aplicable (Manual de Convivencia INDET 2025)", ln=True)
        pdf.set_font("Helvetica", "", 10)
        for norma in normativa_aplicable:
            pdf.set_font("Helvetica", "B", 10)
            pdf.multi_cell(0, 6, limpiar_texto_pdf(f"Nivel: {norma['nivel']} — {norma['articulo']}"),
                          new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 5, limpiar_texto_pdf(norma['texto']), new_x="LMARGIN", new_y="NEXT")
            pdf.multi_cell(0, 5, limpiar_texto_pdf(f"Clasificación: {norma['clasificacion']}"),
                          new_x="LMARGIN", new_y="NEXT")
            pdf.multi_cell(0, 5, limpiar_texto_pdf(f"Sanción/acción sugerida: {norma['sancion_sugerida']}"),
                          new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
        pdf.ln(2)

    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Detalle de Inasistencias, Tardanzas y Permisos", ln=True)

    registros_relevantes = [r for r in registros if r.status in ('Ausente', 'Permiso', 'Tardanza')]

    if registros_relevantes:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(35, 7, "Fecha", border=1)
        pdf.cell(30, 7, "Estado", border=1)
        pdf.cell(0, 7, "Observación", border=1, ln=True)
        pdf.set_font("Helvetica", "", 10)
        for r in registros_relevantes:
            obs = limpiar_texto_pdf(r.observation or "-")
            if len(obs) > 60:
                obs = obs[:57] + "..."
            pdf.cell(35, 7, r.date.strftime('%d/%m/%Y'), border=1)
            pdf.cell(30, 7, r.status, border=1)
            pdf.cell(0, 7, obs, border=1, ln=True)
    else:
        pdf.set_font("Helvetica", "", 11)
        pdf.cell(0, 7, "Sin inasistencias, tardanzas ni permisos registrados en el período.", ln=True)

    pdf.ln(4)

    # --- Seguimiento respecto al acta anterior (si existe) ---
    if acta_anterior is not None:
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, "Seguimiento Respecto al Acta Anterior", ln=True)
        pdf.set_font("Helvetica", "", 11)
        pdf.cell(0, 7, f"Acta anterior generada el: {acta_anterior.fecha_generacion.strftime('%d/%m/%Y')}", ln=True)
        if acta_anterior.pct_asistencia_snapshot is not None:
            diff = pct_asistencia - acta_anterior.pct_asistencia_snapshot
            if diff > 2:
                texto_tendencia = (f"MEJORO: de {acta_anterior.pct_asistencia_snapshot}% a {pct_asistencia}% "
                                   f"de asistencia (+{diff:.1f} puntos)")
            elif diff < -2:
                texto_tendencia = (f"EMPEORO: de {acta_anterior.pct_asistencia_snapshot}% a {pct_asistencia}% "
                                   f"de asistencia ({diff:.1f} puntos)")
            else:
                texto_tendencia = (f"SE MANTUVO similar: de {acta_anterior.pct_asistencia_snapshot}% a "
                                   f"{pct_asistencia}% de asistencia")
            pdf.multi_cell(0, 6, limpiar_texto_pdf(texto_tendencia), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

    # --- Acuerdos y compromisos ---
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Acuerdos del Estudiante", ln=True)
    pdf.set_font("Helvetica", "", 11)
    acuerdos_validos = [a for a in (acuerdos_finales or []) if a and a.strip()]
    if acuerdos_validos:
        for a in acuerdos_validos:
            pdf.multi_cell(0, 6, f"- {limpiar_texto_pdf(a.strip())}", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.cell(0, 7, "(Sin acuerdos registrados)", ln=True)
    pdf.ln(3)

    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Compromisos Institucionales", ln=True)
    pdf.set_font("Helvetica", "", 11)
    compromisos_validos = [c for c in (compromisos_finales or []) if c and c.strip()]
    if compromisos_validos:
        for c in compromisos_validos:
            pdf.multi_cell(0, 6, f"- {limpiar_texto_pdf(c.strip())}", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.cell(0, 7, "(Sin compromisos registrados)", ln=True)

    pdf.ln(16)
    pdf.set_font("Helvetica", "", 10)
    col_firma_w = 90
    pdf.cell(col_firma_w, 6, "_________________________", align="C")
    pdf.cell(col_firma_w, 6, "_________________________", align="C", ln=True)
    pdf.cell(col_firma_w, 6, "Firma del Estudiante / Responsable", align="C")
    pdf.cell(col_firma_w, 6, "Firma del Docente Orientador", align="C", ln=True)

    salida = pdf.output()
    return bytes(salida)

# =============================================================================
# IDENTIDAD VISUAL NEXUS — logo + paleta de colores extraída del logo oficial
# =============================================================================
# Coloca el archivo del logo (el que me compartiste) en la misma carpeta que
# este app.py, con el nombre "logo_nexus.png" (o cambia la ruta de abajo).
LOGO_PATH = "logo_nexus.png"

_page_icon = "🎓"
if os.path.exists(LOGO_PATH):
    try:
        from PIL import Image as _PILImage
        _page_icon = _PILImage.open(LOGO_PATH)
    except Exception:
        pass

st.set_page_config(page_title="NEXUS", page_icon=_page_icon, layout="wide")

if os.path.exists(LOGO_PATH):
    st.logo(LOGO_PATH, icon_image=LOGO_PATH)

CSS_NEXUS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600;700&family=Inter:wght@400;500;600;700&display=swap');

:root{
    --nexus-navy: #0B1F4D;
    --nexus-navy-dark: #071433;
    --nexus-navy-light: #1E3A73;
    --nexus-gold: #C9972E;
    --nexus-gold-light: #E4C77A;
    --nexus-bg: #F7F8FB;
    --nexus-white: #FFFFFF;
}

html, body, [data-testid="stAppViewContainer"]{
    background-color: var(--nexus-bg);
    background-image: radial-gradient(circle at 15% 10%, rgba(11,31,77,0.035) 0%, transparent 45%),
                       radial-gradient(circle at 85% 90%, rgba(201,151,46,0.05) 0%, transparent 45%);
    font-family: 'Inter', sans-serif;
}

/* --- Scrollbar personalizado --- */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: var(--nexus-bg); }
::-webkit-scrollbar-thumb {
    background: var(--nexus-navy-light);
    border-radius: 10px;
    border: 2px solid var(--nexus-bg);
}
::-webkit-scrollbar-thumb:hover { background: var(--nexus-gold); }

/* --- Tarjeta de login --- */
.nexus-login-card {
    background: var(--nexus-white);
    border-radius: 16px;
    padding: 2rem 2rem 1rem 2rem;
    margin-top: 3rem;
    box-shadow: 0 12px 32px rgba(11, 31, 77, 0.14);
    border-top: 4px solid var(--nexus-gold);
    animation: nexusFadeIn 0.5s ease-out;
}

[data-testid="stHeader"]{
    background-color: transparent;
}

/* --- Títulos con la tipografía serif del logo --- */
h1, h2, h3 {
    font-family: 'Playfair Display', serif !important;
    color: var(--nexus-navy) !important;
    letter-spacing: 0.3px;
}
h4, h5, h6 {
    color: var(--nexus-navy) !important;
    font-family: 'Inter', sans-serif;
}

/* --- Animación de entrada suave para el contenido principal --- */
[data-testid="stAppViewContainer"] > .main {
    animation: nexusFadeIn 0.45s ease-out;
}
@keyframes nexusFadeIn {
    from { opacity: 0; transform: translateY(8px); }
    to   { opacity: 1; transform: translateY(0); }
}

/* --- Botones --- */
.stButton > button, .stFormSubmitButton > button, .stDownloadButton > button {
    background: linear-gradient(135deg, var(--nexus-navy) 0%, var(--nexus-navy-light) 100%);
    color: var(--nexus-white) !important;
    border: 1px solid var(--nexus-gold);
    border-radius: 8px;
    font-weight: 600;
    padding: 0.5rem 1.1rem;
    transition: transform 0.15s ease, box-shadow 0.15s ease, background 0.25s ease;
    box-shadow: 0 2px 6px rgba(11, 31, 77, 0.15);
}
.stButton > button:hover, .stFormSubmitButton > button:hover, .stDownloadButton > button:hover {
    transform: translateY(-2px);
    box-shadow: 0 6px 16px rgba(11, 31, 77, 0.28);
    background: linear-gradient(135deg, var(--nexus-gold) 0%, var(--nexus-gold-light) 100%);
    color: var(--nexus-navy-dark) !important;
    border-color: var(--nexus-navy);
}
.stButton > button:active{ transform: translateY(0); }

/* --- Botón primario (type="primary") con más protagonismo dorado --- */
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {
    background: linear-gradient(135deg, var(--nexus-gold) 0%, #B9822A 100%);
    color: var(--nexus-navy-dark) !important;
    border: 1px solid var(--nexus-navy);
}
.stButton > button[kind="primary"]:hover {
    background: linear-gradient(135deg, var(--nexus-navy) 0%, var(--nexus-navy-light) 100%);
    color: var(--nexus-white) !important;
}

/* --- Pestañas (st.tabs) --- */
[data-testid="stTabs"] button[data-baseweb="tab"] {
    font-weight: 600;
    color: var(--nexus-navy-light);
    transition: color 0.2s ease;
}
[data-testid="stTabs"] button[data-baseweb="tab"]:hover {
    color: var(--nexus-gold);
}
[data-testid="stTabs"] button[aria-selected="true"] {
    color: var(--nexus-navy) !important;
}
[data-testid="stTabs"] [data-baseweb="tab-highlight"] {
    background-color: var(--nexus-gold) !important;
    height: 3px !important;
}

/* --- Tarjetas / contenedores con borde (st.container(border=True)) --- */
[data-testid="stVerticalBlockBorderWrapper"] {
    border-radius: 12px !important;
    border: 1px solid rgba(11, 31, 77, 0.12) !important;
    transition: box-shadow 0.2s ease, transform 0.2s ease;
}
[data-testid="stVerticalBlockBorderWrapper"]:hover {
    box-shadow: 0 6px 18px rgba(11, 31, 77, 0.12);
}

/* --- Métricas --- */
[data-testid="stMetric"] {
    background: var(--nexus-white);
    border-left: 4px solid var(--nexus-gold);
    border-radius: 10px;
    padding: 0.6rem 0.9rem;
    box-shadow: 0 2px 8px rgba(11, 31, 77, 0.06);
}
[data-testid="stMetricValue"] { color: var(--nexus-navy); }

/* --- Alertas (success/info/warning/error) con entrada animada --- */
[data-testid="stAlert"] {
    border-radius: 10px;
    animation: nexusSlideIn 0.3s ease-out;
}
@keyframes nexusSlideIn {
    from { opacity: 0; transform: translateX(-6px); }
    to   { opacity: 1; transform: translateX(0); }
}

/* --- Barra de progreso / spinner con acento dorado --- */
.stSpinner > div { border-top-color: var(--nexus-gold) !important; }

/* --- Inputs, selects, textareas --- */
.stTextInput input, .stTextArea textarea, .stSelectbox div[data-baseweb="select"],
.stDateInput input, .stNumberInput input {
    border-radius: 8px !important;
}
.stTextInput input:focus, .stTextArea textarea:focus {
    border-color: var(--nexus-gold) !important;
    box-shadow: 0 0 0 1px var(--nexus-gold) !important;
}

/* --- Dataframes / tablas --- */
[data-testid="stDataFrame"] {
    border-radius: 10px;
    overflow: hidden;
    box-shadow: 0 2px 10px rgba(11, 31, 77, 0.08);
    border: 1px solid rgba(11, 31, 77, 0.08);
}

/* --- Transición suave al cambiar de pestaña (Streamlit vuelve a montar el
       panel en cada rerun, así que la animación se repite en cada cambio) --- */
[data-baseweb="tab-panel"] {
    animation: nexusFadeIn 0.35s ease-out;
}

/* --- Pulso para alertas de riesgo ALTO (badge 🔴) --- */
.nexus-badge-pulse {
    animation: nexusPulse 1.8s ease-in-out infinite;
}
@keyframes nexusPulse {
    0%   { box-shadow: 0 0 0 0 rgba(217, 57, 76, 0.45); }
    70%  { box-shadow: 0 0 0 8px rgba(217, 57, 76, 0); }
    100% { box-shadow: 0 0 0 0 rgba(217, 57, 76, 0); }
}

/* --- Skeleton loader (shimmer) para mientras cargan tablas/PDFs --- */
.nexus-skel-line {
    height: 14px;
    border-radius: 6px;
    margin: 6px 0;
    background: linear-gradient(90deg, #E7EAF2 25%, #F3F5F9 37%, #E7EAF2 63%);
    background-size: 400% 100%;
    animation: nexusShimmer 1.4s ease-in-out infinite;
}
@keyframes nexusShimmer {
    0%   { background-position: 100% 50%; }
    100% { background-position: 0% 50%; }
}

/* =====================================================================
   TARJETAS TECNOLÓGICAS (glassmorphism + borde con gradiente animado)
   Usadas en las pantallas de selección de módulo / opciones por rol.
   ===================================================================== */
.nexus-tech-card-wrap {
    position: relative;
    border-radius: 18px;
    padding: 2px;
    background: linear-gradient(120deg, var(--nexus-navy), var(--nexus-gold), var(--nexus-navy-light), var(--nexus-gold));
    background-size: 300% 300%;
    animation: nexusGradientShift 6s ease infinite;
    transition: transform 0.25s ease, box-shadow 0.25s ease;
}
.nexus-tech-card-wrap:hover {
    transform: translateY(-6px) scale(1.012);
    box-shadow: 0 16px 34px rgba(11, 31, 77, 0.28);
}
@keyframes nexusGradientShift {
    0%   { background-position: 0% 50%; }
    50%  { background-position: 100% 50%; }
    100% { background-position: 0% 50%; }
}

.nexus-tech-card {
    position: relative;
    border-radius: 16px;
    padding: 1.6rem 1.5rem 1.8rem 1.5rem;
    background: rgba(255, 255, 255, 0.72);
    backdrop-filter: blur(10px);
    -webkit-backdrop-filter: blur(10px);
    overflow: hidden;
}
.nexus-tech-card::after {
    content: "";
    position: absolute;
    top: -40%; left: -40%;
    width: 60%; height: 180%;
    background: linear-gradient(120deg, transparent, rgba(201,151,46,0.18), transparent);
    transform: rotate(20deg);
    transition: left 0.6s ease;
    pointer-events: none;
}
.nexus-tech-card-wrap:hover .nexus-tech-card::after {
    left: 120%;
}

.nexus-tech-card-icon {
    font-size: 2.4rem;
    display: inline-block;
    animation: nexusFloat 3s ease-in-out infinite;
    filter: drop-shadow(0 4px 8px rgba(11,31,77,0.18));
}
@keyframes nexusFloat {
    0%, 100% { transform: translateY(0px); }
    50%      { transform: translateY(-6px); }
}

.nexus-tech-card-title {
    font-family: 'Playfair Display', serif;
    font-weight: 700;
    font-size: 1.25rem;
    color: var(--nexus-navy);
    margin: 0.5rem 0 0.4rem 0;
}
.nexus-tech-card-desc {
    color: #4a5a7a;
    font-size: 0.92rem;
    line-height: 1.4rem;
}

/* --- Tarjeta completa como enlace clicable (sin botón aparte) --- */
a.nexus-tech-card-link {
    text-decoration: none !important;
    display: block;
    cursor: pointer;
}
a.nexus-tech-card-link:hover .nexus-tech-card-arrow {
    transform: translateX(6px);
    opacity: 1;
}
.nexus-tech-card-arrow {
    display: inline-block;
    margin-top: 0.9rem;
    font-weight: 700;
    color: var(--nexus-gold);
    opacity: 0.75;
    transition: transform 0.25s ease, opacity 0.25s ease;
}

/* --- Expander --- */
[data-testid="stExpander"] {
    border-radius: 10px !important;
    border: 1px solid rgba(11, 31, 77, 0.12) !important;
}

/* --- Separadores más sutiles y elegantes --- */
hr { border-color: rgba(201, 151, 46, 0.35) !important; }

/* --- Balloons/confetti no se tocan: son de Streamlit y ya animan solos --- */
</style>
"""
st.markdown(CSS_NEXUS, unsafe_allow_html=True)

@st.cache_resource
def _ejecutar_init_db_una_sola_vez():
    """init_db() corre las migraciones (Base.metadata.create_all + varios ALTER TABLE
    con commit/rollback) — son ~15-20 viajes de ida y vuelta a la base de datos. Sin
    este cache, Streamlit las repetiría en CADA clic (porque re-ejecuta todo el script
    en cada interacción), lo cual hacía que la app se sintiera muy lenta contra una
    base de datos remota como Neon. @st.cache_resource garantiza que esta función se
    ejecute una sola vez por proceso del servidor, sin importar cuántas reruns ocurran
    después ni cuántos usuarios la usen a la vez."""
    init_db()
    return True


try:
    _ejecutar_init_db_una_sola_vez()
    session = get_session()
    # Rehidratar usuario desde user_id si está guardado
    if st.session_state.get('user') is None and st.session_state.get('user_id') is not None:
        try:
            user_obj = session.query(User).get(st.session_state['user_id'])
            st.session_state['user'] = user_obj
        except Exception as e:
            st.warning('No se pudo cargar el usuario guardado')
            st.session_state['user'] = None
            st.session_state['user_id'] = None
except Exception as e:
    st.error(
        "❌ No se pudo conectar con la base de datos. Esto puede ser temporal (el servidor de base de "
        "datos puede estar 'despertando' tras un rato de inactividad) — espera unos segundos y recarga "
        "la página. Si sigue pasando, avisa al administrador del sistema.")
    with st.expander("Detalle técnico (para el administrador)"):
        st.code(str(e))
    st.stop()

# Manejo de Sesión / Estado
if 'user' not in st.session_state:
    st.session_state['user'] = None
if 'user_id' not in st.session_state:
    st.session_state['user_id'] = None

if 'admin_view' not in st.session_state:
    st.session_state['admin_view'] = 'select_ciclo'  # Opciones: 'select_ciclo', 'dashboard', 'horarios', 'asistencia'


# -----------------------------------------------------------------------------
# AUTENTICACIÓN / LOGIN
# -----------------------------------------------------------------------------
if st.session_state['user'] is None:
    col_l1, col_l2, col_l3 = st.columns([1, 1.3, 1])
    with col_l2:
        st.markdown('<div class="nexus-tech-card-wrap" style="margin-top:2.5rem;">'
                    '<div class="nexus-tech-card" style="padding:2rem 2rem 1rem 2rem;">',
                    unsafe_allow_html=True)
        if os.path.exists(LOGO_PATH):
            col_logo1, col_logo2, col_logo3 = st.columns([1, 1.4, 1])
            with col_logo2:
                st.image(LOGO_PATH, width='stretch')
        st.markdown(
            '<h2 style="text-align:center; margin-top:0.2rem;">Acceso al Sistema</h2>',
            unsafe_allow_html=True
        )
        st.markdown(
            '<p style="text-align:center; color:#5a6b8c; margin-top:-0.6rem;">'
            'Calidad e Innovación Educativa</p>',
            unsafe_allow_html=True
        )

        username_input = st.text_input("Usuario", key="login_user_input")
        password_input = st.text_input("Contraseña", type="password", key="login_pass_input")

        if st.button("Ingresar", type="primary", width='stretch', key="login_submit_btn"):
            try:
                user_found = session.query(User).filter_by(username=username_input).first()
                if user_found and verify_password(password_input, user_found.password):
                    st.session_state['user'] = user_found
                    st.session_state['user_id'] = user_found.id
                    st.session_state['admin_view'] = 'select_ciclo'
                    st.success(f"Bienvenido {user_found.username} ({user_found.role})")
                    st.rerun()
                else:
                    st.error("Usuario o contraseña incorrectos")
            except Exception:
                st.error(
                    "❌ No se pudo conectar con el sistema en este momento. Espera unos segundos y vuelve "
                    "a intentar — si el problema sigue, avisa al administrador.")

        st.caption("🔒 **Credenciales de Prueba:**")
        st.caption(
            "- **Admin:** `admin` / `123` | **Docente Orientador:** `profe` / `123` | **Alumno:** `juan` / `123`")
        st.markdown('</div></div>', unsafe_allow_html=True)
    st.stop()

NEXUS_RIESGO_BADGE = {
    "🟢": {"icon": "🟢", "label": "RIESGO BAJO", "bg": "#E6F6EA", "fg": "#1E7A34", "border": "#4CAF50"},
    "🟡": {"icon": "🟡", "label": "RIESGO MEDIO", "bg": "#FFF6E0", "fg": "#8A6200", "border": "#E0A800"},
    "🔴": {"icon": "🔴", "label": "RIESGO ALTO", "bg": "#FDEAEA", "fg": "#A6273A", "border": "#D9394C"},
}


def render_badge_riesgo(status_emoji, pct=None):
    cfg = NEXUS_RIESGO_BADGE.get(status_emoji, NEXUS_RIESGO_BADGE["🟢"])
    pct_html = f'<span style="opacity:0.75; font-weight:500;"> · {pct}% asistencia</span>' if pct is not None else ""
    clase_pulso = "nexus-badge-pulse" if status_emoji == "🔴" else ""
    st.markdown(f"""
    <div class="{clase_pulso}" style="display:inline-flex; align-items:center; gap:0.5rem;
                background:{cfg['bg']}; color:{cfg['fg']}; border:1.5px solid {cfg['border']};
                border-radius:999px; padding:0.4rem 1rem; font-weight:700; font-size:0.95rem;
                margin:0.3rem 0; box-shadow:0 2px 6px rgba(11,31,77,0.08);">
        <span style="font-size:1.1rem;">{cfg['icon']}</span>
        <span>{cfg['label']}</span>
        {pct_html}
    </div>
    """, unsafe_allow_html=True)


def mostrar_skeleton(placeholder, n_lineas=5, titulo=None):
    """Dibuja un skeleton loader (shimmer) dentro de un st.empty() mientras se
    calcula/consulta algo que tarda un momento perceptible (tablas grandes, PDFs)."""
    lineas_html = "".join(
        f'<div class="nexus-skel-line" style="width:{95 - (i % 3) * 12}%;"></div>'
        for i in range(n_lineas)
    )
    titulo_html = f'<div style="font-weight:600; color:#5a6b8c; margin-bottom:0.5rem;">{titulo}</div>' if titulo else ""
    placeholder.markdown(f'<div>{titulo_html}{lineas_html}</div>', unsafe_allow_html=True)


class _CicloActivoCache:
    """Objeto ligero (no un modelo de SQLAlchemy) para devolver el ciclo activo
    cacheado, evitando reusar un objeto ORM que quedaría 'desconectado' de la
    sesión de base de datos en la siguiente rerun."""
    def __init__(self, id_, nombre):
        self.id = id_
        self.nombre = nombre


def detectar_materia_actual_horarios(seccion_nombre):
    """Consulta el módulo de Horarios (SQLite) para averiguar qué materia le
    corresponde AHORA MISMO a una sección, según el día y la hora actuales.
    Devuelve el nombre de la materia (str) o None si no hay clase activa en
    este momento (recreo, almuerzo, fuera de horario, o sin horario cargado)."""
    import sqlite3

    dias_ingles = {'Monday': 'Lunes', 'Tuesday': 'Martes', 'Wednesday': 'Miércoles',
                   'Thursday': 'Jueves', 'Friday': 'Viernes', 'Saturday': 'Sábado', 'Sunday': 'Domingo'}
    ahora = datetime.now()
    dia_actual_esp = dias_ingles.get(ahora.strftime('%A'), ahora.strftime('%A'))
    hora_actual = ahora.time()

    # Mismos bloques horarios que usa el módulo de Horarios (ver pestaña
    # "Visualizar y Generar Horarios"), solo los bloques de clase (sin recesos).
    bloque_actual_id = None
    for b in MEDIOS_BLOQUES_HORARIO:
        h_ini = datetime.strptime(b["ini"], "%H:%M").time()
        h_fin = datetime.strptime(b["fin"], "%H:%M").time()
        if h_ini <= hora_actual <= h_fin:
            bloque_actual_id = b["id"]
            break

    if bloque_actual_id is None:
        return None

    try:
        conn_h = sqlite3.connect("modulo_horarios/database.db")
        cursor_h = conn_h.cursor()
        cursor_h.execute("SELECT id FROM seccion WHERE nombre = ?", (seccion_nombre,))
        sec_res = cursor_h.fetchone()
        if not sec_res:
            conn_h.close()
            return None

        cursor_h.execute("""
                         SELECT m.nombre
                         FROM horario h
                                  JOIN materia m ON h.materia_id = m.id
                         WHERE h.seccion_id = ? AND h.dia = ? AND h.bloque_id = ?
                         """, (sec_res[0], dia_actual_esp, bloque_actual_id))
        res = cursor_h.fetchone()
        conn_h.close()
        return res[0] if res else None
    except Exception:
        return None


def obtener_o_crear_subject_por_nombre(session, nombre_materia):
    """Puente entre el módulo de Horarios (materias reales del pénsum, en
    SQLite) y el módulo de Asistencia (tabla 'subjects' en Postgres). Si la
    materia detectada todavía no existe del lado de Asistencia, se crea con
    el mismo nombre para que ambos módulos queden sincronizados."""
    materia_existente = session.query(Subject).filter_by(name=nombre_materia).first()
    if materia_existente:
        return materia_existente
    nueva_materia = Subject(name=nombre_materia)
    session.add(nueva_materia)
    session.commit()
    return nueva_materia


def obtener_bloques_pasados_hoy(seccion_nombre):
    """Devuelve el nombre de cada materia cuyo bloque de clase, HOY, ya terminó
    (según el horario real de la sección). Se usa para reconciliar asistencia:
    solo se puede marcar 'Ausente' un bloque que ya pasó, nunca uno futuro.
    Devuelve lista vacía en fin de semana (sábado/domingo)."""
    import sqlite3

    ahora = datetime.now()
    if ahora.weekday() >= 5:  # 5=sábado, 6=domingo
        return []

    dias_ingles = {'Monday': 'Lunes', 'Tuesday': 'Martes', 'Wednesday': 'Miércoles',
                   'Thursday': 'Jueves', 'Friday': 'Viernes'}
    dia_actual_esp = dias_ingles.get(ahora.strftime('%A'))
    if not dia_actual_esp:
        return []
    hora_actual = ahora.time()

    bloques_pasados_ids = [
        b["id"] for b in MEDIOS_BLOQUES_HORARIO
        if datetime.strptime(b["fin"], "%H:%M").time() <= hora_actual
    ]
    if not bloques_pasados_ids:
        return []

    try:
        conn_h = sqlite3.connect("modulo_horarios/database.db")
        cursor_h = conn_h.cursor()
        cursor_h.execute("SELECT id FROM seccion WHERE nombre = ?", (seccion_nombre,))
        sec_res = cursor_h.fetchone()
        if not sec_res:
            conn_h.close()
            return []

        placeholders = ",".join("?" * len(bloques_pasados_ids))
        cursor_h.execute(f"""
            SELECT DISTINCT m.nombre
            FROM horario h
            JOIN materia m ON h.materia_id = m.id
            WHERE h.seccion_id = ? AND h.dia = ? AND h.bloque_id IN ({placeholders})
        """, (sec_res[0], dia_actual_esp, *bloques_pasados_ids))
        resultados = [r[0] for r in cursor_h.fetchall()]
        conn_h.close()
        return resultados
    except Exception:
        return []


def reconciliar_ausencias_seccion(session, seccion_nombre):
    """Para cada bloque de clase de HOY que ya pasó, revisa el listado de
    estudiantes de la sección y crea automáticamente un registro 'Ausente'
    para quien no tenga ya un registro (por QR o manual) en ese bloque. El
    docente puede corregir cualquiera de estos registros manualmente después
    (a Presente o Permiso) — esta función nunca sobreescribe un registro que
    ya exista, sea cual sea su estado."""
    materias_pasadas = obtener_bloques_pasados_hoy(seccion_nombre)
    if not materias_pasadas:
        return 0

    roster = session.query(Student).filter_by(section=seccion_nombre).all()
    if not roster:
        return 0

    hoy = date.today()
    ciclo_id_actual = get_ciclo_activo(session).id
    creados = 0

    for nombre_materia in materias_pasadas:
        subject_obj = obtener_o_crear_subject_por_nombre(session, nombre_materia)

        # Una sola consulta trae TODOS los registros ya existentes de este bloque,
        # en vez de una consulta por estudiante (ver optimización del punto 2).
        ids_ya_registrados = {
            r[0] for r in session.query(Attendance.student_id).filter_by(
                subject_id=subject_obj.id, date=hoy
            ).filter(Attendance.student_id.in_([e.id for e in roster])).all()
        }

        for est in roster:
            if est.id in ids_ya_registrados:
                continue
            session.add(Attendance(
                student_id=est.id,
                subject_id=subject_obj.id,
                date=hoy,
                status="Ausente",
                observation="Registrado automáticamente: no se escaneó QR en este bloque.",
                ciclo_id=ciclo_id_actual
            ))
            creados += 1

    if creados:
        session.commit()
    return creados


def get_ciclo_activo(session):
    """Devuelve el ciclo activo. Se cachea en st.session_state (solo id y nombre,
    valores simples) porque esta función se llama varias veces por ejecución, y
    cada consulta es un viaje de ida y vuelta a la base de datos remota (Neon).
    El caché se invalida explícitamente en los 2 lugares donde el ciclo activo
    puede cambiar (activar un ciclo existente, o crear uno nuevo)."""
    if st.session_state.get('ciclo_activo_id') is not None:
        return _CicloActivoCache(
            st.session_state['ciclo_activo_id'], st.session_state['ciclo_activo_nombre'])

    ciclo = session.query(CicloEscolar).filter_by(activo=True).first()
    if not ciclo:
        ciclo = CicloEscolar(nombre=str(date.today().year), activo=True)
        session.add(ciclo)
        session.commit()

    st.session_state['ciclo_activo_id'] = ciclo.id
    st.session_state['ciclo_activo_nombre'] = ciclo.nombre
    return ciclo


current_user = st.session_state['user']

# --- Badge visual por rol (ícono + color distintivo) ---
NEXUS_ROL_BADGE = {
    "Admin": {"icon": "🛡️", "bg": "#0B1F4D", "fg": "#FFFFFF", "border": "#C9972E"},
    "Docente": {"icon": "🍎", "bg": "#EAF1FF", "fg": "#0B1F4D", "border": "#1E3A73"},
    "Alumno": {"icon": "🎒", "bg": "#FBF3E2", "fg": "#7A5A12", "border": "#C9972E"},
}


def render_badge_rol(rol, username, seccion=None):
    cfg = NEXUS_ROL_BADGE.get(rol, {"icon": "👤", "bg": "#EEE", "fg": "#333", "border": "#999"})
    seccion_html = f'<span style="opacity:0.8;"> · Sección: {seccion}</span>' if seccion else ""
    st.markdown(f"""
    <div style="display:inline-flex; align-items:center; gap:0.5rem;
                background:{cfg['bg']}; color:{cfg['fg']}; border:1.5px solid {cfg['border']};
                border-radius:999px; padding:0.35rem 0.9rem; font-weight:600; font-size:0.92rem;
                margin-top:0.2rem; box-shadow:0 2px 6px rgba(11,31,77,0.08);">
        <span style="font-size:1.1rem;">{cfg['icon']}</span>
        <span>{username}</span>
        <span style="opacity:0.55;">|</span>
        <span>{rol}</span>
        {seccion_html}
    </div>
    """, unsafe_allow_html=True)


# Encabezado
col_h1, col_h2 = st.columns([4, 1])
with col_h1:
    st.title("🎓 NEXUS SISTEMA INTEGRAL")
    render_badge_rol(current_user.role, current_user.username, current_user.assigned_section)
with col_h2:
    if st.button("🚪 Cerrar Sesión", key="btn_logout_header"):
        st.session_state['user'] = None
        st.session_state['user_id'] = None
        st.session_state['admin_view'] = 'select_ciclo'
        st.rerun()

st.divider()

# -----------------------------------------------------------------------------
# 1. PERFIL ALUMNO (GPS)
# -----------------------------------------------------------------------------
if current_user.role == "Alumno":
    st.subheader("🎒 Mi Perfil")
    student_data = session.query(Student).filter_by(id=current_user.student_id).first()
    if not student_data:
        st.error("Registro de alumno no encontrado.")
        st.stop()

    st.info(f"Estudiante: **{student_data.name}** | Sección: **{student_data.section}**")

    # =====================================================================
    # CANJE DE QR DE ASISTENCIA RÁPIDA (si se llegó a esta página vía el
    # enlace codificado en el QR, ej. ?attendance_token=XXXX)
    # =====================================================================
    token_param_qr = st.query_params.get("attendance_token")
    if token_param_qr:
        st.markdown("### 📷 Registro de Asistencia por Código QR")

        token_row_qr = session.query(QRAttendanceToken).filter_by(token=token_param_qr).first()

        if not token_row_qr:
            st.error("❌ Este código QR no es válido o ya no existe.")
        elif datetime.now() > token_row_qr.expires_at:
            st.error("⌛ Este código QR ya expiró (los códigos duran 5 minutos). Pide a tu docente que genere uno nuevo.")
        elif token_row_qr.seccion != student_data.section:
            st.error("❌ Este código QR no corresponde a tu sección.")
        else:
            ya_canjeado = session.query(QRAttendanceCheckin).filter_by(
                token_id=token_row_qr.id, student_id=student_data.id
            ).first()

            if ya_canjeado:
                st.info("✅ Ya habías registrado tu asistencia con este código QR.")
            else:
                # --- 1) VALIDACIÓN DE DISPOSITIVO VINCULADO ---
                device_id_navegador = streamlit_js_eval(
                    js_expressions="""
                    (function(){
                        let id = localStorage.getItem('nexus_device_id');
                        if(!id){
                            id = 'dev_' + Math.random().toString(36).substring(2,12) + Date.now().toString(36);
                            localStorage.setItem('nexus_device_id', id);
                        }
                        return id;
                    })()
                    """,
                    key="get_device_id_qr"
                )

                if device_id_navegador is None:
                    st.info("🔄 Verificando tu dispositivo... si no avanza en unos segundos, recarga la página.")
                else:
                    dispositivo_ok = False
                    if not current_user.device_id:
                        # Primera vez que este usuario usa el QR: se vincula automáticamente este dispositivo
                        current_user.device_id = device_id_navegador
                        session.commit()
                        dispositivo_ok = True
                    elif current_user.device_id == device_id_navegador:
                        dispositivo_ok = True

                    if not dispositivo_ok:
                        st.error(
                            "❌ Este código debe escanearse desde **tu propio dispositivo vinculado**. "
                            "Si cambiaste de celular, pide a tu docente orientador que reinicie tu vínculo de dispositivo.")
                    else:
                        # --- 2) VALIDACIÓN DE GPS (mismo criterio que el marcaje automático) ---
                        loc_qr = get_geolocation()
                        if loc_qr and 'coords' in loc_qr:
                            dist_qr = calculate_distance(
                                loc_qr['coords']['latitude'], loc_qr['coords']['longitude'],
                                INSTITUTE_LAT, INSTITUTE_LON
                            )
                            if dist_qr <= MAX_DISTANCE_METERS:
                                if not token_row_qr.subject_id:
                                    st.error(
                                        "❌ Este código QR no tiene una materia asociada (fue generado antes de "
                                        "esta actualización). Pide a tu docente que genere uno nuevo.")
                                    st.stop()
                                try:
                                    sub_id_qr = token_row_qr.subject_id
                                    existing_att_qr = session.query(Attendance).filter_by(
                                        student_id=student_data.id, subject_id=sub_id_qr, date=date.today()
                                    ).first()

                                    if not existing_att_qr:
                                        session.add(Attendance(
                                            student_id=student_data.id,
                                            subject_id=sub_id_qr,
                                            date=date.today(),
                                            status="Presente",
                                            observation="Asistencia registrada vía QR",
                                            ciclo_id=get_ciclo_activo(session).id
                                        ))
                                    else:
                                        existing_att_qr.status = "Presente"
                                        existing_att_qr.observation = "Asistencia registrada vía QR"

                                    session.add(QRAttendanceCheckin(
                                        token_id=token_row_qr.id, student_id=student_data.id
                                    ))
                                    session.commit()

                                    st.balloons()
                                    st.success("🚀 ¡Asistencia registrada correctamente mediante el código QR!")
                                except Exception:
                                    session.rollback()
                                    st.error(
                                        "❌ No se pudo guardar tu asistencia por un problema de conexión con la "
                                        "base de datos. Vuelve a escanear el código — si aún es válido, se "
                                        "registrará sin problema.")
                            else:
                                st.error(
                                    f"❌ Debes estar dentro del instituto para registrar tu asistencia por QR. "
                                    f"(Te encuentras a {dist_qr:.2f} metros)")
                        else:
                            st.warning("Por favor activa y concede el acceso a tu ubicación GPS en el navegador para completar el registro.")
        st.stop()

    # =====================================================================
    # PERFIL DEL ALUMNO (vista normal, sin llegar vía QR): ya NO se registra
    # asistencia automáticamente por GPS al entrar — la única forma de marcar
    # asistencia es escaneando el QR que genera el docente. Aquí el alumno
    # solo puede CONSULTAR su propia información.
    # =====================================================================
    st.info(
        "📷 Para registrar tu asistencia, escanea el código QR que tu docente muestra en clase. "
        "Esta pantalla es solo para consultar tu historial.")

    tab_mi_asistencia, tab_mis_permisos = st.tabs(["📊 Mi Asistencia", "📝 Mis Permisos"])

    with tab_mi_asistencia:
        try:
            resultado_riesgo_propio = evaluate_student_risk_detailed(session, student_data.id)
            render_badge_riesgo(resultado_riesgo_propio['status'], resultado_riesgo_propio['pct'])

            historial_propio = session.query(Attendance).filter_by(
                student_id=student_data.id
            ).order_by(Attendance.date.desc()).all()

            if not historial_propio:
                st.info("Todavía no tienes registros de asistencia.")
            else:
                presentes_propio = sum(1 for h in historial_propio if h.status == "Presente")
                tardanzas_propio = sum(1 for h in historial_propio if h.status == "Tardanza")
                ausentes_propio = sum(1 for h in historial_propio if h.status == "Ausente")
                permisos_propio = sum(1 for h in historial_propio if h.status == "Permiso")

                col_mp1, col_mp2, col_mp3, col_mp4 = st.columns(4)
                col_mp1.metric("Presentes", presentes_propio)
                col_mp2.metric("Tardanzas", tardanzas_propio)
                col_mp3.metric("Ausencias", ausentes_propio)
                col_mp4.metric("Permisos", permisos_propio)

                st.markdown("##### Historial completo")
                datos_hist_propio = []
                for h in historial_propio:
                    mat_nombre = "-"
                    if h.subject_id:
                        mat_obj = session.query(Subject).filter_by(id=h.subject_id).first()
                        if mat_obj:
                            mat_nombre = mat_obj.name
                    datos_hist_propio.append({
                        "Fecha": h.date.strftime('%d/%m/%Y'),
                        "Materia": mat_nombre,
                        "Estado": h.status,
                        "Observación": h.observation or "-"
                    })
                st.dataframe(pd.DataFrame(datos_hist_propio), width='stretch', hide_index=True)

        except Exception:
            st.error('❌ No se pudo cargar tu historial de asistencia por un problema de conexión. Recarga la página.')
    with tab_mis_permisos:
        try:
            permisos_propios = session.query(Attendance).filter(
                Attendance.student_id == student_data.id,
                Attendance.observation.isnot(None),
                Attendance.observation != ""
            ).order_by(Attendance.date.desc()).all()

            if not permisos_propios:
                st.info("No tienes permisos ni justificaciones registradas.")
            else:
                datos_permisos_propio = []
                for p in permisos_propios:
                    datos_permisos_propio.append({
                        "Fecha": p.date.strftime('%d/%m/%Y'),
                        "Estado": p.status,
                        "Detalle": p.observation,
                        "Evidencia": "📎 Sí" if p.evidencia_path else "—"
                    })
                st.dataframe(pd.DataFrame(datos_permisos_propio), width='stretch', hide_index=True)

                registros_con_evidencia_propio = [p for p in permisos_propios if p.evidencia_path]
                if registros_con_evidencia_propio:
                    st.markdown("###### 📎 Evidencias adjuntas")
                    for p in registros_con_evidencia_propio:
                        if os.path.exists(p.evidencia_path):
                            with open(p.evidencia_path, "rb") as f_ev:
                                st.download_button(
                                    label=f"Descargar evidencia del {p.date.strftime('%d/%m/%Y')}",
                                    data=f_ev.read(),
                                    file_name=os.path.basename(p.evidencia_path),
                                    key=f"btn_desc_evid_propio_{p.id}"
                                )
        except Exception:
            st.error('❌ No se pudo cargar tu historial de permisos por un problema de conexión. Recarga la página.')
    st.stop()
    st.stop()

# -----------------------------------------------------------------------------
# 2. PERFIL ADMINISTRADOR: PANTALLA PRINCIPAL CON SELECCIÓN DE MÓDULOS
# -----------------------------------------------------------------------------
if current_user.role == "Admin":

    if st.session_state['admin_view'] == 'select_ciclo':
        st.subheader("🎓 NEXUS — Selecciona tu Sesión de Trabajo")
        st.write("Elige el ciclo escolar (año) donde quieres trabajar, o crea uno nuevo.")
        st.write("")

        todos_ciclos_landing = session.query(CicloEscolar).order_by(CicloEscolar.nombre.desc()).all()
        items_landing = [("nuevo", None)] + [("ciclo", c) for c in todos_ciclos_landing]

        cols_por_fila = 4
        filas_landing = [items_landing[i:i + cols_por_fila] for i in range(0, len(items_landing), cols_por_fila)]

        for fila in filas_landing:
            cols_landing = st.columns(cols_por_fila)
            for col, (tipo, ciclo_obj) in zip(cols_landing, fila):
                with col:
                    if tipo == "nuevo":
                        st.markdown("""
                        <div class="nexus-tech-card-wrap">
                            <div class="nexus-tech-card" style="text-align:center; padding-top:2.2rem; padding-bottom:2.2rem;">
                                <span class="nexus-tech-card-icon">➕</span>
                                <div class="nexus-tech-card-title">Crear Nueva Sesión</div>
                                <div class="nexus-tech-card-desc">Empieza un ciclo escolar nuevo, en blanco.</div>
                            </div>
                        </div>
                        """, unsafe_allow_html=True)
                        if st.button("➕ Crear Nueva Sesión", key="btn_trigger_crear_ciclo_landing",
                                     width='stretch'):
                            st.session_state['mostrar_form_nuevo_ciclo'] = True
                            st.rerun()
                    else:
                        c = ciclo_obj
                        badge_activo = (
                            '<div style="margin-top:0.5rem; display:inline-block; background:var(--nexus-navy); '
                            'color:#fff; padding:0.15rem 0.7rem; border-radius:999px; font-size:0.72rem; '
                            'font-weight:700;">ACTIVO AHORA</div>'
                        ) if c.activo else ''
                        st.markdown(f"""
                        <div class="nexus-tech-card-wrap">
                            <div class="nexus-tech-card" style="text-align:center; padding-top:2.2rem; padding-bottom:2rem;">
                                <span class="nexus-tech-card-icon">📁</span>
                                <div class="nexus-tech-card-title">{c.nombre}</div>
                                <div class="nexus-tech-card-desc">Ciclo escolar {c.nombre}</div>
                                {badge_activo}
                            </div>
                        </div>
                        """, unsafe_allow_html=True)
                        etiqueta_btn = "✅ Ya estás aquí" if c.activo else "🔓 Entrar a este ciclo"
                        if st.button(etiqueta_btn, key=f"btn_trigger_entrar_ciclo_{c.id}", width='stretch'):
                            for cc in todos_ciclos_landing:
                                cc.activo = (cc.id == c.id)
                            session.commit()
                            st.session_state.pop('ciclo_activo_id', None)
                            st.session_state.pop('ciclo_activo_nombre', None)
                            st.session_state['admin_view'] = 'dashboard'
                            st.rerun()

        # --- Formulario de creación (aparece tras darle clic a "Crear Nueva Sesión") ---
        if st.session_state.get('mostrar_form_nuevo_ciclo'):
            st.divider()
            with st.form("form_crear_ciclo_landing"):
                st.markdown("##### ➕ Nueva Sesión / Ciclo Escolar")
                nombre_ciclo_landing = st.text_input("Nombre del ciclo (ej. 2027)",
                                                     key="txt_nombre_ciclo_landing")
                crear_y_entrar = st.form_submit_button("Crear y Entrar", type="primary")
                if crear_y_entrar:
                    nombre_limpio_landing = nombre_ciclo_landing.strip()
                    if not nombre_limpio_landing:
                        st.warning("Escribe un nombre para el ciclo.")
                    elif session.query(CicloEscolar).filter_by(nombre=nombre_limpio_landing).first():
                        st.error(f"Ya existe un ciclo llamado '{nombre_limpio_landing}'.")
                    else:
                        for cc in todos_ciclos_landing:
                            cc.activo = False
                        session.add(CicloEscolar(nombre=nombre_limpio_landing, activo=True))
                        session.commit()
                        st.session_state.pop('ciclo_activo_id', None)
                        st.session_state.pop('ciclo_activo_nombre', None)
                        st.session_state['mostrar_form_nuevo_ciclo'] = False
                        st.session_state['admin_view'] = 'dashboard'
                        st.success(f"✅ Ciclo '{nombre_limpio_landing}' creado y activado.")
                        st.rerun()

        st.stop()

    if st.session_state['admin_view'] == 'dashboard':
        ciclo_activo_dash = get_ciclo_activo(session)
        col_dash_head1, col_dash_head2 = st.columns([4, 1])
        with col_dash_head1:
            st.subheader("🏛️ Panel Central de Administración NEXUS")
            st.caption(f"📁 Trabajando en el ciclo: **{ciclo_activo_dash.nombre}**")
        with col_dash_head2:
            st.write("")
            if st.button("🔄 Cambiar de Ciclo", key="btn_cambiar_ciclo_dash"):
                st.session_state['admin_view'] = 'select_ciclo'
                st.rerun()
        st.write("Selecciona el módulo al que deseas ingresar:")
        st.write("")

        col_m1, col_m2 = st.columns(2)

        with col_m1:
            st.markdown("""
            <div class="nexus-tech-card-wrap">
                <div class="nexus-tech-card">
                    <span class="nexus-tech-card-icon">📅</span>
                    <div class="nexus-tech-card-title">Módulo de Horarios</div>
                    <div class="nexus-tech-card-desc">
                        Gestión y programación de horarios institucionales, asignación de
                        materias y carga académica.
                    </div>
                </div>
            </div>
            """, unsafe_allow_html=True)
            if st.button("👉 Ingresar a Módulo de Horarios", type="primary", width='stretch',
                         key="btn_goto_horarios_dash"):
                st.session_state['admin_view'] = 'horarios'
                st.rerun()

        with col_m2:
            st.markdown("""
            <div class="nexus-tech-card-wrap">
                <div class="nexus-tech-card">
                    <span class="nexus-tech-card-icon">📊</span>
                    <div class="nexus-tech-card-title">Módulo de Asistencia</div>
                    <div class="nexus-tech-card-desc">
                        Control inteligente de asistencia, geolocalización, seguimiento de
                        casos y semáforo de permanencia.
                    </div>
                </div>
            </div>
            """, unsafe_allow_html=True)
            if st.button("👉 Ingresar a Módulo de Asistencia", type="primary", width='stretch',
                         key="btn_goto_asistencia_dash"):
                st.session_state['admin_view'] = 'asistencia'
                st.rerun()

        st.stop()



    elif st.session_state['admin_view'] == 'horarios':

        if st.button("⬅️ Volver al Panel Principal NEXUS", key="btn_back_from_horarios_view"):
            st.session_state['admin_view'] = 'dashboard'

            st.rerun()

        st.divider()

        st.subheader("📅 Módulo Avanzado de Horarios y Carga Académica")

        st.info(
            "Sistema integrado nativamente: gestión de secciones, docentes, cargas, generación algorítmica y control total.")

        import sqlite3

        import random

        try:
            conn_h = sqlite3.connect("modulo_horarios/database.db")
            cursor_h = conn_h.cursor()

            # --- PARCHE QUIRÚRGICO: Asegurar columnas de días en la tabla docente ---
            try:
                cursor_h.execute("ALTER TABLE docente ADD COLUMN dias_matutino TEXT;")
                conn_h.commit()
            except Exception:
                pass

            try:
                cursor_h.execute("ALTER TABLE docente ADD COLUMN dias_vespertino TEXT;")
                conn_h.commit()
            except Exception:
                pass

            try:
                cursor_h.execute("ALTER TABLE horario ADD COLUMN origen TEXT DEFAULT 'generado';")
                conn_h.commit()
            except Exception:
                pass
            # ------------------------------------------------------------------------

            # Reducido a 4 pestañas limpias y funcionales
            tab_h1, tab_h2, tab_h3, tab_h4 = st.tabs([

                "🏫 Secciones", "📚 Materias", "👨‍🏫 Docentes y Cargas", "👁️ Visualizar y Generar Horarios"

            ])

            # -----------------------------------------------------------------
            # 1. GESTIÓN DE SECCIONES
            # -----------------------------------------------------------------
            with tab_h1:
                st.markdown("#### Gestión y Creación de Secciones")

                MAPA_ABREVIACIONES = {
                    "Bachillerato Academico": "BTO-A",
                    "Bachillerato técnico productivo en sistemas eléctricos y energías renovables": "BTP-SEER",
                    "Bachillerato Tecnico vocacional Sistemas Electricos": "BTV-SE",
                    "Bachillerato técnico vocacional en desarrollo de software": "BTV-DS",
                    "bachillerato técnico vocacional administrativo contable": "BTV-AC",
                    "Bachillerato técnico vocacional en diseño grafico": "BTV-DG",
                    "Bachillerato técnico productivo en salud y bienestar social": "BTP-SBS",
                    "Bachillerato Tecnico vocacional Atención Primaria en salud": "BTV-APS"
                }

                with st.form("form_crear_seccion_nativo"):
                    modalidad_sel = st.selectbox("Modalidad", list(MAPA_ABREVIACIONES.keys()))
                    anio_sel = st.selectbox("Año", ["1° año", "2° año", "3° año"])
                    grupo_sel = st.selectbox("Grupo", ["A", "A1", "A2", "B", "C", "D", "E"])
                    btn_sec_sub = st.form_submit_button("➕ Crear Sección Oficial", width='stretch')

                    if btn_sec_sub:
                        import re

                        num_anio = re.search(r'\d+', str(anio_sel))
                        num_anio_str = num_anio.group() if num_anio else ""
                        prefijo = MAPA_ABREVIACIONES.get(modalidad_sel, "SEC")
                        nombre_generado = f"{prefijo}-{num_anio_str}{grupo_sel}"
                        formato_anio = f"{anio_sel} '{grupo_sel}'"

                        try:
                            cursor_h.execute(
                                "INSERT INTO seccion (nombre, modalidad, anio) VALUES (?, ?, ?)",
                                (nombre_generado, modalidad_sel, formato_anio)
                            )
                            conn_h.commit()
                            st.success(f"¡Sección '{nombre_generado}' creada con éxito!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error al crear la sección: {e}")

                st.divider()
                st.markdown("#### 🗑️ Eliminar Sección")

                cursor_h.execute("SELECT id, nombre FROM seccion ORDER BY nombre")
                secs_existentes = cursor_h.fetchall()

                if secs_existentes:
                    mapa_secs_del = {s[1]: s[0] for s in secs_existentes}
                    sec_a_borrar = st.selectbox("Selecciona la sección a eliminar", list(mapa_secs_del.keys()),
                                                key="select_del_sec")

                    if st.button("❌ Eliminar Sección Seleccionada", type="primary", width='stretch'):
                        sec_id_del = mapa_secs_del[sec_a_borrar]
                        try:
                            # Opcional: Eliminar primero los registros asociados en horario y carga si es necesario para evitar errores de integridad
                            cursor_h.execute("DELETE FROM horario WHERE seccion_id = ?", (sec_id_del,))
                            cursor_h.execute("DELETE FROM cargaacademica WHERE seccion_id = ?", (sec_id_del,))
                            cursor_h.execute("DELETE FROM seccion WHERE id = ?", (sec_id_del,))
                            conn_h.commit()
                            st.success(f"¡Sección '{sec_a_borrar}' eliminada correctamente!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error al eliminar la sección: {e}")
                else:
                    st.info("No hay secciones disponibles para eliminar.")

                st.divider()
                df_secciones = pd.read_sql("SELECT * FROM seccion", conn_h)
                if not df_secciones.empty:
                    st.dataframe(df_secciones, width='stretch')
                else:
                    st.warning("No hay secciones registradas.")

                # -----------------------------------------------------------------
                # 2. GESTIÓN DE MATERIAS
                # -----------------------------------------------------------------
            with tab_h2:
                        st.markdown("#### Gestión de Materias")
                        with st.form("form_crear_materia_nativo"):
                            mat_nombre = st.text_input("Nombre de la Materia")
                            mat_tipo = st.selectbox("Tipo de Materia", ["Básica", "Modular"])
                            btn_mat_sub = st.form_submit_button("➕ Registrar Materia", width='stretch')

                            if btn_mat_sub:
                                if mat_nombre.strip():
                                    try:
                                        cursor_h.execute("INSERT INTO materia (nombre, tipo) VALUES (?, ?)",
                                                         (mat_nombre.strip(), mat_tipo))
                                        conn_h.commit()
                                        st.success(f"¡Materia '{mat_nombre.strip()}' guardada con éxito!")
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"Error: {e}")
                                else:
                                    st.warning("Ingresa un nombre válido.")

                        st.divider()
                        st.markdown("#### 🗑️ Eliminar Materia")

                        cursor_h.execute("SELECT id, nombre FROM materia")
                        mats_existentes = cursor_h.fetchall()

                        if mats_existentes:
                            mapa_mats_del = {m[1]: m[0] for m in mats_existentes}
                            mat_a_borrar = st.selectbox("Selecciona la materia a eliminar", list(mapa_mats_del.keys()),
                                                        key="select_del_mat")

                            if st.button("❌ Eliminar Materia Seleccionada", type="primary", width='stretch'):
                                mat_id_del = mapa_mats_del[mat_a_borrar]
                                try:
                                    cursor_h.execute("DELETE FROM horario WHERE materia_id = ?", (mat_id_del,))
                                    cursor_h.execute("DELETE FROM cargaacademica WHERE materia_id = ?", (mat_id_del,))
                                    cursor_h.execute("DELETE FROM materia WHERE id = ?", (mat_id_del,))
                                    conn_h.commit()
                                    st.success(f"¡Materia '{mat_a_borrar}' eliminada correctamente!")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error al eliminar la materia: {e}")
                        else:
                            st.info("No hay materias disponibles para eliminar.")

                        st.divider()
                        df_mat = pd.read_sql("SELECT * FROM materia", conn_h)
                        if not df_mat.empty:
                            st.dataframe(df_mat, width='stretch')
                        else:
                            st.warning("No hay materias registradas.")
                # -----------------------------------------------------------------
                # 3. GESTIÓN DE DOCENTES Y CARGAS ACADÉMICAS
                # -----------------------------------------------------------------
            with tab_h3:
                                st.markdown("#### Registro de Docentes y Asignación de Carga")

                                cursor_h.execute("SELECT id, nombre FROM seccion ORDER BY nombre")
                                secs_db = cursor_h.fetchall()
                                mapa_secs_id = {s[1]: s[0] for s in secs_db}

                                cursor_h.execute("SELECT id, nombre FROM materia")
                                mats_db = cursor_h.fetchall()
                                mapa_mats_id = {m[1]: m[0] for m in mats_db}

                                if 'num_cargas_dinamicas' not in st.session_state:
                                    st.session_state['num_cargas_dinamicas'] = 1

                                doc_nombre = st.text_input("Nombre del Docente", key="doc_nombre_input")
                                doc_correo = st.text_input("Correo Institucional", value="docente@instituto.edu",
                                                           key="doc_correo_input")

                                doc_turno = st.selectbox(
                                    "Turno Preferente",
                                    ["Matutino", "Vespertino", "Doble Turno", "Horario Accesible"],
                                    key="doc_turno_select"
                                )

                                # Variables para capturar los días por turno
                                dias_matutino_str = ""
                                dias_vespertino_str = ""

                                if doc_turno == "Horario Accesible":
                                    st.markdown("##### 🗓️ Configurar Días por Turno")
                                    dias_semana_opciones = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes"]

                                    col_d1, col_d2 = st.columns(2)
                                    with col_d1:
                                        dias_mat = st.multiselect("🌅 Días en Turno Matutino", dias_semana_opciones,
                                                                  key="form_dias_mat")
                                    with col_d2:
                                        dias_vesp = st.multiselect("🌇 Días en Turno Vespertino", dias_semana_opciones,
                                                                   key="form_dias_vesp")

                                    dias_matutino_str = ", ".join(dias_mat) if dias_mat else ""
                                    dias_vespertino_str = ", ".join(dias_vesp) if dias_vesp else ""

                                st.divider()
                                st.markdown("##### Asignación de Carga (Sección + Materia + Horas Semanales)")

                                cargas_ingresadas = []
                                if secs_db and mats_db:
                                    for i in range(st.session_state['num_cargas_dinamicas']):
                                        col_c1, col_c2, col_c3 = st.columns(3)
                                        with col_c1:
                                            s_sel = st.selectbox(f"Sección #{i + 1}",
                                                                 ["Ninguna"] + list(mapa_secs_id.keys()),
                                                                 key=f"c_sec_{i}")
                                        with col_c2:
                                            m_sel = st.selectbox(f"Materia #{i + 1}",
                                                                 ["Ninguna"] + list(mapa_mats_id.keys()),
                                                                 key=f"c_mat_{i}")
                                        with col_c3:
                                            h_val = st.number_input(f"Medios Bloques #{i + 1}", min_value=0,
                                                                    max_value=20, value=0,
                                                                    key=f"c_hrs_{i}")

                                        if s_sel != "Ninguna" and m_sel != "Ninguna" and h_val > 0:
                                            cargas_ingresadas.append((mapa_secs_id[s_sel], mapa_mats_id[m_sel], h_val))
                                else:
                                    st.info("Primero debes crear Secciones y Materias para asignar cargas.")

                                if secs_db and mats_db:
                                    if st.button("➕ Agregar otra materia/carga", width='stretch',
                                                 key="btn_add_carga_extra"):
                                        st.session_state['num_cargas_dinamicas'] += 1
                                        st.rerun()

                                st.divider()

                                if st.button("🚀 Registrar Docente y Carga Oficial", width='stretch',
                                             key="btn_registrar_docente_final"):
                                    if doc_nombre.strip():
                                        try:
                                            cursor_h.execute(
                                                "INSERT INTO docente (nombre, correo_institucional, turno_preferente, dias_matutino, dias_vespertino) VALUES (?, ?, ?, ?, ?)",
                                                (doc_nombre.strip(), doc_correo.strip(), doc_turno, dias_matutino_str,
                                                 dias_vespertino_str)
                                            )
                                            doc_id_nuevo = cursor_h.lastrowid

                                            for s_id, m_id, hrs in cargas_ingresadas:
                                                cursor_h.execute(
                                                    "INSERT INTO cargaacademica (docente_id, seccion_id, materia_id, horas_semanales) VALUES (?, ?, ?, ?)",
                                                    (doc_id_nuevo, s_id, m_id, hrs)
                                                )
                                            conn_h.commit()
                                            st.session_state['num_cargas_dinamicas'] = 1
                                            st.success(f"¡Docente '{doc_nombre.strip()}' registrado con éxito!")
                                            st.rerun()
                                        except Exception as err:
                                            st.error(f"Error detallado al registrar: {err}")
                                    else:
                                        st.warning("El nombre del docente es obligatorio.")

                                st.divider()
                                st.markdown("##### 📋 Listado y Gestión de Docentes Registrados")

                                cursor_h.execute("""
                                                 SELECT d.id,
                                                        d.nombre,
                                                        d.correo_institucional,
                                                        d.turno_preferente,
                                                        d.dias_matutino,
                                                        d.dias_vespertino,
                                                        s.nombre as seccion,
                                                        m.nombre as materia,
                                                        c.horas_semanales
                                                 FROM docente d
                                                          LEFT JOIN cargaacademica c ON d.id = c.docente_id
                                                          LEFT JOIN seccion s ON c.seccion_id = s.id
                                                          LEFT JOIN materia m ON c.materia_id = m.id
                                                 """)
                                datos_registrados = cursor_h.fetchall()

                                if datos_registrados:
                                    import pandas as pd

                                    df_docentes = pd.DataFrame(datos_registrados, columns=[
                                        "ID", "Nombre", "Correo", "Turno", "Días Mat.", "Días Vesp.", "Sección",
                                        "Materia", "Horas"
                                    ])
                                    st.dataframe(df_docentes, width='stretch')

                                    st.markdown("##### 🗑️ Eliminar Docente")
                                    # Obtener lista única de docentes para eliminar
                                    cursor_h.execute("SELECT id, nombre FROM docente")
                                    docentes_db = cursor_h.fetchall()

                                    if docentes_db:
                                        mapa_docentes_del = {f"{d[1]} (ID: {d[0]})": d[0] for d in docentes_db}
                                        docente_a_borrar_sel = st.selectbox(
                                            "Selecciona el docente que deseas eliminar",
                                            list(mapa_docentes_del.keys()),
                                            key="select_docente_eliminar"
                                        )

                                        if st.button("🗑️ Eliminar Docente Seleccionado", width='stretch',
                                                     key="btn_ejecutar_eliminar_docente"):
                                            doc_id_del = mapa_docentes_del[docente_a_borrar_sel]
                                            try:
                                                # Borrar primero la carga académica asociada por la llave foránea
                                                cursor_h.execute("DELETE FROM cargaacademica WHERE docente_id = ?",
                                                                 (doc_id_del,))
                                                # Borrar al docente
                                                cursor_h.execute("DELETE FROM docente WHERE id = ?", (doc_id_del,))
                                                conn_h.commit()
                                                st.success(f"¡Docente eliminado con éxito junto con su carga asociada!")
                                                st.rerun()
                                            except Exception as err:
                                                st.error(f"Error al eliminar el registro: {err}")
                                else:
                                    st.info("Aún no hay docentes registrados en la base de datos.")
                                # -----------------------------------------------------------------
                                # 4. VISUALIZAR EN CUADRÍCULA Y MOTOR DE GENERACIÓN UNIFICADO
                                # -----------------------------------------------------------------
            with tab_h4:

                st.markdown("#### ⚡ Panel de Generación y Visualización de Horarios")

                # --- DEFINICIONES COMPARTIDAS (usadas por ambas sub-pestañas) ---
                MEDIOS_BLOQUES = [
                    {"id": 1, "hora": "7:00 am - 7:45 am"},
                    {"id": 2, "hora": "7:45 am - 8:30 am"},
                    {"id": 3, "hora": "8:40 am - 9:25 am"},
                    {"id": 4, "hora": "9:25 am - 10:10 am"},
                    {"id": 5, "hora": "10:20 am - 11:05 am"},
                    {"id": 6, "hora": "11:05 am - 11:50 am"},
                    {"id": 7, "hora": "1:00 pm - 1:45 pm"},
                    {"id": 8, "hora": "1:45 pm - 2:30 pm"},
                    {"id": 9, "hora": "2:40 pm - 3:25 pm"},
                    {"id": 10, "hora": "3:25 pm - 4:10 pm"},
                    {"id": 11, "hora": "4:20 pm - 5:10 pm"},
                    {"id": 12, "hora": "5:10 pm - 5:50 pm"},
                ]

                MEDIOS_BLOQUES_VIS = [
                    {"id": 1, "hora": "7:00 am - 7:45 am", "es_pausa": False},
                    {"id": 2, "hora": "7:45 am - 8:30 am", "es_pausa": False},
                    {"id": 991, "hora": "8:30 am - 8:40 am", "es_pausa": True, "tipo": "☕ RECESO"},
                    {"id": 3, "hora": "8:40 am - 9:25 am", "es_pausa": False},
                    {"id": 4, "hora": "9:25 am - 10:10 am", "es_pausa": False},
                    {"id": 992, "hora": "10:10 am - 10:20 am", "es_pausa": True, "tipo": "☕ RECESO"},
                    {"id": 5, "hora": "10:20 am - 11:05 am", "es_pausa": False},
                    {"id": 6, "hora": "11:05 am - 11:50 am", "es_pausa": False},
                    {"id": 993, "hora": "11:50 am - 1:00 pm", "es_pausa": True, "tipo": "🍽️ ALMUERZO"},
                    {"id": 7, "hora": "1:00 pm - 1:45 pm", "es_pausa": False},
                    {"id": 8, "hora": "1:45 pm - 2:30 pm", "es_pausa": False},
                    {"id": 994, "hora": "2:30 pm - 2:40 pm", "es_pausa": True, "tipo": "☕ RECESO"},
                    {"id": 9, "hora": "2:40 pm - 3:25 pm", "es_pausa": False},
                    {"id": 10, "hora": "3:25 pm - 4:10 pm", "es_pausa": False},
                    {"id": 995, "hora": "4:10 pm - 4:20 pm", "es_pausa": True, "tipo": "☕ RECESO"},
                    {"id": 11, "hora": "4:20 pm - 5:10 pm", "es_pausa": False},
                    {"id": 12, "hora": "5:10 pm - 5:50 pm", "es_pausa": False},
                ]

                DIAS_SEMANA = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes"]
                # Bloques matutinos (7:00 am - 12:00 pm) y vespertinos (1:00 pm - 5:50 pm)
                # agrupados en parejas: cada pareja = 2 medios bloques seguidos = 1 bloque completo.
                PAREJAS_MATUTINO = [(1, 2), (3, 4), (5, 6)]
                PAREJAS_VESPERTINO = [(7, 8), (9, 10), (11, 12)]

                import random

                def slots_permitidos(turno, dias_mat_str, dias_vesp_str):
                    """Devuelve la lista de (dia, (id1, id2)) que un docente puede usar
                    según su turno preferente y, si aplica, sus días personalizados."""
                    dias_mat_str = dias_mat_str or ""
                    dias_vesp_str = dias_vesp_str or ""
                    slots = []
                    if turno == "Matutino":
                        for d in DIAS_SEMANA:
                            for p in PAREJAS_MATUTINO:
                                slots.append((d, p))
                    elif turno == "Vespertino":
                        for d in DIAS_SEMANA:
                            for p in PAREJAS_VESPERTINO:
                                slots.append((d, p))
                    elif turno == "Doble Turno":
                        for d in DIAS_SEMANA:
                            for p in PAREJAS_MATUTINO + PAREJAS_VESPERTINO:
                                slots.append((d, p))
                    elif turno == "Horario Accesible":
                        dias_mat = [x.strip() for x in dias_mat_str.split(",") if x.strip()]
                        dias_vesp = [x.strip() for x in dias_vesp_str.split(",") if x.strip()]
                        for d in dias_mat:
                            for p in PAREJAS_MATUTINO:
                                slots.append((d, p))
                        for d in dias_vesp:
                            for p in PAREJAS_VESPERTINO:
                                slots.append((d, p))
                    return slots

                sub_tab_sec, sub_tab_doc = st.tabs(
                    ["🏫 Generador y Vista por Sección", "👨‍🏫 Vista y Edición por Docente"])

                # =====================================================================
                # SUB-PESTAÑA 1: GENERADOR Y VISTA POR SECCIÓN
                # =====================================================================
                with sub_tab_sec:

                    if st.button("🚀 Ejecutar Generador Global de Horarios", type="primary",
                                 width='stretch',
                                 key="btn_ejecutar_gen_global_h4"):

                        # --- CONSULTAR CARGAS ACADÉMICAS + TURNO Y DÍAS DEL DOCENTE ---
                        cursor_h.execute("""
                            SELECT ca.id, ca.docente_id, ca.seccion_id, ca.materia_id, ca.horas_semanales,
                                   d.turno_preferente, d.dias_matutino, d.dias_vespertino
                            FROM cargaacademica ca
                            JOIN docente d ON ca.docente_id = d.id
                        """)
                        cargas_raw = cursor_h.fetchall()

                        if not cargas_raw:
                            st.warning(
                                "⚠️ La tabla `cargaacademica` está completamente vacía. Primero debes registrar cargas académicas a los docentes.")
                        else:
                            # --- LOS BLOQUES MARCADOS COMO 'manual' SE RESPETAN Y NUNCA SE BORRAN ---
                            cursor_h.execute(
                                "SELECT seccion_id, docente_id, materia_id, dia, bloque_id FROM horario WHERE origen = 'manual'")
                            bloques_manuales = cursor_h.fetchall()

                            docentes_ocupados = set()
                            secciones_ocupadas = set()
                            horas_ya_cubiertas = {}

                            for sec_id_m, doc_id_m, mat_id_m, dia_m, bloque_id_m in bloques_manuales:
                                docentes_ocupados.add((doc_id_m, dia_m, bloque_id_m))
                                secciones_ocupadas.add((sec_id_m, dia_m, bloque_id_m))
                                clave_m = (doc_id_m, sec_id_m, mat_id_m)
                                horas_ya_cubiertas[clave_m] = horas_ya_cubiertas.get(clave_m, 0) + 1

                            # --- SOLO SE BORRAN LOS BLOQUES GENERADOS AUTOMÁTICAMENTE ---
                            cursor_h.execute("DELETE FROM horario WHERE origen != 'manual' OR origen IS NULL")

                            cargas_lista = list(cargas_raw)
                            random.shuffle(cargas_lista)

                            intentos_exitosos = 0
                            cargas_incompletas = []
                            avisos_manuales = []

                            for c_id, doc_id, sec_id, mat_id, hrs_sem, turno, dias_mat, dias_vesp in cargas_lista:
                                clave = (doc_id, sec_id, mat_id)
                                ya_cubiertas = horas_ya_cubiertas.get(clave, 0)
                                horas_pendientes = hrs_sem - ya_cubiertas

                                if ya_cubiertas > hrs_sem:
                                    avisos_manuales.append(
                                        f"Carga #{c_id}: tiene {ya_cubiertas} medios bloques asignados manualmente, "
                                        f"más que sus {hrs_sem} horas semanales de carga. Revísala manualmente.")
                                    horas_pendientes = 0

                                if horas_pendientes <= 0:
                                    continue

                                slots = slots_permitidos(turno, dias_mat, dias_vesp)
                                random.shuffle(slots)

                                # 1) Asignar bloques completos: siempre 2 medios bloques seguidos
                                for dia, (id1, id2) in slots:
                                    if horas_pendientes < 2:
                                        break

                                    b1 = next((b for b in MEDIOS_BLOQUES if b["id"] == id1), None)
                                    b2 = next((b for b in MEDIOS_BLOQUES if b["id"] == id2), None)
                                    if not b1 or not b2:
                                        continue

                                    c_doc1, c_doc2 = (doc_id, dia, id1), (doc_id, dia, id2)
                                    c_sec1, c_sec2 = (sec_id, dia, id1), (sec_id, dia, id2)

                                    if (
                                            c_doc1 not in docentes_ocupados and c_doc2 not in docentes_ocupados and
                                            c_sec1 not in secciones_ocupadas and c_sec2 not in secciones_ocupadas):

                                        for bloq in [b1, b2]:
                                            cursor_h.execute(
                                                "INSERT INTO horario (seccion_id, docente_id, materia_id, dia, bloque_id, hora_texto, origen) VALUES (?, ?, ?, ?, ?, ?, 'generado')",
                                                (sec_id, doc_id, mat_id, dia, bloq["id"], bloq["hora"])
                                            )
                                            docentes_ocupados.add((doc_id, dia, bloq["id"]))
                                            secciones_ocupadas.add((sec_id, dia, bloq["id"]))
                                        horas_pendientes -= 2
                                        intentos_exitosos += 1

                                # 2) Si sobra 1 medio bloque suelto (horas impares), asignarlo aparte
                                if horas_pendientes == 1:
                                    medios_sueltos = [(dia, i) for dia, par in slots for i in par]
                                    random.shuffle(medios_sueltos)
                                    for dia, id_suelto in medios_sueltos:
                                        b = next((x for x in MEDIOS_BLOQUES if x["id"] == id_suelto), None)
                                        if not b:
                                            continue
                                        c_doc, c_sec = (doc_id, dia, id_suelto), (sec_id, dia, id_suelto)
                                        if c_doc not in docentes_ocupados and c_sec not in secciones_ocupadas:
                                            cursor_h.execute(
                                                "INSERT INTO horario (seccion_id, docente_id, materia_id, dia, bloque_id, hora_texto, origen) VALUES (?, ?, ?, ?, ?, ?, 'generado')",
                                                (sec_id, doc_id, mat_id, dia, b["id"], b["hora"])
                                            )
                                            docentes_ocupados.add(c_doc)
                                            secciones_ocupadas.add(c_sec)
                                            horas_pendientes -= 1
                                            intentos_exitosos += 1
                                            break

                                if horas_pendientes > 0:
                                    cargas_incompletas.append((doc_id, sec_id, horas_pendientes))

                            conn_h.commit()
                            st.success(
                                f"🎉 ¡Generación finalizada! Se insertaron {intentos_exitosos} bloques horarios nuevos. "
                                f"Los bloques que habías editado manualmente se conservaron intactos.")
                            if cargas_incompletas:
                                st.warning(
                                    f"⚠️ {len(cargas_incompletas)} carga(s) académica(s) no se pudieron completar del todo: "
                                    f"no había suficientes espacios disponibles dentro del turno/días permitidos de ese docente. "
                                    f"Revisa si tiene demasiadas horas asignadas para su turno.")
                            for aviso in avisos_manuales:
                                st.warning(f"⚠️ {aviso}")
                            st.rerun()
                    st.divider()

                    # Visualizador en Cuadrícula debajo del botón
                    cursor_h.execute("SELECT id, nombre FROM seccion ORDER BY nombre")
                    secs_db = cursor_h.fetchall()

                    if secs_db:
                        mapa_secs = {str(r[1]): r[0] for r in secs_db}
                        secs_nombres = list(mapa_secs.keys())
                        sec_ids_ordenados = [r[0] for r in secs_db]

                        if "sec_vis_seleccionada_id" not in st.session_state or \
                                st.session_state["sec_vis_seleccionada_id"] not in sec_ids_ordenados:
                            st.session_state["sec_vis_seleccionada_id"] = sec_ids_ordenados[0]

                        idx_actual = sec_ids_ordenados.index(st.session_state["sec_vis_seleccionada_id"])

                        sec_elegida = st.selectbox("Seleccionar Sección a Consultar", secs_nombres,
                                                   index=idx_actual,
                                                   key="select_vis_sec_unificado")
                        sec_id_sel = mapa_secs[sec_elegida]
                        st.session_state["sec_vis_seleccionada_id"] = sec_id_sel

                        dias_semana = DIAS_SEMANA
                        tabla_matriz = []

                        skel_placeholder_sec = st.empty()
                        mostrar_skeleton(skel_placeholder_sec, n_lineas=8,
                                         titulo="Cargando horario de la sección...")

                        for b in MEDIOS_BLOQUES_VIS:
                            fila = {"Hora / Bloque": b["hora"]}
                            if b["es_pausa"]:
                                for d in dias_semana:
                                    fila[d] = b["tipo"]
                            else:
                                for d in dias_semana:
                                    cursor_h.execute("""
                                                     SELECT m.nombre, doc.nombre, h.origen
                                                     FROM horario h
                                                              JOIN materia m ON h.materia_id = m.id
                                                              JOIN docente doc ON h.docente_id = doc.id
                                                     WHERE h.seccion_id = ?
                                                       AND h.dia = ?
                                                       AND h.bloque_id = ?
                                                     """, (sec_id_sel, d, b["id"]))
                                    res = cursor_h.fetchone()
                                    if res:
                                        marca = " ✏️" if res[2] == "manual" else ""
                                        fila[d] = f"{res[0]}\n({res[1]}){marca}"
                                    else:
                                        fila[d] = "-"
                            tabla_matriz.append(fila)

                        df_final_matriz = pd.DataFrame(tabla_matriz)
                        skel_placeholder_sec.empty()

                        st.markdown(f"##### Vista de Horario en Cuadrícula: **{sec_elegida}**")
                        st.caption("✏️ = bloque editado manualmente (protegido de la regeneración automática)")
                        st.dataframe(df_final_matriz, width='stretch', hide_index=True)
                    else:
                        st.info("No hay secciones creadas para visualizar.")

                # =====================================================================
                # SUB-PESTAÑA 2: VISTA Y EDICIÓN MANUAL POR DOCENTE
                # =====================================================================
                with sub_tab_doc:

                    cursor_h.execute("SELECT id, nombre FROM docente ORDER BY nombre")
                    docs_db = cursor_h.fetchall()

                    if not docs_db:
                        st.info("No hay docentes registrados para visualizar.")
                    else:
                        mapa_docs = {str(r[1]): r[0] for r in docs_db}
                        docs_nombres = list(mapa_docs.keys())
                        doc_ids_ordenados = [r[0] for r in docs_db]

                        if "doc_vis_seleccionado_id" not in st.session_state or \
                                st.session_state["doc_vis_seleccionado_id"] not in doc_ids_ordenados:
                            st.session_state["doc_vis_seleccionado_id"] = doc_ids_ordenados[0]

                        idx_doc_actual = doc_ids_ordenados.index(st.session_state["doc_vis_seleccionado_id"])

                        doc_elegido = st.selectbox("Seleccionar Docente a Consultar", docs_nombres,
                                                   index=idx_doc_actual,
                                                   key="select_vis_doc_unificado")
                        doc_id_sel = mapa_docs[doc_elegido]
                        st.session_state["doc_vis_seleccionado_id"] = doc_id_sel

                        dias_semana = DIAS_SEMANA
                        tabla_matriz_doc = []

                        skel_placeholder_doc = st.empty()
                        mostrar_skeleton(skel_placeholder_doc, n_lineas=8,
                                         titulo="Cargando horario del docente...")

                        for b in MEDIOS_BLOQUES_VIS:
                            fila = {"Hora / Bloque": b["hora"]}
                            if b["es_pausa"]:
                                for d in dias_semana:
                                    fila[d] = b["tipo"]
                            else:
                                for d in dias_semana:
                                    cursor_h.execute("""
                                                     SELECT m.nombre, s.nombre, h.origen
                                                     FROM horario h
                                                              JOIN materia m ON h.materia_id = m.id
                                                              JOIN seccion s ON h.seccion_id = s.id
                                                     WHERE h.docente_id = ?
                                                       AND h.dia = ?
                                                       AND h.bloque_id = ?
                                                     """, (doc_id_sel, d, b["id"]))
                                    res = cursor_h.fetchone()
                                    if res:
                                        marca = " ✏️" if res[2] == "manual" else ""
                                        fila[d] = f"{res[0]}\n({res[1]}){marca}"
                                    else:
                                        fila[d] = "-"
                            tabla_matriz_doc.append(fila)

                        df_doc_matriz = pd.DataFrame(tabla_matriz_doc)
                        skel_placeholder_doc.empty()

                        st.markdown(f"##### Vista de Horario en Cuadrícula: **{doc_elegido}**")
                        st.caption("✏️ = bloque editado manualmente (protegido de la regeneración automática)")
                        st.dataframe(df_doc_matriz, width='stretch', hide_index=True)

                        st.divider()
                        st.markdown("##### ✏️ Editar Bloque Manualmente")
                        st.caption(
                            "Este cambio se guarda directamente en el horario de secciones y se respeta en futuras regeneraciones.")

                        # Cargas académicas de este docente, para poblar el selector de sección/materia
                        cursor_h.execute("""
                            SELECT ca.seccion_id, s.nombre, ca.materia_id, m.nombre
                            FROM cargaacademica ca
                            JOIN seccion s ON ca.seccion_id = s.id
                            JOIN materia m ON ca.materia_id = m.id
                            WHERE ca.docente_id = ?
                            ORDER BY s.nombre
                        """, (doc_id_sel,))
                        cargas_doc = cursor_h.fetchall()

                        if not cargas_doc:
                            st.info("Este docente no tiene cargas académicas asignadas todavía.")
                        else:
                            col_e1, col_e2 = st.columns(2)
                            with col_e1:
                                dia_edit = st.selectbox("Día", DIAS_SEMANA, key="edit_doc_dia")
                            with col_e2:
                                opciones_bloque = {f'{b["hora"]}': b["id"] for b in MEDIOS_BLOQUES}
                                bloque_edit_label = st.selectbox("Bloque", list(opciones_bloque.keys()),
                                                                  key="edit_doc_bloque")
                                bloque_edit_id = opciones_bloque[bloque_edit_label]

                            # Mostrar qué hay actualmente en ese día/bloque para este docente
                            cursor_h.execute("""
                                             SELECT s.nombre, m.nombre
                                             FROM horario h
                                                      JOIN seccion s ON h.seccion_id = s.id
                                                      JOIN materia m ON h.materia_id = m.id
                                             WHERE h.docente_id = ? AND h.dia = ? AND h.bloque_id = ?
                                             """, (doc_id_sel, dia_edit, bloque_edit_id))
                            actual = cursor_h.fetchone()
                            if actual:
                                st.info(f"Actualmente en ese bloque: **{actual[1]}** en la sección **{actual[0]}**")
                            else:
                                st.info("Ese bloque está libre actualmente para este docente.")

                            accion_edit = st.radio("Acción", ["Asignar / Cambiar sección", "Vaciar este bloque"],
                                                    key="edit_doc_accion", horizontal=True)

                            if accion_edit == "Asignar / Cambiar sección":
                                opciones_carga = {
                                    f"{nombre_sec} — {nombre_mat}": (sec_id_c, mat_id_c)
                                    for sec_id_c, nombre_sec, mat_id_c, nombre_mat in cargas_doc
                                }
                                carga_label = st.selectbox("Sección y materia a asignar",
                                                           list(opciones_carga.keys()),
                                                           key="edit_doc_carga_sel")
                                sec_id_nueva, mat_id_nueva = opciones_carga[carga_label]

                                if st.button("💾 Guardar Cambio", type="primary", key="btn_guardar_edit_doc"):
                                    # --- REGLA DE ORO: la sección destino no puede tener ya otro docente en ese día/bloque ---
                                    cursor_h.execute("""
                                                     SELECT doc.nombre
                                                     FROM horario h
                                                              JOIN docente doc ON h.docente_id = doc.id
                                                     WHERE h.seccion_id = ? AND h.dia = ? AND h.bloque_id = ?
                                                       AND h.docente_id != ?
                                                     """, (sec_id_nueva, dia_edit, bloque_edit_id, doc_id_sel))
                                    choque = cursor_h.fetchone()

                                    if choque:
                                        st.error(
                                            f"❌ No se puede guardar: la sección elegida ya tiene clase con **{choque[0]}** "
                                            f"en ese mismo día y bloque. Elige otro bloque o libera esa sección primero.")
                                    else:
                                        hora_texto_edit = next(
                                            b["hora"] for b in MEDIOS_BLOQUES if b["id"] == bloque_edit_id)
                                        # Se quita cualquier clase previa de ESTE docente en ese día/bloque
                                        # (garantiza que nunca quede en 2 secciones a la vez)
                                        cursor_h.execute(
                                            "DELETE FROM horario WHERE docente_id = ? AND dia = ? AND bloque_id = ?",
                                            (doc_id_sel, dia_edit, bloque_edit_id))
                                        cursor_h.execute(
                                            "INSERT INTO horario (seccion_id, docente_id, materia_id, dia, bloque_id, hora_texto, origen) VALUES (?, ?, ?, ?, ?, ?, 'manual')",
                                            (sec_id_nueva, doc_id_sel, mat_id_nueva, dia_edit, bloque_edit_id,
                                             hora_texto_edit))
                                        conn_h.commit()
                                        st.success("✅ Bloque actualizado y marcado como manual.")
                                        st.rerun()
                            else:
                                if st.button("🗑️ Vaciar Bloque", key="btn_vaciar_edit_doc"):
                                    cursor_h.execute(
                                        "DELETE FROM horario WHERE docente_id = ? AND dia = ? AND bloque_id = ?",
                                        (doc_id_sel, dia_edit, bloque_edit_id))
                                    conn_h.commit()
                                    st.success("✅ Bloque vaciado.")
                                    st.rerun()

            conn_h.close()

        except Exception as ex:
            st.error(f"Error operando el módulo de horarios: {ex}")

        st.stop()

if current_user.role == "Admin" and st.session_state['admin_view'] == 'asistencia':
    if st.button("⬅️ Volver al Panel Principal NEXUS", key="btn_back_from_asistencia_view"):
        st.session_state['admin_view'] = 'dashboard'
        st.rerun()
    st.divider()
# -----------------------------------------------------------------------------
# 3. PESTAÑAS DEL MÓDULO DE ASISTENCIA (DOCENTES Y ADMIN)
# -----------------------------------------------------------------------------
available_tabs = []

if current_user.role == "Admin":
    available_tabs.append("🎓 Carga y Gestión de Estudiantes")
    available_tabs.append("👨‍🏫 Carga y Gestión de Docentes")
    available_tabs.append("🚦 Semáforo Institucional")

if current_user.role in ["Docente", "Admin"] and current_user.assigned_section:
    available_tabs.append("📋 Mi Sección (Orientador)")
    available_tabs.append("📝 Gestión de Permisos")

tabs = st.tabs(available_tabs)

# TAB: CARGA Y GESTIÓN DE ESTUDIANTES (EXCEL Y SECCIONES)
if "🎓 Carga y Gestión de Estudiantes" in available_tabs:
    idx = available_tabs.index("🎓 Carga y Gestión de Estudiantes")
    with tabs[idx]:
        st.subheader("🎓 Carga Masiva y Gestión de Estudiantes")

        import sqlite3

        lista_secciones = []
        lista_docentes_horarios = []

        try:
            conn_h = sqlite3.connect("modulo_horarios/database.db")
            cursor_h = conn_h.cursor()

            cursor_h.execute("SELECT nombre FROM seccion")
            resultados_sec = cursor_h.fetchall()
            lista_secciones = [row[0] for row in resultados_sec if row[0]]

            cursor_h.execute("SELECT nombre FROM docente")
            resultados_doc = cursor_h.fetchall()
            lista_docentes_horarios = [row[0] for row in resultados_doc if row[0]]

            conn_h.close()
        except Exception as e:
            st.error(f"Error conectando a la BD de horarios: {e}")

        if not lista_secciones:
            lista_secciones = ["Sin Secciones Creadas en Horarios"]

        if not lista_docentes_horarios:
            lista_docentes_horarios = ["Sin Docentes Creados en Horarios"]

        # --- Docentes REALES (cuentas ya creadas con su NIP en 'Carga y Gestión de
        # Docentes'). Es lo que debe usarse para asignar el orientador — nunca el
        # nombre suelto del módulo de horarios, que no es un usuario válido. ---
        docentes_reales = session.query(User).filter_by(role="Docente").order_by(User.username).all()
        mapa_docentes_reales = {u.username: u.username for u in docentes_reales}
        opciones_docente_real = list(mapa_docentes_reales.keys()) if mapa_docentes_reales else [
            "Sin Docentes Registrados (créalos primero en 'Carga y Gestión de Docentes')"
        ]

        col_ex1, col_ex2 = st.columns([1, 1])

        with col_ex1:
            with st.container(border=True):
                st.markdown("#### 📥 Importar Nómina desde Excel")

                seccion_destino = st.selectbox("1. Asignar a la Sección:", lista_secciones,
                                               key="sb_excel_section_dest_upload")
                docente_orientador_label = st.selectbox("2. Asignar Docente Orientador:", opciones_docente_real,
                                                        key="sb_excel_docente_orientador_select")
                docente_orientador_nip = mapa_docentes_reales.get(docente_orientador_label)
                uploaded_file = st.file_uploader("3. Seleccionar archivo Excel (.xlsx)", type=["xlsx", "xls"],
                                                 key="excel_file_uploader_input")

                if st.button("🚀 Cargar e Importar Alumnos", type="primary", width='stretch',
                             key="btn_import_excel_action"):
                    if uploaded_file is not None and lista_secciones != ["Sin Secciones Creadas en Horarios"]:
                        try:
                            df = pd.read_excel(uploaded_file)
                            df.columns = [str(c).strip().lower() for c in df.columns]

                            col_nie = next((c for c in df.columns if 'nie' in c or 'id' in c or 'codigo' in c), None)
                            col_nom = next(
                                (c for c in df.columns if 'nombre' in c or 'alumno' in c or 'estudiante' in c), None)

                            if col_nie and col_nom:
                                nuevos = 0
                                actualizados = 0
                                for _, r in df.iterrows():
                                    nie_val = str(r[col_nie]).strip()
                                    nom_val = str(r[col_nom]).strip()

                                    if pd.notna(r[col_nie]) and pd.notna(r[col_nom]):
                                        est = session.query(Student).filter_by(id=nie_val).first()
                                        if not est:
                                            session.add(Student(id=nie_val, name=nom_val, section=seccion_destino))

                                            user_existente = session.query(User).filter_by(username=nie_val).first()
                                            if not user_existente:
                                                nuevo_user_alumno = User(
                                                    username=nie_val,
                                                    password=hash_password("indet2026"),
                                                    role="Alumno",
                                                    assigned_section=seccion_destino,
                                                    student_id=nie_val
                                                )
                                                session.add(nuevo_user_alumno)

                                            nuevos += 1
                                        else:
                                            est.section = seccion_destino
                                            user_existente = session.query(User).filter_by(username=nie_val).first()
                                            if user_existente:
                                                user_existente.assigned_section = seccion_destino
                                            actualizados += 1
                                if docente_orientador_nip:
                                    doc_user = session.query(User).filter_by(
                                        username=docente_orientador_nip, role="Docente").first()
                                    if doc_user:
                                        doc_user.assigned_section = seccion_destino

                                session.commit()
                                st.success(
                                    f"✅ Proceso completado: {nuevos} registrados, {actualizados} actualizados en "
                                    f"{seccion_destino} (Orientador: {docente_orientador_nip or 'ninguno asignado'}).")
                                st.rerun()
                            else:
                                st.error("El archivo debe incluir las columnas 'NIE' y 'Nombre'.")
                        except Exception as ex:
                            st.error(f"Error procesando el archivo: {ex}")
                    else:
                        st.warning("Selecciona un archivo Excel y asegúrate de tener secciones creadas.")

            with st.container(border=True):
                st.markdown("#### ✍️ Registro Manual de Estudiante")

                with st.form("form_registro_manual_estudiante"):
                    manual_nie = st.text_input("NIE del Estudiante")
                    manual_nombre = st.text_input("Nombre Completo")
                    manual_seccion = st.selectbox("Sección Asignada", lista_secciones, key="sb_manual_reg_section")
                    manual_orientador_label = st.selectbox("Docente Orientador Asignado", opciones_docente_real,
                                                           key="sb_manual_reg_orientador")
                    manual_orientador_nip = mapa_docentes_reales.get(manual_orientador_label)

                    submit_manual = st.form_submit_button("➕ Registrar Estudiante Individual", width='stretch')

                    if submit_manual:
                        if manual_nie.strip() and manual_nombre.strip():
                            if lista_secciones == ["Sin Secciones Creadas en Horarios"]:
                                st.error("No hay secciones disponibles para asignar.")
                            else:
                                est_existente = session.query(Student).filter_by(id=manual_nie.strip()).first()
                                if est_existente:
                                    st.error(f"Ya existe un estudiante registrado con el NIE: {manual_nie}")
                                else:
                                    nuevo_est = Student(id=manual_nie.strip(), name=manual_nombre.strip(),
                                                        section=manual_seccion)
                                    session.add(nuevo_est)

                                    user_existente = session.query(User).filter_by(username=manual_nie.strip()).first()
                                    if not user_existente:
                                        nuevo_user_alumno = User(
                                            username=manual_nie.strip(),
                                            password=hash_password("indet2026"),
                                            role="Alumno",
                                            assigned_section=manual_seccion,
                                            student_id=manual_nie.strip()
                                        )
                                        session.add(nuevo_user_alumno)

                                    if manual_orientador_nip:
                                        doc_user = session.query(User).filter_by(
                                            username=manual_orientador_nip, role="Docente").first()
                                        if doc_user:
                                            doc_user.assigned_section = manual_seccion

                                    session.commit()
                                    st.success(f"¡Estudiante {manual_nombre.strip()} registrado con éxito!")
                                    st.rerun()
                        else:
                            st.warning("Por favor completa el NIE y el Nombre Completo del estudiante.")

        with col_ex2:
            with st.container(border=True):
                st.markdown("#### 🔍 Consultar y Filtrar Alumnos")
                sec_filtro = st.selectbox("Filtrar por sección:", ["Todas"] + lista_secciones,
                                          key="sb_filter_students_sec")

                q_st = session.query(Student)
                if sec_filtro != "Todas":
                    q_st = q_st.filter(Student.section == sec_filtro)
                estudiantes_lista = q_st.all()

                st.write(f"Total registrados: **{len(estudiantes_lista)}**")

                # --- BÚSQUEDA Y MUESTRA DEL DOCENTE ORIENTADOR ---
                docente_orientador_asignado = "No asignado"
                if sec_filtro != "Todas":
                    doc_encontrado = session.query(User).filter_by(assigned_section=sec_filtro, role="Docente").first()
                    if doc_encontrado:
                        docente_orientador_asignado = doc_encontrado.username
                else:
                    docente_orientador_asignado = "Seleccione una sección específica"

                st.markdown(f"👨‍🏫 Docente Orientador: **{docente_orientador_asignado}**")
                # ------------------------------------------------

                if estudiantes_lista:

                    datos_tabla = []
                    for e in estudiantes_lista:
                        user_info = session.query(User).filter_by(username=str(e.id)).first()
                        datos_tabla.append({
                            "NIE": e.id,
                            "Nombre": e.name,
                            "Sección": e.section,
                            "Usuario": user_info.username if user_info else "N/A",
                            "Contraseña": user_info.password if user_info else "N/A"
                        })

                    df_display = pd.DataFrame(datos_tabla)
                    st.dataframe(df_display, width='stretch', height=250)

                st.divider()

                if st.button("🗑️ Borrar Todos los Estudiantes", type="secondary", key="btn_clear_all_students_confirm",
                             width='stretch'):
                    try:
                        todos_los_ids_est = [e.id for e in session.query(Student.id).all()]

                        if not todos_los_ids_est:
                            st.info("No hay estudiantes registrados para borrar.")
                        else:
                            # Se borra primero todo lo que depende del estudiante (respetando el
                            # orden de llaves foráneas), y al final el estudiante mismo.
                            session.query(QRAttendanceCheckin).filter(
                                QRAttendanceCheckin.student_id.in_(todos_los_ids_est)).delete(
                                synchronize_session=False)
                            session.query(Matricula).filter(
                                Matricula.student_id.in_(todos_los_ids_est)).delete(
                                synchronize_session=False)
                            session.query(ActaAsistencia).filter(
                                ActaAsistencia.student_id.in_(todos_los_ids_est)).delete(
                                synchronize_session=False)
                            session.query(CaseTracker).filter(
                                CaseTracker.student_id.in_(todos_los_ids_est)).delete(
                                synchronize_session=False)
                            session.query(Attendance).filter(
                                Attendance.student_id.in_(todos_los_ids_est)).delete(
                                synchronize_session=False)
                            session.query(Justification).filter(
                                Justification.student_id.in_(todos_los_ids_est)).delete(
                                synchronize_session=False)
                            # Cuentas de acceso (rol Alumno) ligadas a estos estudiantes
                            session.query(User).filter(
                                User.student_id.in_(todos_los_ids_est), User.role == "Alumno").delete(
                                synchronize_session=False)
                            session.query(Student).filter(
                                Student.id.in_(todos_los_ids_est)).delete(synchronize_session=False)

                            session.commit()
                            st.success(
                                f"✅ Se borraron {len(todos_los_ids_est)} estudiante(s) y todo su historial "
                                f"relacionado (asistencias, permisos, casos, actas y cuentas de acceso).")
                            st.rerun()
                    except Exception as err:
                        session.rollback()
                        st.error(f"Error al borrar los estudiantes: {err}")

                st.divider()
                st.markdown("#### 🔐 Gestión de Credenciales de Alumnos")

                with st.container(border=True):
                    st.markdown("##### 👤 Cambio Individual de Contraseña")
                    todos_alumnos_cred = session.query(Student).all()

                    if todos_alumnos_cred:
                        mapa_alumnos = {f"{e.name} (NIE: {e.id})": e.id for e in todos_alumnos_cred}
                        alumno_seleccionado = st.selectbox("Seleccionar Alumno", list(mapa_alumnos.keys()),
                                                           key="sb_cambio_pass_alumno")
                        nie_elegido = mapa_alumnos[alumno_seleccionado]

                        nueva_pass_ind = st.text_input("Nueva Contraseña", type="password",
                                                       key="txt_nueva_pass_individual")

                        if st.button("💾 Actualizar Contraseña Alumno", width='stretch',
                                     key="btn_update_single_pass"):
                            if nueva_pass_ind.strip():
                                usr_al = session.query(User).filter_by(username=str(nie_elegido)).first()
                                if usr_al:
                                    usr_al.password = hash_password(nueva_pass_ind.strip())
                                    session.commit()
                                    st.success(f"¡Contraseña actualizada para el alumno con NIE {nie_elegido}!")
                                    st.rerun()
                                else:
                                    st.warning("No se encontró un usuario asociado a este NIE.")
                            else:
                                st.warning("Escribe una contraseña válida.")
                    else:
                        st.info("No hay alumnos registrados.")

                    st.divider()

                    st.markdown("##### 🔄 Restablecimiento Masivo")
                    st.write("Asigna una nueva contraseña a **todos** los alumnos registrados de forma simultánea.")

                    nueva_pass_masiva = st.text_input("Contraseña para el Cambio Masivo", type="password",
                                                      key="txt_nueva_pass_masiva")

                    if st.button("⚠️ Aplicar Contraseña a Todos", type="secondary", width='stretch',
                                 key="btn_reset_all_passwords"):
                        if nueva_pass_masiva.strip():
                            try:
                                alumnos_users = session.query(User).filter_by(role="Alumno").all()
                                contador_resets = 0
                                for u in alumnos_users:
                                    u.password = hash_password(nueva_pass_masiva.strip())
                                    contador_resets += 1
                                session.commit()
                                st.success(
                                    f"¡Se ha actualizado la contraseña para {contador_resets} credenciales de alumnos con éxito!")
                                st.rerun()
                            except Exception as ex:
                                session.rollback()
                                st.error(f"Error al actualizar las contraseñas: {ex}")
                        else:
                            st.warning("Por favor ingresa una contraseña válida para el cambio masivo.")

# -------------------------------------------------------------
# TAB: MI SECCIÓN (ORIENTADOR)
# -------------------------------------------------------------
if "📋 Mi Sección (Orientador)" in available_tabs:
    idx = available_tabs.index("📋 Mi Sección (Orientador)")
    with tabs[idx]:
        st.subheader(f"📋 Panel del Docente Orientador — Sección: {current_user.assigned_section}")

        # --- RECONCILIAR AUSENCIAS DE BLOQUES YA PASADOS HOY (una vez por carga) ---
        try:
            reconciliar_ausencias_seccion(session, current_user.assigned_section)
        except Exception:
            session.rollback()

        # --- BANNER DE ALERTAS ACCIONABLES ---
        alertas_docente = get_alertas_docente(session, current_user.assigned_section)
        if alertas_docente:
            with st.container(border=True):
                st.markdown(f"#### 🔔 {len(alertas_docente)} Alerta(s) que requieren tu atención")
                for al in alertas_docente:
                    if al['tipo'] == 'ausencia_hoy':
                        st.warning(al['detalle'])
                    else:
                        st.error(al['detalle'])
        else:
            st.success("✅ Sin alertas pendientes por ahora: nadie faltó hoy sin justificar y no hay casos en riesgo alto sin seguimiento.")

        # --- GENERADOR DE QR DE ASISTENCIA RÁPIDA ---
        st.markdown("---")
        st.markdown("#### 📷 Generar QR de Asistencia Rápida")
        st.caption(
            "Genera un código QR válido por 5 minutos para que los estudiantes de tu sección tomen asistencia "
            "escaneándolo con la cámara de su celular. Se sigue exigiendo que estén dentro del instituto "
            "(GPS) y que escaneen desde su propio dispositivo vinculado.")

        materia_detectada_nombre = detectar_materia_actual_horarios(current_user.assigned_section)
        todas_las_materias = session.query(Subject).order_by(Subject.name).all()
        mapa_materias = {m.name: m.id for m in todas_las_materias}

        if materia_detectada_nombre:
            st.success(f"🕒 Según el horario, ahora mismo toca: **{materia_detectada_nombre}**")
        else:
            st.warning(
                "🕒 No se detectó una clase activa en tu horario para este momento "
                "(puede ser receso, almuerzo, o que el horario de tu sección no esté cargado). "
                "Elige la materia manualmente abajo.")

        opciones_materia_qr = list(mapa_materias.keys())
        if materia_detectada_nombre and materia_detectada_nombre not in opciones_materia_qr:
            opciones_materia_qr.insert(0, materia_detectada_nombre)
        if not opciones_materia_qr:
            opciones_materia_qr = ["(Sin materias registradas)"]

        idx_materia_default = (
            opciones_materia_qr.index(materia_detectada_nombre)
            if materia_detectada_nombre in opciones_materia_qr else 0
        )
        materia_elegida_qr = st.selectbox(
            "Materia para este QR (detectada automáticamente, puedes cambiarla)",
            opciones_materia_qr, index=idx_materia_default, key="sb_materia_qr_manual"
        )

        if st.button("📷 Generar Nuevo QR de Asistencia", key="btn_generar_qr_asistencia"):
            try:
                subject_para_qr = obtener_o_crear_subject_por_nombre(session, materia_elegida_qr)

                nuevo_token_qr = secrets.token_urlsafe(16)
                ahora_qr = datetime.now()
                expiracion_qr = ahora_qr + timedelta(minutes=QR_TOKEN_VALIDEZ_MINUTOS)
                nuevo_qr_row = QRAttendanceToken(
                    token=nuevo_token_qr,
                    seccion=current_user.assigned_section,
                    subject_id=subject_para_qr.id,
                    docente_username=getattr(current_user, 'username', None),
                    created_at=ahora_qr,
                    expires_at=expiracion_qr,
                    ciclo_id=get_ciclo_activo(session).id
                )
                session.add(nuevo_qr_row)
                session.commit()
                st.session_state['qr_token_activo'] = nuevo_token_qr
                st.session_state['qr_token_expira'] = expiracion_qr.isoformat()
                st.session_state['qr_token_materia'] = materia_elegida_qr
                st.rerun()
            except Exception:
                session.rollback()
                st.error(
                    "❌ No se pudo generar el QR por un problema de conexión con la base de datos. "
                    "Intenta de nuevo en unos segundos.")

        if st.session_state.get('qr_token_activo'):
            expira_dt_qr = datetime.fromisoformat(st.session_state['qr_token_expira'])
            segundos_restantes_qr = (expira_dt_qr - datetime.now()).total_seconds()

            if segundos_restantes_qr <= 0:
                st.warning("⌛ El último QR generado ya expiró. Genera uno nuevo cuando lo necesites.")
            else:
                url_checkin_qr = f"{APP_BASE_URL}?attendance_token={st.session_state['qr_token_activo']}"
                try:
                    import qrcode
                    img_qr_obj = qrcode.make(url_checkin_qr)
                    buf_qr = io.BytesIO()
                    img_qr_obj.save(buf_qr, format="PNG")
                    col_qr1, col_qr2 = st.columns([1, 2])
                    with col_qr1:
                        st.image(buf_qr.getvalue(),
                                 caption=f"Válido por ~{int(segundos_restantes_qr)} segundos más", width=220)
                    with col_qr2:
                        st.caption(f"📚 Materia: **{st.session_state.get('qr_token_materia', '-')}**")
                        st.caption("Si el QR no se puede escanear, comparte este enlace directamente:")
                        st.code(url_checkin_qr, language=None)

                        token_row_activo = session.query(QRAttendanceToken).filter_by(
                            token=st.session_state['qr_token_activo']).first()
                        if token_row_activo:
                            total_canjes = session.query(QRAttendanceCheckin).filter_by(
                                token_id=token_row_activo.id).count()
                            st.metric("Estudiantes ya registrados con este QR", total_canjes)
                except ImportError:
                    st.error(
                        "Falta instalar la librería 'qrcode'. Ejecuta en tu terminal: pip install \"qrcode[pil]\"")

        # --- HERRAMIENTA: REINICIAR DISPOSITIVO VINCULADO DE UN ESTUDIANTE ---
        with st.expander("🔧 Reiniciar dispositivo vinculado de un estudiante"):
            st.caption(
                "Usa esto si un estudiante cambió de celular y el sistema ya no le deja escanear el QR "
                "de asistencia porque está vinculado a su equipo anterior.")
            estudiantes_para_reset = session.query(Student).filter_by(
                section=current_user.assigned_section).all()
            if estudiantes_para_reset:
                mapa_reset = {f"{e.name} (NIE: {e.id})": e for e in estudiantes_para_reset}
                est_reset_label = st.selectbox("Estudiante", list(mapa_reset.keys()),
                                               key="sb_reset_dispositivo_est")
                est_reset_obj = mapa_reset[est_reset_label]
                if st.button("🔄 Reiniciar dispositivo vinculado", key="btn_reset_dispositivo"):
                    usuario_est_reset = session.query(User).filter_by(student_id=est_reset_obj.id).first()
                    if usuario_est_reset:
                        usuario_est_reset.device_id = None
                        session.commit()
                        st.success(
                            f"✅ Dispositivo reiniciado para {est_reset_obj.name}. "
                            f"La próxima vez que escanee un QR, se vinculará su nuevo celular.")
                    else:
                        st.warning("Este estudiante no tiene una cuenta de usuario asociada.")

        my_students = session.query(Student).filter_by(section=current_user.assigned_section).all()
        st.write(f"Total de Estudiantes Tutorados: **{len(my_students)}**")

        if not my_students:
            st.info("No hay estudiantes registrados en tu sección asignada.")
        else:
            # Selector de fecha para consultar y modificar la asistencia diaria
            fecha_consulta = st.date_input("Seleccionar Fecha a Consultar", value=date.today(),
                                           key="date_asistencia_orientador_diaria")

            st.markdown("---")
            st.markdown(f"### 📊 Control y Modificación de Asistencia del Día: {fecha_consulta.strftime('%d/%m/%Y')}")
            st.info(
                "💡 Como docente orientador, puedes modificar manualmente el estado de asistencia de cada alumno si se presenta un retraso o justificación.")

            opciones_estado = ["Presente", "Tardanza", "Ausente", "Permiso"]

            # Listado interactivo por estudiante para modificar el estado
            for est in my_students:
                # Buscamos si ya tiene un registro para esta fecha exacta
                reg_existente = session.query(Attendance).filter_by(student_id=est.id, date=fecha_consulta).first()

                estado_actual = reg_existente.status if reg_existente else "Ausente"
                if estado_actual not in opciones_estado:
                    estado_actual = "Ausente"

                idx_default = opciones_estado.index(estado_actual)

                with st.container(border=True):
                    col_e1, col_e2, col_e3 = st.columns([1, 3, 2])
                    with col_e1:
                        st.markdown(f"**NIE:** `{est.id}`")
                    with col_e2:
                        st.markdown(f"**Estudiante:** {est.name}")
                    with col_e3:
                        nuevo_estado = st.selectbox(
                            "Estado",
                            options=opciones_estado,
                            index=idx_default,
                            key=f"sel_mod_estado_{est.id}_{fecha_consulta}_{estado_actual}",
                            label_visibility="collapsed"
                        )

                        # Si el docente cambia el selector, se actualiza automáticamente en la BD
                        if nuevo_estado != estado_actual:
                            if reg_existente:
                                reg_existente.status = nuevo_estado
                            else:
                                nuevo_reg = Attendance(
                                    student_id=est.id,
                                    date=fecha_consulta,
                                    status=nuevo_estado,
                                    ciclo_id=get_ciclo_activo(session).id
                                )
                                session.add(nuevo_reg)
                            session.commit()
                            st.success(f"¡Actualizado a {nuevo_estado}!")
                            st.rerun()

            st.markdown("---")
            st.markdown("### 🔍 Análisis de Patrones de Comportamiento e Individual")

            # Selector de estudiante para ver su expediente y patrones detallados
            mapa_nombres_est = {f"{e.name} (NIE: {e.id})": e for e in my_students}
            est_labels_detalle = list(mapa_nombres_est.keys())
            est_ids_detalle_ordenados = [e.id for e in my_students]

            if "est_detalle_seleccionado_id" not in st.session_state or \
                    st.session_state["est_detalle_seleccionado_id"] not in est_ids_detalle_ordenados:
                st.session_state["est_detalle_seleccionado_id"] = est_ids_detalle_ordenados[0]

            idx_est_detalle_actual = est_ids_detalle_ordenados.index(
                st.session_state["est_detalle_seleccionado_id"])

            est_seleccionado_label = st.selectbox("Seleccionar Estudiante para Diagnóstico Detallado",
                                                  est_labels_detalle,
                                                  index=idx_est_detalle_actual,
                                                  key="sb_orientador_select_alumno_detalle")
            est_obj = mapa_nombres_est[est_seleccionado_label]
            st.session_state["est_detalle_seleccionado_id"] = est_obj.id

            # Obtenemos todo el historial de asistencia del estudiante
            historial_est = session.query(Attendance).filter_by(student_id=est_obj.id).all()

            resultado_riesgo_est = evaluate_student_risk_detailed(session, est_obj.id)
            render_badge_riesgo(resultado_riesgo_est['status'], resultado_riesgo_est['pct'])

            total_asistencias = len(historial_est)
            presentes = sum(1 for h in historial_est if h.status == "Presente")
            ausentes = sum(1 for h in historial_est if h.status == "Ausente")
            tardanzas = sum(1 for h in historial_est if h.status == "Tardanza")
            permisos_count = sum(1 for h in historial_est if h.status == "Permiso")

            col_p1, col_p2, col_p3, col_p4 = st.columns(4)
            col_p1.metric("Total Registros", total_asistencias)
            col_p2.metric("Asistencias / Presente", presentes)
            col_p3.metric("Tardanzas / Ausencias", f"{tardanzas} / {ausentes}")
            col_p4.metric("Permisos Justificados", permisos_count)

            st.markdown("#### 🧠 Patrones Detectados en el Estudiante:")
            patrones = []

            if tardanzas >= 2:
                patrones.append(f"⏱️ **Llegadas tardías frecuentes:** Registra {tardanzas} tardanzas acumuladas.")
            if ausentes >= 2:
                patrones.append(f"⚠️ **Ausencias recurrentes:** Cuenta con {ausentes} inasistencias registradas.")

            # Patrón de días de la semana con faltas
            if historial_est:
                dias_faltas = [h.date.strftime('%A') for h in historial_est if h.status == "Ausente"]
                if dias_faltas:
                    dia_mas_frecuente = max(set(dias_faltas), key=dias_faltas.count)
                    patrones.append(f"📅 **Patrón por día:** Tiende a faltar o fallar más los días {dia_mas_frecuente}.")

            if not patrones:
                patrones.append(
                    "🟢 **Comportamiento regular:** No se detectan anomalías graves ni patrones de riesgo en este periodo.")

            for pat in patrones:
                st.info(pat)

            st.markdown("---")
            st.markdown("#### 📄 Acta de Asistencia")
            st.caption(
                "Genera un acta en PDF con los datos del estudiante, el resumen de asistencia, el detalle de "
                "inasistencias/tardanzas/permisos con fecha, los patrones de riesgo detectados, acuerdos y "
                "compromisos, y el seguimiento respecto al acta anterior (si existe).")

            resultado_detallado_acta = evaluate_student_risk_detailed(session, est_obj.id)
            acuerdos_sugeridos, compromisos_sugeridos = sugerir_acuerdos_compromisos(
                resultado_detallado_acta['tags'])
            # Rellenar a 2 elementos siempre, por si sugirió menos
            while len(acuerdos_sugeridos) < 2:
                acuerdos_sugeridos.append("")
            while len(compromisos_sugeridos) < 2:
                compromisos_sugeridos.append("")

            st.markdown("##### 🤝 Acuerdos del Estudiante")
            st.caption("Sugeridos automáticamente según los patrones detectados. Puedes editarlos.")
            col_ac1, col_ac2 = st.columns(2)
            with col_ac1:
                acuerdo_1 = st.text_area("Acuerdo sugerido 1", value=acuerdos_sugeridos[0],
                                         key=f"acuerdo1_{est_obj.id}", height=80)
            with col_ac2:
                acuerdo_2 = st.text_area("Acuerdo sugerido 2", value=acuerdos_sugeridos[1],
                                         key=f"acuerdo2_{est_obj.id}", height=80)
            col_ac3, col_ac4 = st.columns(2)
            with col_ac3:
                acuerdo_manual_1 = st.text_area("Acuerdo adicional (manual)", value="",
                                                key=f"acuerdo_manual1_{est_obj.id}", height=80)
            with col_ac4:
                acuerdo_manual_2 = st.text_area("Acuerdo adicional (manual)", value="",
                                                key=f"acuerdo_manual2_{est_obj.id}", height=80)

            st.markdown("##### 🏫 Compromisos Institucionales")
            st.caption("Sugeridos automáticamente según los patrones detectados. Puedes editarlos.")
            col_co1, col_co2 = st.columns(2)
            with col_co1:
                compromiso_1 = st.text_area("Compromiso sugerido 1", value=compromisos_sugeridos[0],
                                            key=f"compromiso1_{est_obj.id}", height=80)
            with col_co2:
                compromiso_2 = st.text_area("Compromiso sugerido 2", value=compromisos_sugeridos[1],
                                            key=f"compromiso2_{est_obj.id}", height=80)
            col_co3, col_co4 = st.columns(2)
            with col_co3:
                compromiso_manual_1 = st.text_area("Compromiso adicional (manual)", value="",
                                                    key=f"compromiso_manual1_{est_obj.id}", height=80)
            with col_co4:
                compromiso_manual_2 = st.text_area("Compromiso adicional (manual)", value="",
                                                    key=f"compromiso_manual2_{est_obj.id}", height=80)

            if st.button("📄 Generar Acta de Asistencia (PDF)", key="btn_generar_acta_pdf"):
                skel_placeholder_acta = st.empty()
                mostrar_skeleton(skel_placeholder_acta, n_lineas=6,
                                 titulo="Generando acta de asistencia (calculando patrones, normativa y tendencia)...")
                try:
                    acuerdos_finales = [acuerdo_1, acuerdo_2, acuerdo_manual_1, acuerdo_manual_2]
                    compromisos_finales = [compromiso_1, compromiso_2, compromiso_manual_1, compromiso_manual_2]

                    # Acta anterior de este mismo estudiante, para calcular tendencia
                    acta_anterior = session.query(ActaAsistencia).filter_by(
                        student_id=est_obj.id
                    ).order_by(ActaAsistencia.fecha_generacion.desc()).first()

                    pdf_bytes_acta = generar_acta_asistencia_pdf(
                        session, est_obj, current_user,
                        acuerdos_finales=acuerdos_finales,
                        compromisos_finales=compromisos_finales,
                        acta_anterior=acta_anterior
                    )
                    skel_placeholder_acta.empty()

                    # --- Vincular con un caso de seguimiento (CaseTracker) ---
                    ciclo_activo_acta = get_ciclo_activo(session)
                    caso_abierto = session.query(CaseTracker).filter(
                        CaseTracker.student_id == est_obj.id,
                        CaseTracker.status != 'Cerrado'
                    ).first()
                    if not caso_abierto:
                        caso_abierto = CaseTracker(
                            student_id=est_obj.id,
                            status='Observación',
                            notes=f"Caso abierto automáticamente al generar acta de asistencia el "
                                  f"{date.today().strftime('%d/%m/%Y')}.",
                            created_at=date.today(),
                            ciclo_id=ciclo_activo_acta.id
                        )
                        session.add(caso_abierto)
                        session.flush()  # para obtener caso_abierto.id antes del commit final

                    # --- Guardar el snapshot del acta para poder darle seguimiento después ---
                    nueva_acta = ActaAsistencia(
                        student_id=est_obj.id,
                        fecha_generacion=date.today(),
                        pct_asistencia_snapshot=int(round(resultado_detallado_acta['pct'])),
                        patrones_snapshot="\n".join(patrones) if patrones else "",
                        acuerdos="\n".join([a for a in acuerdos_finales if a and a.strip()]),
                        compromisos="\n".join([c for c in compromisos_finales if c and c.strip()]),
                        case_tracker_id=caso_abierto.id,
                        creado_por=getattr(current_user, 'username', None),
                        ciclo_id=ciclo_activo_acta.id
                    )
                    session.add(nueva_acta)
                    session.commit()

                    st.session_state["acta_pdf_bytes"] = pdf_bytes_acta
                    st.session_state["acta_pdf_estudiante_id"] = est_obj.id
                    st.session_state["acta_pdf_nombre"] = (
                        f"acta_asistencia_{est_obj.name.replace(' ', '_')}_{date.today().strftime('%Y%m%d')}.pdf"
                    )
                    st.success(
                        "✅ Acta generada y vinculada a un caso de seguimiento. Descárgala abajo. "
                        "La próxima acta de este estudiante mostrará automáticamente si mejoró o empeoró.")
                except RuntimeError as err_pdf:
                    skel_placeholder_acta.empty()
                    st.error(f"❌ {err_pdf}")

            if st.session_state.get("acta_pdf_bytes") is not None and \
                    st.session_state.get("acta_pdf_estudiante_id") == est_obj.id:
                st.download_button(
                    label="⬇️ Descargar Acta en PDF",
                    data=st.session_state["acta_pdf_bytes"],
                    file_name=st.session_state.get("acta_pdf_nombre", "acta_asistencia.pdf"),
                    mime="application/pdf",
                    key="btn_descargar_acta_pdf"
                )


# -------------------------------------------------------------
# TAB: GESTIÓN DE PERMISOS Y JUSTIFICACIONES (ORIENTADOR)
# -------------------------------------------------------------
if "📝 Gestión de Permisos" in available_tabs:
    idx_perm = available_tabs.index("📝 Gestión de Permisos")
    with tabs[idx_perm]:
        st.subheader(f"📝 Gestión de Permisos y Justificaciones — Sección: {current_user.assigned_section}")
        st.info(
            "Registra ausencias justificadas por rango de fechas (incapacidades médicas, permisos oficiales, etc.) para que el sistema genere automáticamente el estatus correspondiente.")

        my_students_perm = session.query(Student).filter_by(section=current_user.assigned_section).all()

        if not my_students_perm:
            st.warning("No hay estudiantes registrados en tu sección para gestionar permisos.")
        else:
            mapa_est_perm = {f"{e.name} (NIE: {e.id})": e for e in my_students_perm}
            est_perm_labels = list(mapa_est_perm.keys())
            est_perm_ids_ordenados = [e.id for e in my_students_perm]

            if "est_perm_seleccionado_id" not in st.session_state or \
                    st.session_state["est_perm_seleccionado_id"] not in est_perm_ids_ordenados:
                st.session_state["est_perm_seleccionado_id"] = est_perm_ids_ordenados[0]

            idx_est_perm_actual = est_perm_ids_ordenados.index(st.session_state["est_perm_seleccionado_id"])

            est_perm_label = st.selectbox("Seleccionar Estudiante", est_perm_labels,
                                          index=idx_est_perm_actual,
                                          key="sb_permiso_select_estudiante")
            est_obj_perm = mapa_est_perm[est_perm_label]
            st.session_state["est_perm_seleccionado_id"] = est_obj_perm.id

            st.write(f"Estudiante seleccionado: **{est_obj_perm.name}** (NIE: `{est_obj_perm.id}`)")

            if "permiso_form_reset_counter" not in st.session_state:
                st.session_state["permiso_form_reset_counter"] = 0
            reset_suffix = st.session_state["permiso_form_reset_counter"]

            st.divider()
            with st.form("form_registro_permiso_rango"):
                st.markdown("#### 📅 Definir Rango de Fechas del Permiso / Constancia")

                col_f1, col_f2 = st.columns(2)
                with col_f1:
                    fecha_inicio = st.date_input("Fecha de Inicio", value=date.today(), key="date_permiso_inicio")
                with col_f2:
                    fecha_fin = st.date_input("Fecha de Fin", value=date.today(), key="date_permiso_fin")

                tipo_permiso = st.selectbox(
                    "Motivo / Tipo de Justificación",
                    ["Constancia Médica", "Permiso Familiar", "Actividad Institucional / Deportiva", "Otro Motivo"],
                    key="sb_tipo_permiso_motivo"
                )

                observacion_permiso = st.text_area(
                    "Detalles / Observación de la Justificación",
                    placeholder="Ej. Reposo médico prescrito por el ISSS por 3 días.",
                    key=f"txt_obs_permiso_detalle_{reset_suffix}"
                )

                archivo_evidencia = st.file_uploader(
                    "Adjuntar Evidencia (Constancia Médica, Permiso Oficial, etc.)",
                    type=["png", "jpg", "jpeg", "pdf"],
                    key=f"file_evidencia_permiso_{reset_suffix}",
                    help="Opcional. Se guardará junto a este permiso y quedará disponible en el historial."
                )

                btn_guardar_permiso = st.form_submit_button("🚀 Registrar Permiso y Generar Asistencias Automáticas",
                                                            width='stretch')

                if btn_guardar_permiso:
                    if fecha_inicio > fecha_fin:
                        st.error("❌ La fecha de inicio no puede ser posterior a la fecha de fin.")
                    else:
                        # --- Guardar el archivo de evidencia (si se adjuntó uno) ---
                        ruta_evidencia_guardada = None
                        if archivo_evidencia is not None:
                            carpeta_evidencias = "evidencias_permisos"
                            os.makedirs(carpeta_evidencias, exist_ok=True)
                            extension = os.path.splitext(archivo_evidencia.name)[1]
                            nombre_archivo = (
                                f"est{est_obj_perm.id}_"
                                f"{fecha_inicio.strftime('%Y%m%d')}_{fecha_fin.strftime('%Y%m%d')}"
                                f"_{datetime.now().strftime('%H%M%S')}{extension}"
                            )
                            ruta_evidencia_guardada = os.path.join(carpeta_evidencias, nombre_archivo)
                            with open(ruta_evidencia_guardada, "wb") as f_evid:
                                f_evid.write(archivo_evidencia.getbuffer())

                        # Iterar por cada día del rango seleccionado
                        delta_dias = (fecha_fin - fecha_inicio).days
                        dias_generados = 0

                        for i in range(delta_dias + 1):
                            dia_actual = fecha_inicio + timedelta(days=i)

                            # Opcional: Omitir fines de semana (Sábado=5, Domingo=6) si se desea, o registrar todos los días corridos
                            # Registrarémos todos los días del rango según lo solicite el docente

                            # Verificamos si ya existe un registro de asistencia para este alumno en esta fecha exacta
                            reg_att = session.query(Attendance).filter_by(student_id=est_obj_perm.id,
                                                                          date=dia_actual).first()

                            texto_observacion = f"[{tipo_permiso}] {observacion_permiso}".strip()

                            if not reg_att:
                                nuevo_reg_att = Attendance(
                                    student_id=est_obj_perm.id,
                                    subject_id=1,  # ID por defecto para justificaciones generales del orientador
                                    date=dia_actual,
                                    status="Permiso",
                                    observation=texto_observacion,
                                    evidencia_path=ruta_evidencia_guardada,
                                    ciclo_id=get_ciclo_activo(session).id
                                )
                                session.add(nuevo_reg_att)
                            else:
                                reg_att.subject_id = reg_att.subject_id if reg_att.subject_id else 1
                                reg_att.status = "Permiso"
                                reg_att.observation = texto_observacion
                                if ruta_evidencia_guardada:
                                    reg_att.evidencia_path = ruta_evidencia_guardada
                            dias_generados += 1

                        session.commit()
                        st.success(
                            f"✅ ¡Permiso aplicado con éxito! Se han generado/actualizado los registros de asistencia "
                            f"para {dias_generados} día(s) con estado **Permiso**.")

                        # Forzar que el text_area y el file_uploader nazcan vacíos en el próximo render,
                        # cambiándoles el key (es la forma confiable de resetear un file_uploader en Streamlit)
                        st.session_state["permiso_form_reset_counter"] += 1

                        st.rerun()
        st.divider()
        st.markdown("#### 📋 Historial de Inasistencias y Permisos Registrados del Estudiante")

        if not my_students_perm:
            st.info("ℹ️ No hay estudiantes registrados en tu sección para mostrar historial.")
        else:
            # Consultamos los registros de asistencia de este alumno que contengan una observación de permiso o justificación
            historial_permisos = session.query(Attendance).filter(
                Attendance.student_id == est_obj_perm.id,
                Attendance.observation.isnot(None),
                Attendance.observation != ""
            ).order_by(Attendance.date.desc()).all()

            if historial_permisos:
                datos_hist_perm = []
                for hp in historial_permisos:
                    datos_hist_perm.append({
                        "Fecha": hp.date.strftime('%d/%m/%Y'),
                        "Estatus": hp.status,
                        "Detalle / Observación": hp.observation,
                        "Evidencia": "📎 Sí" if hp.evidencia_path else "—"
                    })

                df_hp = pd.DataFrame(datos_hist_perm)
                st.dataframe(df_hp, width='stretch', height=200)

                registros_con_evidencia = [hp for hp in historial_permisos if hp.evidencia_path]
                if registros_con_evidencia:
                    st.markdown("###### 📎 Evidencias adjuntas")
                    for hp in registros_con_evidencia:
                        if os.path.exists(hp.evidencia_path):
                            with open(hp.evidencia_path, "rb") as f_ev:
                                st.download_button(
                                    label=f"Descargar evidencia del {hp.date.strftime('%d/%m/%Y')}",
                                    data=f_ev.read(),
                                    file_name=os.path.basename(hp.evidencia_path),
                                    key=f"btn_desc_evid_{hp.id}"
                                )
            else:
                st.info("ℹ️ No hay permisos ni observaciones registradas para este estudiante actualmente.")
# -------------------------------------------------------------
# TAB: CARGA Y GESTIÓN DE DOCENTES (SOLO ADMIN)
# -------------------------------------------------------------
if "👨‍🏫 Carga y Gestión de Docentes" in available_tabs:
    idx_doc = available_tabs.index("👨‍🏫 Carga y Gestión de Docentes")
    with tabs[idx_doc]:
        st.subheader("👨‍🏫 Carga y Gestión de Cuentas de Docentes")

        col_doc1, col_doc2 = st.columns([1, 1])

        with col_doc1:
            with st.container(border=True):
                st.markdown("#### 👤 Registrar Nuevo Docente / Orientador")
                with st.form("form_crear_docente_nuevo_tab"):
                    docente_seleccionado_reg = st.selectbox("Nombre del Docente", lista_docentes_horarios,
                                                            key="sb_docente_nombre_select_tab")
                    nuevo_user_doc = st.text_input("NIE / Usuario del Docente", key="txt_nie_doc_tab")
                    nuevo_pass_doc = st.text_input("Contraseña", value="indet2026", type="password",
                                                   key="txt_pass_doc_tab")

                    btn_crear_doc = st.form_submit_button("➕ Crear / Actualizar Cuenta de Docente",
                                                          width='stretch')

                    if btn_crear_doc:
                        if nuevo_user_doc.strip() and docente_seleccionado_reg != "Sin Docentes Creados en Horarios":
                            nie_doc_str = nuevo_user_doc.strip()

                            seccion_encontrada = "Ninguna"
                            try:
                                conn_h = sqlite3.connect("modulo_horarios/database.db")
                                cursor_h = conn_h.cursor()
                                cursor_h.execute("""
                                                 SELECT s.nombre
                                                 FROM seccion s
                                                          JOIN horario h ON s.id = h.seccion_id
                                                          JOIN docente d ON h.docente_id = d.id
                                                 WHERE d.nombre = ?
                                                 """, (docente_seleccionado_reg,))
                                res_sec = cursor_h.fetchone()
                                if res_sec and res_sec[0]:
                                    seccion_encontrada = res_sec[0]
                                conn_h.close()
                            except Exception as ex:
                                pass

                            sec_val = None if seccion_encontrada == "Ninguna" else seccion_encontrada

                            doc_existente = session.query(User).filter_by(username=nie_doc_str).first()
                            if not doc_existente:
                                nuevo_docente = User(
                                    username=nie_doc_str,
                                    password=hash_password(nuevo_pass_doc.strip() if nuevo_pass_doc.strip() else "indet2026"),
                                    role="Docente",
                                    assigned_section=sec_val
                                )
                                session.add(nuevo_docente)
                                session.commit()
                                st.success(
                                    f"¡Docente '{docente_seleccionado_reg}' (Usuario: {nie_doc_str}) creado con éxito! Sección asignada: {seccion_encontrada}")
                                st.rerun()
                            else:
                                doc_existente.password = hash_password(nuevo_pass_doc.strip()) if nuevo_pass_doc.strip() else doc_existente.password
                                doc_existente.assigned_section = sec_val
                                session.commit()
                                st.success(
                                    f"¡Docente '{nie_doc_str}' actualizado correctamente con la sección {seccion_encontrada}!")
                                st.rerun()
                        else:
                            st.warning("Por favor ingresa el NIE/Usuario y selecciona un docente válido.")

        with col_doc2:
            with st.container(border=True):
                st.markdown("#### 📋 Listado de Docentes Registrados")
                docentes_users = session.query(User).filter_by(role="Docente").all()
                if docentes_users:
                    datos_docentes = []
                    for d in docentes_users:
                        datos_docentes.append({
                            "Usuario / NIE": d.username,
                            "Contraseña": d.password,
                            "Sección Asignada": d.assigned_section if d.assigned_section else "Ninguna"
                        })
                    df_docentes_display = pd.DataFrame(datos_docentes)
                    st.dataframe(df_docentes_display, width='stretch', height=250)
                else:
                    st.info("No hay cuentas de docentes creadas todavía.")

                st.divider()

                # --- ELIMINAR DOCENTE ---
                st.markdown("#### 🗑️ Eliminar Cuenta de Docente")
                if docentes_users:
                    mapa_docentes_del = {f"{d.username} (Sección: {d.assigned_section or 'Ninguna'})": d.username for d in
                                         docentes_users}
                    docente_a_borrar_label = st.selectbox("Seleccionar Docente a Eliminar", list(mapa_docentes_del.keys()),
                                                          key="sb_docente_eliminar")
                    username_a_borrar = mapa_docentes_del[docente_a_borrar_label]

                    if st.button("🗑️ Borrar Docente Seleccionado", type="secondary", width='stretch',
                                 key="btn_delete_single_docente"):
                        try:
                            doc_obj = session.query(User).filter_by(username=username_a_borrar, role="Docente").first()
                            if doc_obj:
                                session.delete(doc_obj)
                                session.commit()
                                st.success(f"¡Cuenta del docente '{username_a_borrar}' eliminada con éxito!")
                                st.rerun()
                            else:
                                st.error("No se encontró la cuenta del docente.")
                        except Exception as err:
                            session.rollback()
                            st.error(f"Error al eliminar el docente: {err}")
                else:
                    st.info("No hay docentes disponibles para eliminar.")

                st.divider()

                # --- MODIFICAR CONTRASEÑA DE DOCENTE ---
                st.markdown("#### 🔐 Modificar Contraseña de Docente")
                if docentes_users:
                    mapa_docentes_pass = {f"{d.username} (Sección: {d.assigned_section or 'Ninguna'})": d.username for d in
                                          docentes_users}
                    docente_pass_label = st.selectbox("Seleccionar Docente", list(mapa_docentes_pass.keys()),
                                                      key="sb_docente_pass_select")
                    username_pass = mapa_docentes_pass[docente_pass_label]

                    nueva_pass_docente = st.text_input("Nueva Contraseña para el Docente", type="password",
                                                       key="txt_nueva_pass_docente_mod")

                    if st.button("💾 Actualizar Contraseña Docente", width='stretch',
                                 key="btn_update_docente_pass"):
                        if nueva_pass_docente.strip():
                            doc_usr = session.query(User).filter_by(username=username_pass, role="Docente").first()
                            if doc_usr:
                                doc_usr.password = hash_password(nueva_pass_docente.strip())
                                session.commit()
                                st.success(f"¡Contraseña actualizada con éxito para el docente '{username_pass}'!")
                                st.rerun()
                            else:
                                st.warning("No se encontró el usuario docente.")
                        else:
                            st.warning("Por favor ingresa una contraseña válida.")
                else:
                    st.info("No hay docentes registrados para modificar contraseña.")

# -------------------------------------------------------------
# TAB: SEMÁFORO INSTITUCIONAL (SOLO ADMIN)
# -------------------------------------------------------------
if "🚦 Semáforo Institucional" in available_tabs:
    idx_sem = available_tabs.index("🚦 Semáforo Institucional")
    with tabs[idx_sem]:
        st.subheader("🚦 Semáforo Institucional de Riesgo")
        st.caption(
            "Vista de todos los estudiantes según su nivel de riesgo, calculado con los últimos 30 "
            "días de asistencia (mismo motor que usa el orientador y las actas).")

        try:
            skel_sem = st.empty()
            mostrar_skeleton(skel_sem, n_lineas=6, titulo="Calculando el semáforo institucional...")
            resumen_semaforo = get_institutional_semaphore(session)
            skel_sem.empty()

            total_estudiantes_sem = sum(len(v) for v in resumen_semaforo.values())

            if total_estudiantes_sem == 0:
                st.info("No hay estudiantes registrados todavía.")
            else:
                col_sem1, col_sem2, col_sem3, col_sem4 = st.columns(4)
                col_sem1.metric("Total Estudiantes", total_estudiantes_sem)
                col_sem2.metric("🟢 Riesgo Bajo", len(resumen_semaforo['🟢']))
                col_sem3.metric("🟡 Riesgo Medio", len(resumen_semaforo['🟡']))
                col_sem4.metric("🔴 Riesgo Alto", len(resumen_semaforo['🔴']))

                st.divider()

                secciones_disp_sem = sorted({
                    e['student'].section for lst in resumen_semaforo.values() for e in lst
                })
                col_f1, col_f2 = st.columns([1, 2])
                with col_f1:
                    filtro_seccion_sem = st.selectbox(
                        "Filtrar por sección", ["Todas"] + secciones_disp_sem,
                        key="sb_filtro_seccion_semaforo")
                with col_f2:
                    filtro_nivel_sem = st.multiselect(
                        "Filtrar por nivel de riesgo", ["🔴 Alto", "🟡 Medio", "🟢 Bajo"],
                        default=["🔴 Alto", "🟡 Medio"], key="ms_filtro_nivel_semaforo")

                mapa_nivel_sem = {"🔴 Alto": "🔴", "🟡 Medio": "🟡", "🟢 Bajo": "🟢"}
                niveles_a_mostrar = [mapa_nivel_sem[n] for n in filtro_nivel_sem]

                hay_resultados_sem = False
                for nivel in ["🔴", "🟡", "🟢"]:
                    if nivel not in niveles_a_mostrar:
                        continue

                    entradas_nivel = resumen_semaforo[nivel]
                    if filtro_seccion_sem != "Todas":
                        entradas_nivel = [e for e in entradas_nivel if e['student'].section == filtro_seccion_sem]
                    if not entradas_nivel:
                        continue

                    hay_resultados_sem = True
                    cfg_nivel = NEXUS_RIESGO_BADGE[nivel]
                    st.markdown(f"#### {cfg_nivel['icon']} {cfg_nivel['label']} ({len(entradas_nivel)})")

                    entradas_ordenadas = sorted(entradas_nivel, key=lambda e: e['pct'])
                    cols_grid_sem = st.columns(3)
                    for i, entrada in enumerate(entradas_ordenadas):
                        est_sem = entrada['student']
                        with cols_grid_sem[i % 3]:
                            clase_pulso_sem = "nexus-badge-pulse" if nivel == "🔴" else ""
                            patrones_html_sem = "".join(
                                f"<li style='font-size:0.78rem; margin-bottom:2px;'>{p}</li>"
                                for p in entrada['patterns'][:3]
                            )
                            lista_html_sem = (
                                f"<ul style='margin:0.4rem 0 0 1.1rem; padding:0;'>{patrones_html_sem}</ul>"
                                if patrones_html_sem else ""
                            )
                            st.markdown(f"""
                            <div class="{clase_pulso_sem}" style="background:{cfg_nivel['bg']};
                                        border:1.5px solid {cfg_nivel['border']}; border-radius:12px;
                                        padding:0.9rem 1rem; margin-bottom:0.9rem;">
                                <div style="font-weight:700; color:{cfg_nivel['fg']};">{est_sem.name}</div>
                                <div style="font-size:0.8rem; opacity:0.8; color:{cfg_nivel['fg']};">
                                    NIE: {est_sem.id} · Sección: {est_sem.section}
                                </div>
                                <div style="font-size:0.82rem; margin-top:0.35rem; color:{cfg_nivel['fg']};">
                                    Asistencia: <b>{entrada['pct']}%</b>
                                </div>
                                {lista_html_sem}
                            </div>
                            """, unsafe_allow_html=True)

                if not hay_resultados_sem:
                    st.info("No hay estudiantes que coincidan con los filtros seleccionados.")

        except Exception:
            st.error(
                "❌ No se pudo calcular el semáforo institucional por un problema de conexión. "
                "Intenta de nuevo en unos segundos.")