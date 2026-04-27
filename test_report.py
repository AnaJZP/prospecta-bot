"""
Script de prueba para generar un reporte PDF de ejemplo.
Ejecuta el pipeline completo de agentes y compila el PDF.
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

# Agregar el directorio actual al path
sys.path.insert(0, str(Path(__file__).parent))

import asyncio
from main import (
    run_agents, generate_report_text, _extract_financial_json,
    compile_pdf, logger
)

QUERY = "Cafe de especialidad del Valle del Cauca"

async def main():
    logger.info("=== Generando reporte de ejemplo ===")
    logger.info("Consulta: %s", QUERY)

    # Paso 1: Ejecutar los tres agentes (Gemini Flash)
    logger.info("Paso 1/4: Ejecutando pipeline de agentes (Flash)...")
    analysis = await run_agents(QUERY)
    logger.info("Agente Local: %d caracteres", len(analysis["local"]))
    logger.info("Agente Global: %d caracteres", len(analysis["global"]))
    logger.info("Agente Financiero: %d caracteres", len(analysis["financial"]))

    # Paso 2: Sintetizar reporte (Gemini Pro)
    logger.info("Paso 2/4: Sintetizando informe final (Pro)...")
    sections = await generate_report_text(QUERY, analysis)
    for k, v in sections.items():
        logger.info("Seccion %s: %d caracteres", k, len(v))

    # Paso 3: Extraer datos financieros
    logger.info("Paso 3/4: Extrayendo datos financieros...")
    financial_json = _extract_financial_json(analysis["financial"])
    if financial_json:
        logger.info("JSON financiero: %s", list(financial_json.keys()))
    else:
        logger.warning("No se extrajo JSON financiero, se usaran valores N/D")

    # Paso 4: Compilar PDF
    logger.info("Paso 4/4: Compilando PDF con LaTeX...")
    pdf_path = compile_pdf(QUERY, sections, financial_json)

    if pdf_path and pdf_path.exists():
        # Copiar al directorio del proyecto
        dest = Path(__file__).parent / "reporte_ejemplo.pdf"
        import shutil
        shutil.copy2(pdf_path, dest)
        shutil.rmtree(pdf_path.parent, ignore_errors=True)
        logger.info("PDF generado exitosamente: %s", dest)
        logger.info("Tamano: %.1f KB", dest.stat().st_size / 1024)
    else:
        logger.error("No se pudo generar el PDF")

if __name__ == "__main__":
    asyncio.run(main())
