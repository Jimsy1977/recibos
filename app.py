import os
import glob
import time
import base64
import threading
import io
import re
import tempfile
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
from selenium.common.exceptions import TimeoutException

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
sessions = {}
sessions_lock = threading.Lock()

BASE_URL  = "https://www.chavimochic.gob.pe"
LOGIN_URL = f"{BASE_URL}/iscomweb/iscon/maincon.aspx"

def fix_url(url):
    """Corrige URLs localhost del proxy de ChromeDriver → URL real del portal."""
    if not url:
        return url
    from urllib.parse import urlparse, urlunparse
    p = urlparse(url)
    if p.hostname in ("localhost", "127.0.0.1") or (p.hostname or "").startswith("192.168."):
        bp = urlparse(BASE_URL)
        return urlunparse((bp.scheme, bp.netloc, p.path, p.params, p.query, p.fragment))
    return url

# ── helpers ───────────────────────────────────────────────────────────────────
def log(sumi, msg):
    with sessions_lock:
        sessions[sumi].setdefault("log", []).append(msg)
    print(f"[{sumi}] {msg}", flush=True)

def _shot(driver, sumi, step):
    try:
        b64 = base64.b64encode(driver.get_screenshot_as_png()).decode()
        with sessions_lock:
            sessions[sumi].setdefault("screenshots", []).append({"step": step, "b64": b64})
    except Exception:
        pass

def make_driver(download_dir):
    o = Options()
    o.add_argument("--headless=new")
    o.add_argument("--no-sandbox")
    o.add_argument("--disable-dev-shm-usage")
    o.add_argument("--disable-gpu")
    o.add_argument("--window-size=1920,1080")
    o.add_argument("--disable-blink-features=AutomationControlled")
    o.add_argument("--disable-popup-blocking")
    o.add_argument("--disable-extensions")
    o.add_argument("--lang=es-PE")
    o.add_experimental_option("excludeSwitches", ["enable-automation"])
    o.add_experimental_option("useAutomationExtension", False)
    o.add_experimental_option("prefs", {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,
        "safebrowsing.enabled": True,
    })
    driver = webdriver.Chrome(options=o)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    })
    return driver

def wait_for_pdf(download_dir, known_before, timeout=25):
    """Espera hasta `timeout` s a que aparezca un PDF nuevo — revisa cada 0.5 s."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        current   = set(glob.glob(os.path.join(download_dir, "*.pdf")))
        nuevos    = current - known_before
        parciales = glob.glob(os.path.join(download_dir, "*.crdownload"))
        if nuevos and not parciales:
            return nuevos
        time.sleep(0.5)
    return set()

def tabla_a_lista(html_fragment):
    try:
        soup  = BeautifulSoup(html_fragment, "html.parser")
        tabla = soup.find("table")
        if not tabla:
            return []
        filas = tabla.find_all("tr")
        if not filas:
            return []
        headers = [th.get_text(strip=True) for th in filas[0].find_all(["th","td"])]
        resultado = []
        for fila in filas[1:]:
            celdas = [td.get_text(strip=True) for td in fila.find_all(["td","th"])]
            if any(celdas):
                d = {headers[j] if j < len(headers) else f"Col{j}": v
                     for j, v in enumerate(celdas)}
                resultado.append(d)
        return resultado
    except Exception:
        return []

def make_requests_session(driver, referer=""):
    """Transfiere cookies de Selenium a requests."""
    ua = driver.execute_script("return navigator.userAgent")
    s  = req_lib.Session()
    s.headers.update({"User-Agent": ua, "Referer": fix_url(referer),
                      "Accept": "text/html,application/pdf,*/*"})
    for c in driver.get_cookies():
        s.cookies.set(c["name"], c["value"],
                      domain=c.get("domain","").lstrip("."),
                      path=c.get("path","/"))
    return s

def fetch_seccion_con_requests(s, url, sumi, nombre):
    """Descarga una sección del portal con requests (sin Selenium)."""
    try:
        r = s.get(url, verify=False, timeout=15)
        html = r.text
        log(sumi, f"  {nombre}: requests OK ({len(html)} chars)")
        return html
    except Exception as ex:
        log(sumi, f"  {nombre}: requests error {ex}")
        return ""


# ── scraping principal ────────────────────────────────────────────────────────
def scrape_recibos(suministro):
    download_dir = tempfile.mkdtemp(prefix=f"chavi_{suministro}_")
    with sessions_lock:
        sessions[suministro] = {
            "status": "loading", "archivos": [], "error": None,
            "log": [], "screenshots": [], "html_debug": {},
            "datos": {
                "estado_cuenta": {"filas": [], "html": ""},
                "pagos":         {"filas": [], "html": ""},
                "consumos":      {"filas": [], "html": ""},
            }
        }

    driver = None
    t0 = time.time()
    try:
        log(suministro, f"[0s] Iniciando Chrome → {download_dir}")
        driver = make_driver(download_dir)
        wait   = WebDriverWait(driver, 20)

        # ── 1. Login (~3-5 s) ────────────────────────────────────────────────
        driver.get(LOGIN_URL)
        wait.until(EC.presence_of_element_located((By.ID, "TxtContrato")))
        log(suministro, f"[{time.time()-t0:.1f}s] Página login cargada")

        driver.find_element(By.ID, "TxtContrato").send_keys(suministro)
        driver.find_element(By.ID, "TxtPassword").send_keys(suministro)
        driver.find_element(By.ID, "BotonOK").click()

        # Esperar frame (login OK) en vez de sleep fijo
        try:
            wait.until(EC.frame_to_be_available_and_switch_to_it("frmMenu"))
            log(suministro, f"[{time.time()-t0:.1f}s] Login exitoso — frmMenu disponible")
        except TimeoutException:
            log(suministro, "Login timeout — suministro inválido")
            with sessions_lock:
                sessions[suministro]["status"] = "empty"
            return

        # Capturar menú: obtener URLs de cada sección
        menu_urls = {}
        for a in driver.find_elements(By.TAG_NAME, "a"):
            t  = (a.text or "").strip()
            h  = fix_url(a.get_attribute("href") or "")
            tl = t.lower()
            if h and not h.startswith("javascript"):
                menu_urls[tl] = h
            log(suministro, f"  Menu: '{t}' → '{h}'")

        log(suministro, f"[{time.time()-t0:.1f}s] Menu URLs: {menu_urls}")

        # ── 3. Click Estado de Cuenta → capturar frmMain ────────────────────
        clicked = False
        for a in driver.find_elements(By.TAG_NAME, "a"):
            if "estado" in (a.text or "").lower():
                driver.execute_script("arguments[0].click();", a)
                clicked = True
                break

        if not clicked:
            log(suministro, "No se encontró 'Estado de Cuenta' en el menú")
            with sessions_lock:
                sessions[suministro]["status"] = "empty"
            return

        driver.switch_to.default_content()

        # Esperar frmMain con contenido (tabla o link) — máx 12 s
        try:
            wait.until(EC.frame_to_be_available_and_switch_to_it("frmMain"))
            WebDriverWait(driver, 12).until(
                lambda d: d.find_elements(By.TAG_NAME, "a") or
                          d.find_elements(By.TAG_NAME, "table")
            )
            log(suministro, f"[{time.time()-t0:.1f}s] frmMain con contenido")
        except TimeoutException:
            log(suministro, "Timeout esperando frmMain")
            with sessions_lock:
                sessions[suministro]["status"] = "empty"
            return

        # Scroll para activar lazy-load
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(0.5)

        _shot(driver, suministro, "3_estado_cuenta")
        html_ec   = driver.page_source
        frame_url = fix_url(driver.current_url)  # ← corregir localhost proxy
        log(suministro, f"[{time.time()-t0:.1f}s] frame_url={frame_url}")

        with sessions_lock:
            sessions[suministro]["datos"]["estado_cuenta"]["html"]  = html_ec
            sessions[suministro]["datos"]["estado_cuenta"]["filas"] = tabla_a_lista(html_ec)
            sessions[suministro]["html_debug"]["estado_cuenta"]     = html_ec[:6000]
        log(suministro, f"[{time.time()-t0:.1f}s] Estado cuenta: {len(sessions[suministro]['datos']['estado_cuenta']['filas'])} filas")

        # ── 4. Capturar links de recibo ───────────────────────────────────────
        recibo_raw = []
        for a in driver.find_elements(By.TAG_NAME, "a"):
            t  = (a.text or "").strip()
            h  = a.get_attribute("href") or ""
            oc = a.get_attribute("onclick") or ""
            if "recibo" in t.lower():
                recibo_raw.append({"texto": t, "href": h, "onclick": oc})
                log(suministro, f"  Recibo link: '{t}' href='{h}'")
        log(suministro, f"[{time.time()-t0:.1f}s] Links recibo: {len(recibo_raw)}")

        # ── 5. Transferir cookies a requests ─────────────────────────────────
        req_s = make_requests_session(driver, referer=frame_url)

        # ── 6. Sus Pagos y Sus Consumos con requests (sin Selenium) ──────────
        # Si el menú entregó URLs directas, usarlas; si no, encontrar el link
        # en el HTML del menú que ya cargó
        def url_seccion(keyword):
            for k, u in menu_urls.items():
                if keyword in k:
                    return u
            return ""

        url_pagos    = url_seccion("pago")
        url_consumos = url_seccion("consumo")
        log(suministro, f"[{time.time()-t0:.1f}s] URL pagos='{url_pagos}' consumos='{url_consumos}'")

        # Descargar en paralelo con threading
        resultados = {"pagos": "", "consumos": ""}

        def fetch_pagos():
            if url_pagos:
                resultados["pagos"] = fetch_seccion_con_requests(req_s, url_pagos, suministro, "Sus Pagos")

        def fetch_consumos():
            if url_consumos:
                resultados["consumos"] = fetch_seccion_con_requests(req_s, url_consumos, suministro, "Sus Consumos")

        # Lanzar ambas en paralelo
        t_pag = threading.Thread(target=fetch_pagos, daemon=True)
        t_con = threading.Thread(target=fetch_consumos, daemon=True)
        t_pag.start(); t_con.start()

        # Mientras esperan, procesamos los links de recibo con Selenium
        # (las secciones se cargan sin bloquear el browser)

        # ── 7. Descargar PDFs ─────────────────────────────────────────────────
        if not recibo_raw:
            log(suministro, "Sin links de recibo")
            t_pag.join(); t_con.join()
            with sessions_lock:
                sessions[suministro]["status"] = "empty"
            return

        log(suministro, f"[{time.time()-t0:.1f}s] === Descargando {len(recibo_raw)} PDFs ===")

        soup_ec = BeautifulSoup(html_ec, "html.parser")
        def gv(name):
            el = soup_ec.find("input", {"name": name})
            return el["value"] if el and el.get("value") else ""
        vs = gv("__VIEWSTATE"); vsg = gv("__VIEWSTATEGENERATOR"); ev = gv("__EVENTVALIDATION")

        archivos_data = []

        for i, info in enumerate(recibo_raw):
            href = info["href"]; texto = info["texto"]; onclick = info["onclick"]
            pdf_bytes = None
            log(suministro, f"[{time.time()-t0:.1f}s] --- Recibo {i+1}/{len(recibo_raw)}: '{texto}' ---")

            # Estrategia A: URL directa
            if href and not href.startswith("javascript") and href.startswith("http"):
                href_fixed = fix_url(href)
                try:
                    r = req_s.get(href_fixed, verify=False, timeout=20)
                    ct = r.headers.get("content-type","").lower()
                    log(suministro, f"  A: {r.status_code} ct={ct} bytes={len(r.content)} url={href_fixed}")
                    if r.content[:4] == b"%PDF" or "pdf" in ct:
                        pdf_bytes = r.content
                        log(suministro, "  A ✓")
                except Exception as ex:
                    log(suministro, f"  A ✗ {ex}")


            # Estrategia B: postback ASP.NET
            if not pdf_bytes:
                m = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", onclick + href)
                if m:
                    try:
                        r = req_s.post(frame_url, verify=False, timeout=20, data={
                            "__EVENTTARGET": m.group(1), "__EVENTARGUMENT": m.group(2),
                            "__VIEWSTATE": vs, "__VIEWSTATEGENERATOR": vsg, "__EVENTVALIDATION": ev,
                        })
                        ct = r.headers.get("content-type","").lower()
                        log(suministro, f"  B: {r.status_code} ct={ct} bytes={len(r.content)}")
                        if r.content[:4] == b"%PDF" or "pdf" in ct:
                            pdf_bytes = r.content
                            log(suministro, "  B ✓")
                    except Exception as ex:
                        log(suministro, f"  B ✗ {ex}")

            # Estrategia C: click Selenium → PDF en disco
            if not pdf_bytes:
                try:
                    log(suministro, f"  C: click [{i}] → disco")
                    pdfs_antes = set(glob.glob(os.path.join(download_dir, "*.pdf")))
                    fl = [a for a in driver.find_elements(By.TAG_NAME, "a")
                          if "recibo" in (a.text or "").lower()]
                    log(suministro, f"  C: links en DOM={len(fl)}")

                    if i < len(fl):
                        ventanas_antes = set(driver.window_handles)
                        driver.execute_script("arguments[0].click();", fl[i])

                        # Esperar PDF en disco (máx 25 s)
                        nuevos = wait_for_pdf(download_dir, pdfs_antes, timeout=25)
                        log(suministro, f"  C: PDFs nuevos={nuevos}")

                        if nuevos:
                            path = sorted(nuevos)[0]
                            with open(path, "rb") as f:
                                pdf_bytes = f.read()
                            log(suministro, f"  C ✓ disco {os.path.basename(path)} ({len(pdf_bytes)}B)")

                        else:
                            # Analizar nueva pestaña
                            nuevas_v = set(driver.window_handles) - ventanas_antes
                            for v in nuevas_v:
                                driver.switch_to.window(v)
                                time.sleep(2)
                                tab_url  = fix_url(driver.current_url)  # ← fix localhost
                                tab_html = driver.page_source
                                _shot(driver, suministro, f"tab_{i}")
                                log(suministro, f"  C: tab_url={tab_url}")
                                with sessions_lock:
                                    sessions[suministro]["html_debug"][f"tab_{i}"] = tab_html[:5000]

                                # Requests en tab_url
                                if tab_url and tab_url not in ("about:blank",""):
                                    try:
                                        r = req_s.get(tab_url, verify=False, timeout=20)
                                        ct = r.headers.get("content-type","").lower()
                                        log(suministro, f"  C req tab: {r.status_code} ct={ct} B={len(r.content)}")
                                        if r.content[:4] == b"%PDF" or "pdf" in ct:
                                            pdf_bytes = r.content
                                            log(suministro, "  C ✓ tab requests")
                                    except Exception as ex:
                                        log(suministro, f"  C tab req ✗ {ex}")

                                # Buscar PDF en HTML de pestaña
                                if not pdf_bytes and tab_html:
                                    ts   = BeautifulSoup(tab_html, "html.parser")
                                    cands = []
                                    for tg in ts.find_all(["embed","object"]):
                                        src = tg.get("src") or tg.get("data","")
                                        if src: cands.append(urljoin(tab_url, src))
                                    for tg in ts.find_all("iframe"):
                                        src = tg.get("src","")
                                        if src and "blank" not in src: cands.append(urljoin(tab_url, src))
                                    for j in re.findall(r'["\']([^"\']+\.pdf[^"\']*)["\']', tab_html, re.I):
                                        cands.append(urljoin(tab_url, j))
                                    b64m = re.search(r'data:application/pdf;base64,([A-Za-z0-9+/=]+)', tab_html)
                                    if b64m:
                                        try:
                                            pdf_bytes = base64.b64decode(b64m.group(1))
                                            log(suministro, f"  C ✓ data-URI ({len(pdf_bytes)}B)")
                                        except Exception: pass
                                    for purl in cands:
                                        if pdf_bytes: break
                                        try:
                                            r = req_s.get(purl, verify=False, timeout=20)
                                            if r.content[:4] == b"%PDF" or "pdf" in r.headers.get("content-type","").lower():
                                                pdf_bytes = r.content
                                                log(suministro, f"  C ✓ cand {purl}")
                                        except Exception: pass

                                # CDP printToPDF como último recurso
                                if not pdf_bytes:
                                    try:
                                        res = driver.execute_cdp_cmd("Page.printToPDF",{
                                            "printBackground":True,"preferCSSPageSize":True,
                                            "marginTop":0,"marginBottom":0,"marginLeft":0,"marginRight":0
                                        })
                                        data = base64.b64decode(res.get("data",""))
                                        if len(data) > 500:
                                            pdf_bytes = data
                                            log(suministro, f"  C ✓ printToPDF ({len(pdf_bytes)}B)")
                                    except Exception as ex:
                                        log(suministro, f"  C printToPDF ✗ {ex}")

                                driver.close()

                        driver.switch_to.window(driver.window_handles[0])
                        try: driver.switch_to.frame("frmMain")
                        except Exception: pass
                    else:
                        log(suministro, f"  C: índice {i} fuera de rango ({len(fl)})")
                except Exception as ex:
                    log(suministro, f"  C ✗ {ex}")
                    try: driver.switch_to.window(driver.window_handles[0])
                    except Exception: pass

            # Guardar PDF
            if pdf_bytes and len(pdf_bytes) > 500:
                nombre = re.sub(r"[^a-zA-Z0-9_\-]", "_", texto) or f"Recibo_{i+1:02d}"
                archivos_data.append({
                    "id": len(archivos_data),
                    "nombre": f"{nombre}.pdf",
                    "base64": base64.b64encode(pdf_bytes).decode()
                })
                log(suministro, f"  GUARDADO: {nombre}.pdf ({len(pdf_bytes)}B)")
            else:
                log(suministro, f"  SIN PDF recibo {i+1}")

        # ── 8. Esperar resultados de Pagos/Consumos (deberían ya estar) ───────
        t_pag.join(timeout=5)
        t_con.join(timeout=5)

        html_pag = resultados["pagos"]
        html_con = resultados["consumos"]

        # Si requests no trajo URLs del menú, intentar Selenium como fallback
        if not html_pag or not html_con:
            log(suministro, f"[{time.time()-t0:.1f}s] Fallback Selenium para secciones sin URL")

            def navegar_rapido(texto_buscar):
                try:
                    driver.switch_to.default_content()
                    WebDriverWait(driver, 8).until(
                        EC.frame_to_be_available_and_switch_to_it("frmMenu"))
                    for a in driver.find_elements(By.TAG_NAME, "a"):
                        if texto_buscar in (a.text or "").lower():
                            driver.execute_script("arguments[0].click();", a)
                            break
                    driver.switch_to.default_content()
                    WebDriverWait(driver, 10).until(
                        EC.frame_to_be_available_and_switch_to_it("frmMain"))
                    WebDriverWait(driver, 8).until(
                        lambda d: d.find_elements(By.TAG_NAME, "table") or
                                  d.find_elements(By.TAG_NAME, "tr"))
                    time.sleep(0.5)
                    return driver.page_source
                except Exception as ex:
                    log(suministro, f"  navegar_rapido({texto_buscar}) error: {ex}")
                    return ""

            if not html_pag:
                html_pag = navegar_rapido("sus pagos") or navegar_rapido("pago")
            if not html_con:
                html_con = navegar_rapido("sus consumos") or navegar_rapido("consumo")

        with sessions_lock:
            if html_pag:
                sessions[suministro]["datos"]["pagos"]["html"]  = html_pag
                sessions[suministro]["datos"]["pagos"]["filas"] = tabla_a_lista(html_pag)
                sessions[suministro]["html_debug"]["pagos"]     = html_pag[:6000]
                log(suministro, f"[{time.time()-t0:.1f}s] Pagos: {len(sessions[suministro]['datos']['pagos']['filas'])} filas")
            if html_con:
                sessions[suministro]["datos"]["consumos"]["html"]  = html_con
                sessions[suministro]["datos"]["consumos"]["filas"] = tabla_a_lista(html_con)
                sessions[suministro]["html_debug"]["consumos"]     = html_con[:6000]
                log(suministro, f"[{time.time()-t0:.1f}s] Consumos: {len(sessions[suministro]['datos']['consumos']['filas'])} filas")

        # ── Resultado final ───────────────────────────────────────────────────
        elapsed = time.time() - t0
        if archivos_data:
            log(suministro, f"[{elapsed:.1f}s] ÉXITO: {len(archivos_data)}/{len(recibo_raw)} recibo(s)")
            with sessions_lock:
                sessions[suministro]["archivos"] = archivos_data
                sessions[suministro]["status"]   = "done"
        else:
            log(suministro, f"[{elapsed:.1f}s] Sin PDFs")
            with sessions_lock:
                sessions[suministro]["status"] = "empty"

    except Exception as e:
        import traceback
        msg = f"{e}\n{traceback.format_exc()}"
        log(suministro, f"EXCEPCIÓN: {msg}")
        with sessions_lock:
            sessions[suministro]["status"] = "error"
            sessions[suministro]["error"]  = str(e)
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass


# ── Rutas Flask ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/consultar", methods=["POST"])
def consultar():
    sumi = request.form.get("suministro","").strip()
    if not sumi:
        return render_template("error.html", mensaje="Ingresa un número de suministro.", suministro="")
    threading.Thread(target=scrape_recibos, args=(sumi,), daemon=True).start()
    return render_template("loading.html", suministro=sumi)

@app.route("/estado/<suministro>")
def estado(suministro):
    with sessions_lock:
        s = sessions.get(suministro)
    if not s: return jsonify({"status":"not_found"})
    if s["status"] == "done":
        return jsonify({"status":"done","total":len(s["archivos"]),
                        "archivos":[{"id":a["id"],"nombre":a["nombre"]} for a in s["archivos"]]})
    if s["status"] == "empty": return jsonify({"status":"empty"})
    if s["status"] == "error": return jsonify({"status":"error","error":s.get("error","")})
    return jsonify({"status":"loading"})

@app.route("/debug/<suministro>")
def debug(suministro):
    with sessions_lock:
        se = sessions.get(suministro, {})
    log_html   = "<br>".join(se.get("log",[]))
    shots_html = "".join(f'<h3>{x["step"]}</h3><img src="data:image/png;base64,{x["b64"]}" style="max-width:100%;margin-bottom:10px;border:1px solid #ccc">'
                          for x in se.get("screenshots",[]))
    dbg_html   = "".join(f'<h3>{k}</h3><pre style="background:#111;color:#0f0;padding:10px;overflow:auto;max-height:300px">{v[:3000]}</pre>'
                          for k,v in se.get("html_debug",{}).items())
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Debug {suministro}</title>
    <style>body{{font-family:monospace;padding:20px;background:#1a1a1a;color:#eee}}</style></head><body>
    <h1>Debug: {suministro}</h1><h2>Estado: {se.get('status','N/A')}</h2>
    <h2>Log</h2><pre style="background:#111;color:#0f0;padding:12px">{log_html}</pre>
    <h2>Screenshots</h2>{shots_html}
    <h2>HTML debug</h2>{dbg_html}
    </body></html>"""

@app.route("/error")
def error_page():
    return render_template("error.html",
                           mensaje=request.args.get("msg","Error."),
                           suministro=request.args.get("suministro",""))

@app.route("/recibos/<suministro>")
def recibos(suministro):
    with sessions_lock:
        s = sessions.get(suministro)
    if not s or s["status"] != "done":
        return render_template("error.html",
                               mensaje="Sesión expirada. Realiza una nueva consulta.",
                               suministro=suministro)
    archivos = [{"id":a["id"],"nombre":a["nombre"]} for a in s["archivos"]]
    return render_template("recibos.html", suministro=suministro, archivos=archivos)

@app.route("/datos/<suministro>/estado")
def datos_estado(suministro):
    with sessions_lock:
        s = sessions.get(suministro)
    if not s or s["status"] != "done":
        return render_template("error.html", mensaje="Sesión no disponible.", suministro=suministro)
    d = s["datos"]["estado_cuenta"]
    return render_template("datos_estado.html", suministro=suministro,
                           filas=d["filas"], html_raw=d["html"])

@app.route("/datos/<suministro>/pagos")
def datos_pagos(suministro):
    with sessions_lock:
        s = sessions.get(suministro)
    if not s or s["status"] != "done":
        return render_template("error.html", mensaje="Sesión no disponible.", suministro=suministro)
    d = s["datos"]["pagos"]
    return render_template("datos_pagos.html", suministro=suministro,
                           filas=d["filas"], html_raw=d["html"])

@app.route("/datos/<suministro>/consumos")
def datos_consumos(suministro):
    with sessions_lock:
        s = sessions.get(suministro)
    if not s or s["status"] != "done":
        return render_template("error.html", mensaje="Sesión no disponible.", suministro=suministro)
    d = s["datos"]["consumos"]
    return render_template("datos_consumos.html", suministro=suministro,
                           filas=d["filas"], html_raw=d["html"])

@app.route("/ver/<suministro>/<int:idx>")
def ver_recibo(suministro, idx):
    with sessions_lock:
        s = sessions.get(suministro)
    if not s or s["status"] != "done": abort(404)
    archivos = s["archivos"]
    if idx >= len(archivos): abort(404)
    total = len(archivos)
    return render_template("visor.html", suministro=suministro, archivo=archivos[idx],
                           idx=idx, total=total,
                           prev_idx=idx-1 if idx > 0 else None,
                           next_idx=idx+1 if idx < total-1 else None)

@app.route("/descargar/<suministro>/<int:idx>")
def descargar(suministro, idx):
    with sessions_lock:
        s = sessions.get(suministro)
    if not s or s["status"] != "done": abort(404)
    archivos = s["archivos"]
    if idx >= len(archivos): abort(404)
    a = archivos[idx]
    return send_file(io.BytesIO(base64.b64decode(a["base64"])),
                     mimetype="application/pdf", as_attachment=True,
                     download_name=a["nombre"])

@app.route("/pdf_data/<suministro>/<int:idx>")
def pdf_data(suministro, idx):
    with sessions_lock:
        s = sessions.get(suministro)
    if not s or s["status"] != "done": abort(404)
    archivos = s["archivos"]
    if idx >= len(archivos): abort(404)
    return jsonify({"base64":archivos[idx]["base64"],"nombre":archivos[idx]["nombre"]})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=False)
