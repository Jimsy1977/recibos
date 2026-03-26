import os
import time
import base64
import threading
import io
import re
import urllib3
import requests as req_lib
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from flask import Flask, render_template, request, jsonify, send_file, abort
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

sessions = {}
sessions_lock = threading.Lock()

BASE_URL  = "https://www.chavimochic.gob.pe"
LOGIN_URL = f"{BASE_URL}/iscomweb/iscon/maincon.aspx"


# ── helpers ───────────────────────────────────────────────────────────────────

def log(suministro, msg):
    with sessions_lock:
        sessions[suministro].setdefault("log", []).append(msg)
    print(f"[{suministro}] {msg}")


def get_chrome_options():
    o = Options()
    o.add_argument("--headless=new")
    o.add_argument("--no-sandbox")
    o.add_argument("--disable-dev-shm-usage")
    o.add_argument("--disable-gpu")
    o.add_argument("--window-size=1920,1080")
    o.add_argument("--disable-blink-features=AutomationControlled")
    o.add_argument("--disable-extensions")
    o.add_argument("--disable-popup-blocking")
    o.add_argument("--disable-infobars")
    o.add_argument("--lang=es-PE")
    o.add_experimental_option("excludeSwitches", ["enable-automation"])
    o.add_experimental_option("useAutomationExtension", False)
    return o


# ── scraping principal ────────────────────────────────────────────────────────

def scrape_recibos(suministro):
    with sessions_lock:
        sessions[suministro] = {
            "status": "loading",
            "archivos": [],
            "error": None,
            "log": [],
            "screenshots": [],   # lista de {"step": "...", "b64": "..."}
            "html_debug": {},    # {"frmMain": "<html>..."}
        }

    driver = None
    try:
        log(suministro, "Iniciando Chrome headless...")
        options = get_chrome_options()
        driver = webdriver.Chrome(options=options)

        # Inyectar JS para ocultar webdriver fingerprint
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        })

        wait = WebDriverWait(driver, 25)

        # ── PASO 1: Login ─────────────────────────────────────────────────────
        log(suministro, f"Abriendo {LOGIN_URL}")
        driver.get(LOGIN_URL)
        wait.until(EC.presence_of_element_located((By.ID, "TxtContrato")))
        _screenshot(driver, suministro, "1_login_page")

        log(suministro, "Ingresando credenciales...")
        driver.find_element(By.ID, "TxtContrato").send_keys(suministro)
        driver.find_element(By.ID, "TxtPassword").send_keys(suministro)
        driver.find_element(By.ID, "BotonOK").click()

        log(suministro, "Esperando respuesta post-login (8s)...")
        time.sleep(8)
        _screenshot(driver, suministro, "2_post_login")

        # Detectar si seguimos en la página de login (falló)
        if driver.find_elements(By.ID, "TxtContrato"):
            log(suministro, "ERROR: Login fallido - seguimos en página de login")
            # Capturar el texto de error si existe
            try:
                err = driver.find_element(By.CLASS_NAME, "error").text
                log(suministro, f"Mensaje error portal: {err}")
            except Exception:
                pass
            with sessions_lock:
                sessions[suministro]["status"] = "empty"
            return

        log(suministro, f"Login exitoso. URL actual: {driver.current_url}")
        log(suministro, f"Título página: {driver.title}")

        # ── PASO 2: Entrar al frame menú ──────────────────────────────────────
        # Primero listar todos los frames disponibles
        frames_disponibles = driver.find_elements(By.TAG_NAME, "frame") + \
                             driver.find_elements(By.TAG_NAME, "iframe")
        frame_names = [f.get_attribute("name") or f.get_attribute("id") or "sin-nombre"
                       for f in frames_disponibles]
        log(suministro, f"Frames detectados: {frame_names}")

        try:
            wait.until(EC.frame_to_be_available_and_switch_to_it("frmMenu"))
            log(suministro, "Dentro de frmMenu")
        except Exception as e:
            log(suministro, f"ERROR al entrar frmMenu: {e}")
            with sessions_lock:
                sessions[suministro]["status"] = "empty"
                sessions[suministro]["error"] = f"No se pudo entrar al frmMenu: {e}"
            return

        _screenshot(driver, suministro, "3_frmMenu")
        menu_html = driver.page_source
        with sessions_lock:
            sessions[suministro]["html_debug"]["frmMenu"] = menu_html[:5000]

        # Listar todos los links del menú para debug
        links_menu = [(a.text.strip(), a.get_attribute("href")) for a in driver.find_elements(By.TAG_NAME, "a")]
        log(suministro, f"Links en frmMenu: {links_menu}")

        # ── PASO 3: Click en Estado de Cuenta ─────────────────────────────────
        clicked = False
        for a in driver.find_elements(By.TAG_NAME, "a"):
            texto = (a.text or "").strip().lower()
            if "estado" in texto:
                log(suministro, f"Haciendo click en: '{a.text.strip()}'")
                driver.execute_script("arguments[0].click();", a)
                clicked = True
                break

        if not clicked:
            log(suministro, "ERROR: No se encontró link 'Estado de Cuenta' en el menú")
            with sessions_lock:
                sessions[suministro]["status"] = "empty"
                sessions[suministro]["error"] = "No se encontró 'Estado de Cuenta' en el menú"
            return

        driver.switch_to.default_content()
        log(suministro, "Esperando que cargue frmMain (7s)...")
        time.sleep(7)

        # ── PASO 4: Entrar al frame principal ──────────────────────────────────
        try:
            wait.until(EC.frame_to_be_available_and_switch_to_it("frmMain"))
            log(suministro, "Dentro de frmMain")
        except Exception as e:
            log(suministro, f"ERROR al entrar frmMain: {e}")
            with sessions_lock:
                sessions[suministro]["status"] = "empty"
                sessions[suministro]["error"] = f"No se pudo entrar al frmMain: {e}"
            return

        # Espera adicional para carga de contenido dinámico
        log(suministro, "Esperando contenido dinámico en frmMain (10s)...")
        time.sleep(10)

        _screenshot(driver, suministro, "4_frmMain")
        main_html = driver.page_source
        with sessions_lock:
            sessions[suministro]["html_debug"]["frmMain"] = main_html[:8000]
        log(suministro, f"HTML frmMain (primeros 500 chars): {main_html[:500]}")

        # ── PASO 5: Buscar todos los links "Ver Recibo" ───────────────────────
        todos_links = driver.find_elements(By.TAG_NAME, "a")
        log(suministro, f"Total links en frmMain: {len(todos_links)}")

        recibo_links = []
        for a in todos_links:
            texto = (a.text or "").strip()
            href  = a.get_attribute("href") or ""
            if "recibo" in texto.lower() or "recibo" in href.lower():
                recibo_links.append({
                    "texto": texto,
                    "href": href,
                    "onclick": a.get_attribute("onclick") or "",
                })
                log(suministro, f"  → Link recibo: texto='{texto}' href='{href}'")

        if not recibo_links:
            log(suministro, "No se encontraron links de recibos. Revisando todo el HTML...")
            soup = BeautifulSoup(main_html, "html.parser")
            all_a = soup.find_all("a")
            log(suministro, f"BeautifulSoup encontró {len(all_a)} links: {[(a.get_text(strip=True), a.get('href','')) for a in all_a]}")
            with sessions_lock:
                sessions[suministro]["status"] = "empty"
            return

        log(suministro, f"Total links de recibos encontrados: {len(recibo_links)}")

        # ── PASO 6: Obtener cookies para requests ─────────────────────────────
        frame_url = driver.current_url
        selenium_cookies = driver.get_cookies()
        user_agent = driver.execute_script("return navigator.userAgent")

        s = req_lib.Session()
        s.headers.update({"User-Agent": user_agent, "Referer": frame_url})
        for c in selenium_cookies:
            s.cookies.set(c["name"], c["value"])

        # ViewState del frame actual
        soup_main = BeautifulSoup(main_html, "html.parser")
        def gv(name):
            el = soup_main.find("input", {"name": name})
            return el["value"] if el and el.get("value") else ""

        viewstate      = gv("__VIEWSTATE")
        vsgenerator    = gv("__VIEWSTATEGENERATOR")
        evvalidation   = gv("__EVENTVALIDATION")

        # ── PASO 7: Descargar cada PDF ─────────────────────────────────────────
        archivos_data = []
        for i, info in enumerate(recibo_links):
            href    = info["href"]
            texto   = info["texto"]
            onclick = info["onclick"]
            pdf_bytes = None

            log(suministro, f"Procesando recibo {i+1}/{len(recibo_links)}: '{texto}'")

            # Estrategia A: URL directa (no javascript)
            if href and not href.startswith("javascript") and href.startswith("http"):
                try:
                    log(suministro, f"  Estrategia A: GET {href}")
                    r = s.get(href, verify=False, timeout=30)
                    content = r.content
                    ct = r.headers.get("content-type", "").lower()
                    log(suministro, f"  Respuesta: status={r.status_code} content-type={ct} size={len(content)}")
                    if content[:4] == b"%PDF" or "pdf" in ct:
                        pdf_bytes = content
                        log(suministro, "  ✓ PDF obtenido por URL directa")
                except Exception as ex:
                    log(suministro, f"  ✗ Estrategia A falló: {ex}")

            # Estrategia B: postback ASP.NET
            if not pdf_bytes:
                match = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", onclick + href)
                if match:
                    try:
                        et = match.group(1)
                        ea = match.group(2)
                        log(suministro, f"  Estrategia B: postback eventTarget='{et}'")
                        post_data = {
                            "__EVENTTARGET": et,
                            "__EVENTARGUMENT": ea,
                            "__VIEWSTATE": viewstate,
                            "__VIEWSTATEGENERATOR": vsgenerator,
                            "__EVENTVALIDATION": evvalidation,
                        }
                        r = s.post(frame_url, data=post_data, verify=False, timeout=30)
                        content = r.content
                        ct = r.headers.get("content-type", "").lower()
                        log(suministro, f"  Respuesta postback: status={r.status_code} content-type={ct} size={len(content)}")
                        if content[:4] == b"%PDF" or "pdf" in ct:
                            pdf_bytes = content
                            log(suministro, "  ✓ PDF obtenido por postback")
                    except Exception as ex:
                        log(suministro, f"  ✗ Estrategia B falló: {ex}")

            # Estrategia C: Selenium clic + captura de nueva pestaña/URL
            if not pdf_bytes:
                try:
                    log(suministro, "  Estrategia C: click selenium + captura URL")
                    # Re-obtener los links frescos
                    fresh_links = [a for a in driver.find_elements(By.TAG_NAME, "a")
                                   if "recibo" in (a.text or "").lower()]
                    if i < len(fresh_links):
                        ventanas_antes = set(driver.window_handles)
                        # Usar JS click para evitar interception
                        driver.execute_script("arguments[0].click();", fresh_links[i])
                        time.sleep(5)

                        ventanas_nuevas = set(driver.window_handles) - ventanas_antes
                        log(suministro, f"  Ventanas nuevas abiertas: {len(ventanas_nuevas)}")

                        for v in ventanas_nuevas:
                            driver.switch_to.window(v)
                            nueva_url = driver.current_url
                            log(suministro, f"  URL en nueva pestaña: {nueva_url}")
                            _screenshot(driver, suministro, f"5_nueva_tab_{i}")
                            try:
                                r = s.get(nueva_url, verify=False, timeout=30)
                                content = r.content
                                ct = r.headers.get("content-type","").lower()
                                log(suministro, f"  Requests en URL pestaña: status={r.status_code} ct={ct} size={len(content)}")
                                if content[:4] == b"%PDF" or "pdf" in ct:
                                    pdf_bytes = content
                                    log(suministro, "  ✓ PDF obtenido de URL de nueva pestaña")
                            except Exception as ex:
                                log(suministro, f"  ✗ Requests en nueva URL: {ex}")
                            driver.close()

                        # Volver a ventana/frame principal
                        driver.switch_to.window(driver.window_handles[0])
                        try:
                            driver.switch_to.frame("frmMain")
                        except Exception:
                            pass
                except Exception as ex:
                    log(suministro, f"  ✗ Estrategia C falló: {ex}")
                    try:
                        driver.switch_to.window(driver.window_handles[0])
                    except Exception:
                        pass

            if pdf_bytes and len(pdf_bytes) > 500:
                b64 = base64.b64encode(pdf_bytes).decode("utf-8")
                nombre = re.sub(r"[^a-zA-Z0-9_\-]", "_", texto) or f"Recibo_{i+1:02d}"
                archivos_data.append({
                    "id": len(archivos_data),
                    "nombre": f"{nombre}.pdf",
                    "base64": b64,
                })
                log(suministro, f"  ✓ Recibo {i+1} guardado: {nombre}.pdf ({len(pdf_bytes)} bytes)")
            else:
                log(suministro, f"  ✗ No se pudo obtener PDF para recibo {i+1}")

        # ── Resultado ─────────────────────────────────────────────────────────
        if archivos_data:
            log(suministro, f"ÉXITO: {len(archivos_data)} recibo(s) obtenidos")
            with sessions_lock:
                sessions[suministro]["archivos"] = archivos_data
                sessions[suministro]["status"] = "done"
        else:
            log(suministro, "Sin archivos PDF al final")
            with sessions_lock:
                sessions[suministro]["status"] = "empty"

    except Exception as e:
        log(suministro, f"EXCEPCIÓN GENERAL: {e}")
        import traceback
        log(suministro, traceback.format_exc())
        with sessions_lock:
            sessions[suministro]["status"] = "error"
            sessions[suministro]["error"] = str(e)
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def _screenshot(driver, suministro, step):
    """Captura pantalla y la guarda en la sesión como base64."""
    try:
        b64 = base64.b64encode(driver.get_screenshot_as_png()).decode("utf-8")
        with sessions_lock:
            sessions[suministro].setdefault("screenshots", []).append({"step": step, "b64": b64})
    except Exception:
        pass


# ── Rutas Flask ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/consultar", methods=["POST"])
def consultar():
    suministro = request.form.get("suministro", "").strip()
    if not suministro:
        return render_template("error.html", mensaje="Debes ingresar un número de suministro.", suministro="")
    hilo = threading.Thread(target=scrape_recibos, args=(suministro,), daemon=True)
    hilo.start()
    return render_template("loading.html", suministro=suministro)


@app.route("/estado/<suministro>")
def estado(suministro):
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
    return jsonify({"status": "loading"})


@app.route("/debug/<suministro>")
def debug(suministro):
    """Endpoint de diagnóstico: muestra log, screenshots y HTML capturado."""
    with sessions_lock:
        sesion = sessions.get(suministro, {})

    log_lines  = sesion.get("log", [])
    shots      = sesion.get("screenshots", [])
    html_debug = sesion.get("html_debug", {})

    shots_html = ""
    for s in shots:
        shots_html += (f'<h3>{s["step"]}</h3>'
                       f'<img src="data:image/png;base64,{s["b64"]}" style="max-width:100%;border:1px solid #ccc;margin-bottom:16px"/>')

    html_sections = ""
    for key, html in html_debug.items():
        html_sections += f"<h3>HTML: {key}</h3><pre style='background:#f4f4f4;padding:12px;overflow:auto;max-height:400px'>{html[:5000]}</pre>"

    return f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
    <title>Debug {suministro}</title>
    <style>body{{font-family:monospace;padding:20px}}pre{{background:#111;color:#0f0;padding:12px}}</style>
    </head><body>
    <h1>Debug Suministro: {suministro}</h1>
    <h2>Estado: {sesion.get("status","N/A")}</h2>
    <h2>Log</h2><pre>{"<br>".join(log_lines)}</pre>
    <h2>Capturas de Pantalla</h2>{shots_html}
    {html_sections}
    </body></html>"""


@app.route("/error")
def error_page():
    mensaje = request.args.get("msg", "Error al procesar la solicitud.")
    suministro = request.args.get("suministro", "")
    return render_template("error.html", mensaje=mensaje, suministro=suministro)


@app.route("/recibos/<suministro>")
def recibos(suministro):
    with sessions_lock:
        sesion = sessions.get(suministro)
    if not sesion or sesion["status"] != "done":
        return render_template("error.html",
                               mensaje="Sesión no encontrada o expirada. Realiza una nueva consulta.",
                               suministro=suministro)
    archivos = [{"id": a["id"], "nombre": a["nombre"]} for a in sesion["archivos"]]
    return render_template("recibos.html", suministro=suministro, archivos=archivos)


@app.route("/ver/<suministro>/<int:idx>")
def ver_recibo(suministro, idx):
    with sessions_lock:
        sesion = sessions.get(suministro)
    if not sesion or sesion["status"] != "done":
        abort(404)
    archivos = sesion["archivos"]
    if idx >= len(archivos):
        abort(404)
    archivo = archivos[idx]
    total = len(archivos)
    return render_template("visor.html", suministro=suministro, archivo=archivo,
                           idx=idx, total=total,
                           prev_idx=idx - 1 if idx > 0 else None,
                           next_idx=idx + 1 if idx < total - 1 else None)


@app.route("/descargar/<suministro>/<int:idx>")
def descargar(suministro, idx):
    with sessions_lock:
        sesion = sessions.get(suministro)
    if not sesion or sesion["status"] != "done":
        abort(404)
    archivos = sesion["archivos"]
    if idx >= len(archivos):
        abort(404)
    archivo = archivos[idx]
    pdf_bytes = base64.b64decode(archivo["base64"])
    return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf",
                     as_attachment=True, download_name=archivo["nombre"])


@app.route("/pdf_data/<suministro>/<int:idx>")
def pdf_data(suministro, idx):
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
