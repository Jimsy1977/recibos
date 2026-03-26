import os
import time
import base64
import threading
import io
import re
import urllib3
import requests as req_lib
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs

from flask import Flask, render_template, request, jsonify, send_file, abort
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# Almacén temporal en memoria
sessions = {}
sessions_lock = threading.Lock()

BASE_URL = "https://www.chavimochic.gob.pe"
LOGIN_URL = f"{BASE_URL}/iscomweb/iscon/maincon.aspx"


# ── Opción A: scraping puro con requests (sin Selenium) ───────────────────────

def scrape_con_requests(suministro):
    """
    Intenta hacer login y obtener los PDFs usando solo requests + BeautifulSoup.
    Funciona si el portal acepta formularios ASP.NET estándar.
    """
    s = req_lib.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Accept-Language": "es-PE,es;q=0.9",
    })

    # 1. Obtener página de login y tokens ASP.NET
    r = s.get(LOGIN_URL, verify=False, timeout=30)
    soup = BeautifulSoup(r.text, "html.parser")

    def get_input(name):
        el = soup.find("input", {"name": name})
        return el["value"] if el and el.get("value") else ""

    post_data = {
        "__VIEWSTATE": get_input("__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": get_input("__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": get_input("__EVENTVALIDATION"),
        "__EVENTTARGET": "",
        "__EVENTARGUMENT": "",
        "TxtContrato": suministro,
        "TxtPassword": suministro,
        "BotonOK": "Ingresar",
    }

    # 2. POST login
    r2 = s.post(LOGIN_URL, data=post_data, verify=False, timeout=30,
                headers={"Referer": LOGIN_URL,
                         "Content-Type": "application/x-www-form-urlencoded"})

    # Verificar que login fue exitoso (debe haber redirigido o cargado el portal)
    if "TxtContrato" in r2.text and "BotonOK" in r2.text:
        return None  # Seguimos en login → falló

    # 3. Buscar la URL del frame frmMenu en la respuesta
    soup2 = BeautifulSoup(r2.text, "html.parser")
    frame_menu = soup2.find("frame", {"name": "frmMenu"}) or soup2.find("iframe", {"name": "frmMenu"})
    frame_main = soup2.find("frame", {"name": "frmMain"}) or soup2.find("iframe", {"name": "frmMain"})

    if not frame_menu:
        return None

    menu_url = urljoin(BASE_URL, frame_menu.get("src", ""))

    # 4. Cargar el menú y buscar "Estado de Cuenta"
    r_menu = s.get(menu_url, verify=False, timeout=30, headers={"Referer": r2.url})
    soup_menu = BeautifulSoup(r_menu.text, "html.parser")

    estado_link = None
    for a in soup_menu.find_all("a"):
        if "estado" in (a.get_text() or "").lower():
            estado_link = a
            break

    if not estado_link:
        return None

    # La URL del frame main donde se cargan los recibos
    main_url = None
    if frame_main:
        main_url = urljoin(BASE_URL, frame_main.get("src", ""))

    # El click en "Estado de Cuenta" puede cargar una URL en frmMain vía target
    link_href = estado_link.get("href", "")
    if link_href and not link_href.startswith("javascript"):
        estado_url = urljoin(menu_url, link_href)
        r_estado = s.get(estado_url, verify=False, timeout=30, headers={"Referer": menu_url})
        soup_estado = BeautifulSoup(r_estado.text, "html.parser")
    elif main_url:
        r_estado = s.get(main_url, verify=False, timeout=30, headers={"Referer": menu_url})
        soup_estado = BeautifulSoup(r_estado.text, "html.parser")
    else:
        return None

    # 5. Buscar links "Ver Recibo"
    recibo_links = []
    for a in soup_estado.find_all("a"):
        texto = (a.get_text() or "").strip().lower()
        href = a.get("href", "") or ""
        if "ver recibo" in texto or "recibo" in href.lower():
            full_url = urljoin(r_estado.url, href)
            recibo_links.append(full_url)

    if not recibo_links:
        return None

    # 6. Descargar cada PDF
    archivos_data = []
    for i, url in enumerate(recibo_links):
        try:
            r_pdf = s.get(url, verify=False, timeout=30, headers={"Referer": r_estado.url})
            content = r_pdf.content
            ct = r_pdf.headers.get("content-type", "").lower()
            if content[:4] == b"%PDF" or "pdf" in ct:
                b64 = base64.b64encode(content).decode("utf-8")
                archivos_data.append({
                    "id": i,
                    "nombre": f"Recibo_{suministro}_{i+1:02d}.pdf",
                    "base64": b64,
                })
        except Exception:
            pass

    return archivos_data if archivos_data else None


# ── Opción B: Selenium + requests combinados ──────────────────────────────────

def get_chrome_options():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-popup-blocking")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    return options


def scrape_con_selenium(suministro):
    """
    Usa Selenium para navegar el portal, extrae URLS y cookies,
    luego usa requests para descargar cada PDF directamente.
    """
    driver = None
    try:
        options = get_chrome_options()
        driver = webdriver.Chrome(options=options)
        wait = WebDriverWait(driver, 25)

        # 1. Login
        driver.get(LOGIN_URL)
        wait.until(EC.presence_of_element_located((By.ID, "TxtContrato")))
        driver.find_element(By.ID, "TxtContrato").send_keys(suministro)
        driver.find_element(By.ID, "TxtPassword").send_keys(suministro)
        driver.find_element(By.ID, "BotonOK").click()
        time.sleep(7)

        # Verificar login: debe haber frames, no inputs de login
        cur_src = driver.page_source.lower()
        if "botonok" in cur_src or "txtcontrato" in cur_src:
            return None  # Sigue en login = credenciales inválidas

        # 2. Entrar al frame del menú
        try:
            wait.until(EC.frame_to_be_available_and_switch_to_it("frmMenu"))
        except Exception:
            return None

        # 3. Buscar y clicar "Estado de Cuenta"
        try:
            link = wait.until(EC.element_to_be_clickable(
                (By.XPATH, "//a[contains(translate(text(),'ABCDEFGHIJKLMNÑOPQRSTUVWXYZ','abcdefghijklmnñopqrstuvwxyz'),'estado')]")
            ))
            link.click()
        except Exception:
            return None

        driver.switch_to.default_content()
        time.sleep(5)

        # 4. Entrar al frame principal
        try:
            wait.until(EC.frame_to_be_available_and_switch_to_it("frmMain"))
        except Exception:
            return None

        # Esperar a que cargue el contenido dinámico (hasta 15s)
        for _ in range(15):
            time.sleep(1)
            if driver.find_elements(By.TAG_NAME, "a"):
                break

        # 5. Extraer TODOS los links del frame
        frame_url = driver.current_url
        todos_links = driver.find_elements(By.TAG_NAME, "a")

        recibo_info = []
        for a in todos_links:
            texto = (a.text or "").strip()
            href = a.get_attribute("href") or ""
            onclick = a.get_attribute("onclick") or ""

            is_recibo = (
                "ver recibo" in texto.lower()
                or "recibo" in href.lower()
                or ("ver" in texto.lower() and "recibo" in onclick.lower())
            )
            if is_recibo:
                recibo_info.append({
                    "texto": texto,
                    "href": href,
                    "onclick": onclick,
                    "index": len(recibo_info),
                })

        if not recibo_info:
            return None

        # 6. Transferir cookies de Selenium a requests
        user_agent = driver.execute_script("return navigator.userAgent")
        selenium_cookies = driver.get_cookies()

        s = req_lib.Session()
        s.headers.update({
            "User-Agent": user_agent,
            "Referer": frame_url,
            "Accept": "application/pdf,*/*",
        })
        for c in selenium_cookies:
            s.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))

        # 7. Para cada recibo: intentar descarga directa o postback
        archivos_data = []
        for info in recibo_info:
            href = info["href"]
            i = info["index"]
            pdf_bytes = None

            # Caso A: URL directa (no javascript)
            if href and not href.startswith("javascript") and href.startswith("http"):
                try:
                    r = s.get(href, verify=False, timeout=30)
                    ct = r.headers.get("content-type", "").lower()
                    if r.content[:4] == b"%PDF" or "pdf" in ct:
                        pdf_bytes = r.content
                except Exception:
                    pass

            # Caso B: postback ASP.NET (__doPostBack)
            if not pdf_bytes and ("dopostback" in (info["onclick"] + href).lower()
                                   or href.startswith("javascript")):
                try:
                    # Extraer eventTarget y eventArgument del __doPostBack
                    match = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", info["onclick"] + href)
                    if match:
                        event_target = match.group(1)
                        event_arg = match.group(2)
                    else:
                        event_target = ""
                        event_arg = str(i)

                    # Obtener ViewState de la página actual
                    html_frame = driver.page_source
                    soup_f = BeautifulSoup(html_frame, "html.parser")

                    def gv(name):
                        el = soup_f.find("input", {"name": name})
                        return el["value"] if el and el.get("value") else ""

                    post_data = {
                        "__EVENTTARGET": event_target,
                        "__EVENTARGUMENT": event_arg,
                        "__VIEWSTATE": gv("__VIEWSTATE"),
                        "__VIEWSTATEGENERATOR": gv("__VIEWSTATEGENERATOR"),
                        "__EVENTVALIDATION": gv("__EVENTVALIDATION"),
                        "__ASYNCPOST": "true",
                    }
                    r = s.post(frame_url, data=post_data, verify=False, timeout=30,
                               headers={"Content-Type": "application/x-www-form-urlencoded",
                                        "X-Requested-With": "XMLHttpRequest"})
                    ct = r.headers.get("content-type", "").lower()
                    if r.content[:4] == b"%PDF" or "pdf" in ct:
                        pdf_bytes = r.content
                except Exception:
                    pass

            # Caso C: Selenium hace clic y captura el PDF que abre en nueva pestaña
            if not pdf_bytes:
                try:
                    driver.switch_to.frame("frmMain")
                    fresh = driver.find_elements(By.XPATH, "//a[contains(translate(text(),'VERCIBOAB','vercibo'), 'ver recibo')]")
                    if not fresh:
                        fresh = driver.find_elements(By.PARTIAL_LINK_TEXT, "Ver Recibo")
                    if not fresh:
                        fresh = driver.find_elements(By.PARTIAL_LINK_TEXT, "Recibo")

                    if i < len(fresh):
                        ventanas_antes = set(driver.window_handles)
                        fresh[i].click()
                        time.sleep(6)

                        # Ver si abrió nueva ventana
                        nuevas = set(driver.window_handles) - ventanas_antes
                        for v in nuevas:
                            driver.switch_to.window(v)
                            nueva_url = driver.current_url
                            if nueva_url and nueva_url != "about:blank":
                                try:
                                    r = s.get(nueva_url, verify=False, timeout=30)
                                    if r.content[:4] == b"%PDF" or "pdf" in r.headers.get("content-type","").lower():
                                        pdf_bytes = r.content
                                except Exception:
                                    pass
                            driver.close()

                        driver.switch_to.window(driver.window_handles[0])
                        try:
                            driver.switch_to.frame("frmMain")
                        except Exception:
                            pass
                except Exception:
                    try:
                        driver.switch_to.window(driver.window_handles[0])
                    except Exception:
                        pass

            if pdf_bytes and len(pdf_bytes) > 500:
                b64 = base64.b64encode(pdf_bytes).decode("utf-8")
                archivos_data.append({
                    "id": len(archivos_data),
                    "nombre": f"Recibo_{suministro}_{len(archivos_data)+1:02d}.pdf",
                    "base64": b64,
                })

        return archivos_data if archivos_data else None

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# ── Función principal que orquesta ambas opciones ─────────────────────────────

def scrape_recibos(suministro):
    with sessions_lock:
        sessions[suministro] = {"status": "loading", "archivos": [], "error": None}

    try:
        # Intento 1: requests puro (más rápido y confiable)
        archivos = scrape_con_requests(suministro)

        # Intento 2: Selenium + requests combinados
        if not archivos:
            archivos = scrape_con_selenium(suministro)

        if archivos:
            with sessions_lock:
                sessions[suministro]["archivos"] = archivos
                sessions[suministro]["status"] = "done"
        else:
            with sessions_lock:
                sessions[suministro]["status"] = "empty"

    except Exception as e:
        with sessions_lock:
            sessions[suministro]["status"] = "error"
            sessions[suministro]["error"] = str(e)


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
