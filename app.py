import os
import glob
import time
import base64
import threading
from flask import Flask, render_template, request, jsonify, send_file, abort
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
import tempfile
import io

app = Flask(__name__)

# Almacén temporal en memoria: { suministro: { "status": "loading|done|error", "archivos": [...], "error": "..." } }
sessions = {}
sessions_lock = threading.Lock()


def get_chrome_options(download_dir):
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    prefs = {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,
    }
    options.add_experimental_option("prefs", prefs)
    return options


def scrape_recibos(suministro):
    """Función que corre en hilo separado para hacer el scraping."""
    download_dir = tempfile.mkdtemp(prefix=f"chavimochic_{suministro}_")

    with sessions_lock:
        sessions[suministro] = {"status": "loading", "archivos": [], "error": None, "download_dir": download_dir}

    driver = None
    try:
        options = get_chrome_options(download_dir)
        driver = webdriver.Chrome(options=options)
        wait = WebDriverWait(driver, 15)

        # Login
        driver.get("https://www.chavimochic.gob.pe/iscomweb/iscon/maincon.aspx")
        time.sleep(3)

        contrato_input = wait.until(EC.presence_of_element_located((By.ID, "TxtContrato")))
        clave_input = driver.find_element(By.ID, "TxtPassword")
        contrato_input.send_keys(suministro)
        clave_input.send_keys(suministro)
        driver.find_element(By.ID, "BotonOK").click()
        time.sleep(5)

        # Navegar a recibos
        driver.switch_to.frame("frmMenu")
        recibos_link = wait.until(EC.element_to_be_clickable((By.PARTIAL_LINK_TEXT, "Estado de Cuenta")))
        recibos_link.click()
        driver.switch_to.default_content()
        time.sleep(3)
        driver.switch_to.frame("frmMain")
        time.sleep(5)

        # Descargar recibos
        enlaces_recibos = driver.find_elements(By.PARTIAL_LINK_TEXT, "Ver Recibo")
        total = len(enlaces_recibos)

        if total == 0:
            with sessions_lock:
                sessions[suministro]["status"] = "empty"
            return

        for i in range(total):
            try:
                enlaces = driver.find_elements(By.PARTIAL_LINK_TEXT, "Ver Recibo")
                enlaces[i].click()
                time.sleep(5)
            except Exception:
                pass

        # Leer PDFs descargados
        time.sleep(2)
        archivos_pdf = glob.glob(os.path.join(download_dir, "*.pdf"))
        archivos_data = []
        for idx, path in enumerate(sorted(archivos_pdf)):
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            nombre = os.path.basename(path)
            archivos_data.append({
                "id": idx,
                "nombre": nombre,
                "base64": b64,
                "path": path,
            })

        with sessions_lock:
            sessions[suministro]["archivos"] = archivos_data
            sessions[suministro]["status"] = "done"

    except Exception as e:
        with sessions_lock:
            sessions[suministro]["status"] = "error"
            sessions[suministro]["error"] = str(e)
    finally:
        if driver:
            driver.quit()


# ── Rutas ──────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/consultar", methods=["POST"])
def consultar():
    suministro = request.form.get("suministro", "").strip()
    if not suministro:
        return render_template("error.html", mensaje="Debes ingresar un número de suministro.", suministro="")

    # Iniciar scraping en hilo
    hilo = threading.Thread(target=scrape_recibos, args=(suministro,), daemon=True)
    hilo.start()

    return render_template("loading.html", suministro=suministro)


@app.route("/estado/<suministro>")
def estado(suministro):
    """Endpoint de polling que devuelve el estado del scraping en JSON."""
    with sessions_lock:
        sesion = sessions.get(suministro)

    if not sesion:
        return jsonify({"status": "not_found"})

    if sesion["status"] == "done":
        archivos = [{"id": a["id"], "nombre": a["nombre"]} for a in sesion["archivos"]]
        return jsonify({"status": "done", "archivos": archivos, "total": len(archivos)})
    elif sesion["status"] == "empty":
        return jsonify({"status": "empty"})
    elif sesion["status"] == "error":
        return jsonify({"status": "error", "error": sesion.get("error", "Error desconocido")})
    else:
        return jsonify({"status": "loading"})


@app.route("/error")
def error_page():
    mensaje = request.args.get("msg", "Error al procesar la solicitud.")
    suministro = request.args.get("suministro", "")
    return render_template("error.html", mensaje=mensaje, suministro=suministro)


@app.route("/recibos/<suministro>")
def recibos(suministro):
    """Página que muestra la lista de recibos."""
    with sessions_lock:
        sesion = sessions.get(suministro)

    if not sesion or sesion["status"] != "done":
        return render_template("error.html", mensaje="Sesión no encontrada o expirada. Realiza una nueva consulta.", suministro=suministro)

    archivos = [{"id": a["id"], "nombre": a["nombre"]} for a in sesion["archivos"]]
    return render_template("recibos.html", suministro=suministro, archivos=archivos)


@app.route("/ver/<suministro>/<int:idx>")
def ver_recibo(suministro, idx):
    """Página visor del recibo individual."""
    with sessions_lock:
        sesion = sessions.get(suministro)

    if not sesion or sesion["status"] != "done":
        abort(404)

    archivos = sesion["archivos"]
    if idx >= len(archivos):
        abort(404)

    archivo = archivos[idx]
    total = len(archivos)
    prev_idx = idx - 1 if idx > 0 else None
    next_idx = idx + 1 if idx < total - 1 else None

    return render_template(
        "visor.html",
        suministro=suministro,
        archivo=archivo,
        idx=idx,
        total=total,
        prev_idx=prev_idx,
        next_idx=next_idx,
    )


@app.route("/descargar/<suministro>/<int:idx>")
def descargar(suministro, idx):
    """Descarga directa del PDF."""
    with sessions_lock:
        sesion = sessions.get(suministro)

    if not sesion or sesion["status"] != "done":
        abort(404)

    archivos = sesion["archivos"]
    if idx >= len(archivos):
        abort(404)

    archivo = archivos[idx]
    pdf_bytes = base64.b64decode(archivo["base64"])
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=archivo["nombre"],
    )


@app.route("/pdf_data/<suministro>/<int:idx>")
def pdf_data(suministro, idx):
    """Devuelve el base64 del PDF para el visor inline."""
    with sessions_lock:
        sesion = sessions.get(suministro)

    if not sesion or sesion["status"] != "done":
        abort(404)

    archivos = sesion["archivos"]
    if idx >= len(archivos):
        abort(404)

    return jsonify({"base64": archivos[idx]["base64"], "nombre": archivos[idx]["nombre"]})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
