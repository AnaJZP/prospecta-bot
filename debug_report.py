"""Debug: ver la respuesta raw de Gemini Pro y diagnosticar el parsing."""
import os, asyncio, re, json
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

from main import _call_gemini, run_agents, FLASH_MODEL, PRO_MODEL, REPORT_SYSTEM
from main import _extract_financial_json, compile_pdf, sanitize_latex, TEMPLATE_PATH
from datetime import datetime

QUERY = "Cafe de especialidad del Valle del Cauca"

async def main():
    print("=== Ejecutando agentes ===")
    analysis = await run_agents(QUERY)

    print(f"\n=== Respuesta Agente Financiero (primeros 500 chars) ===")
    print(analysis["financial"][:500])

    # Probar extraccion JSON
    fj = _extract_financial_json(analysis["financial"])
    print(f"\n=== JSON extraido ===")
    print(json.dumps(fj, indent=2, ensure_ascii=False) if fj else "No se extrajo JSON")

    # Generar reporte con Pro
    prompt = (
        f"Producto/Consulta: {QUERY}\n\n"
        f"--- Analisis del Agente de Identidad Local ---\n{analysis['local']}\n\n"
        f"--- Analisis del Agente de Mercado Global ---\n{analysis['global']}\n\n"
        f"--- Analisis del Agente Financiero ---\n{analysis['financial']}\n\n"
        "Redacta el informe final consolidado."
    )
    text = await _call_gemini(PRO_MODEL, REPORT_SYSTEM, prompt)

    print(f"\n=== Respuesta Gemini Pro (primeros 1000 chars) ===")
    print(text[:1000])

    # Guardar respuesta completa para debug
    Path("debug_pro_response.txt").write_text(text, encoding="utf-8")
    print(f"\nRespuesta completa guardada en debug_pro_response.txt ({len(text)} chars)")

    # Intentar compilar con contenido directo (sin parseo por secciones)
    # Usar la respuesta completa como contenido de cada seccion
    sections = {
        "RESUMEN_EJECUTIVO": "Analisis generado por el sistema Cali-Glocal Scout.",
        "ANALISIS_LOCAL": analysis["local"][:2000],
        "ANALISIS_GLOBAL": analysis["global"][:2000],
        "ANALISIS_FINANCIERO": analysis["financial"][:1500],
        "CONCLUSIONES": "Informe generado automaticamente. Consulte el analisis completo.",
    }

    print("\n=== Compilando PDF con contenido directo ===")
    pdf_path = compile_pdf(QUERY, sections, fj)
    if pdf_path and pdf_path.exists():
        import shutil
        dest = Path("reporte_ejemplo.pdf")
        shutil.copy2(pdf_path, dest)
        shutil.rmtree(pdf_path.parent, ignore_errors=True)
        print(f"PDF generado: {dest} ({dest.stat().st_size / 1024:.1f} KB)")
    else:
        print("PDF no generado. Revisando error de LaTeX...")
        # Intentar compilar sin pdflatex para ver el .tex
        import tempfile, subprocess
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        today = datetime.now().strftime("%d de %B de %Y")
        table_rows = ""
        if fj:
            for a in fj.get("activos", []):
                name = sanitize_latex(str(a.get("nombre", "")))
                alloc = a.get("asignacion_pct", 0)
                ret = a.get("rendimiento_esperado_pct", 0)
                risk = a.get("riesgo_pct", 0)
                table_rows += f"{name} & {alloc} & {ret} & {risk} \\\\\n"
        if not table_rows:
            table_rows = "Sin datos & -- & -- & -- \\\\\n"

        replacements = {
            "<<DATE>>": today,
            "<<PDF_TITLE>>": f"Informe Cali-Glocal Scout",
            "<<REPORT_TITLE>>": sanitize_latex(QUERY[:80]),
            "<<PRODUCT>>": sanitize_latex(QUERY[:60]),
            "<<EXECUTIVE_SUMMARY>>": sanitize_latex(sections["RESUMEN_EJECUTIVO"]),
            "<<LOCAL_ANALYSIS>>": sanitize_latex(sections["ANALISIS_LOCAL"]),
            "<<GLOBAL_ANALYSIS>>": sanitize_latex(sections["ANALISIS_GLOBAL"]),
            "<<FINANCIAL_NARRATIVE>>": sanitize_latex(sections["ANALISIS_FINANCIERO"]),
            "<<PORTFOLIO_TABLE_ROWS>>": table_rows,
            "<<PORTFOLIO_RETURN>>": str(fj.get("rendimiento_portafolio_pct", "N/D")) if fj else "N/D",
            "<<PORTFOLIO_RISK>>": str(fj.get("riesgo_portafolio_pct", "N/D")) if fj else "N/D",
            "<<SHARPE_RATIO>>": str(fj.get("ratio_sharpe", "N/D")) if fj else "N/D",
            "<<HORIZON>>": str(fj.get("horizonte_anos", "N/D")) if fj else "N/D",
            "<<CONCLUSIONS>>": sanitize_latex(sections["CONCLUSIONES"]),
        }
        for ph, val in replacements.items():
            template = template.replace(ph, val)

        tmpdir = Path(tempfile.mkdtemp(prefix="cgs_debug_"))
        tex_path = tmpdir / "report.tex"
        tex_path.write_text(template, encoding="utf-8")
        print(f"Archivo .tex guardado en: {tex_path}")

        result = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-output-directory",
             str(tmpdir), str(tex_path)],
            capture_output=True, text=True, timeout=120,
        )
        # Mostrar ultimas lineas del log
        log_path = tmpdir / "report.log"
        if log_path.exists():
            log_text = log_path.read_text(encoding="utf-8", errors="ignore")
            # Buscar lineas con error
            errors = [l for l in log_text.split("\n") if "!" in l or "Error" in l]
            print(f"\nErrores en LaTeX ({len(errors)} encontrados):")
            for e in errors[:10]:
                print(f"  {e}")

        pdf_path2 = tmpdir / "report.pdf"
        if pdf_path2.exists():
            import shutil
            dest = Path("reporte_ejemplo.pdf")
            shutil.copy2(pdf_path2, dest)
            print(f"\nPDF generado en segundo intento: {dest} ({dest.stat().st_size / 1024:.1f} KB)")

asyncio.run(main())
