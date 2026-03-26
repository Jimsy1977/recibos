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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
sessions = {}
sessions_lock = threading.Lock()

BASE_URL  = "https://www.chavimochic.gob.pe"
LOGIN_URL = f"{BASE_URL}/iscomweb/iscon/maincon.aspx"

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

def wait_for_pdf(download_dir, known_before, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        current  = set(glob.glob(os.path.join(download_dir, "*.pdf")))
        nuevos   = current - known_before
        parciales = glob.glob(os.path.join(download_dir, "*.crdownload"))
        if nuevos and not parciales:
            return nuevos
        time.sleep(1)
    return set()

def tabla_a_lista(html_fragrment):
    """Convierte un <table> HTML a lista de dicts."""
    try:
        soup = BeautifulSoup(html_fragrment, "html.parser")
        tabla = soup.find("table")
        if not tabla:
            return []
        filas = tabla.find_all("tr")
        if not filas:
            return []
        encabezados = [th.get_text(strip=True) for th in filas[0].find_all(["th","td"])]
        resultado = []
        for fila in filas[1:]:
            celdas = [td.get_text(strip=True) for td in fila.find_all(["td","th"])]
            if any(celdas):
                d = {}
                for j, enc in enumerate(encabezados):
                    d[enc or f"Col{j}"] = celdas[j] if j < len(celdas) else ""
                resultado.append(d)
        return resultado
    except Exception:
        return []

def navegar_seccion(driver, sumi, texto_menu):
    """
    Desde default_content, clic en el menú y devuelve el HTML de frmMain.
    Retorna (html, soup) o (None, None) si falla.
    """
    wait = WebDriverWait(driver, 20)
    try:
        driver.switch_to.default_content()
        wait.until(EC.frame_to_be_available_and_switch_to_it("frmMenu"))
        clicked = False
        for a in driver.find_elements(By.TAG_NAME, "a"):
            if texto_menu.lower() in (a.text or "").lower():
                log(sumi, f"  Menu click: '{a.text.strip()}'")
                driver.execute_script("arguments[0].click();", a)
                clicked = True
                break
        if not clicked:
            log(sumi, f"  No encontrado en menu: '{texto_menu}'")
            return None, None
        driver.switch_to.default_content()
        time.sleep(5)
        wait.until(EC.frame_to_be_available_and_switch_to_it("frmMain"))
        # Espera activa por contenido
        for _ in range(10):
            if len(driver.find_elements(By.TAG_NAME, "table")) > 0:
                break
            time.sleep(1)
        time.sleep(2)
        html = driver.page_source
        soup = BeautifulSoup(html, "html.parser")
        return html, soup
    except Exception as e:
        log(sumi, f"  navegar_seccion({texto_menu}) error: {e}")
        return None, None


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
    try:
        log(suministro, f"Iniciando Chrome → {download_dir}")
        driver = make_driver(download_dir)
        wait  = WebDriverWait(driver, 25)

        # ── 1. Login ──────────────────────────────────────────────────────────
        driver.get(LOGIN_URL)
        wait.until(EC.presence_of_element_located((By.ID, "TxtContrato")))
        _shot(driver, suministro, "1_login")
        driver.find_element(By.ID, "TxtContrato").send_keys(suministro)
        driver.find_element(By.ID, "TxtPassword").send_keys(suministro)
        driver.find_element(By.ID, "BotonOK").click()
        time.sleep(7)

        if driver.find_elements(By.ID, "TxtContrato"):
            log(suministro, "Login fallido")
            with sessions_lock:
                sessions[suministro]["status"] = "empty"
            return

        log(suministro, f"Login OK — {driver.current_url}")
        _shot(driver, suministro, "2_post_login")

        # ── 2. Verificar frames ───────────────────────────────────────────────
        frames = [f.get_attribute("name") or "?" for f in
                  driver.find_elements(By.TAG_NAME, "frame") +
                  driver.find_elements(By.TAG_NAME, "iframe")]
        log(suministro, f"Frames: {frames}")

        # ── 3. Estado de Cuenta ───────────────────────────────────────────────
        log(suministro, "=== Sección: Estado de Cuenta ===")
        html_ec, soup_ec = navegar_seccion(driver, suministro, "estado de cuenta")
        _shot(driver, suministro, "3_estado_cuenta")
        if html_ec:
            with sessions_lock:
                sessions[suministro]["datos"]["estado_cuenta"]["html"] = html_ec
                sessions[suministro]["datos"]["estado_cuenta"]["filas"] = tabla_a_lista(html_ec)
                sessions[suministro]["html_debug"]["estado_cuenta"] = html_ec[:6000]
            log(suministro, f"  Estado cuenta: {len(sessions[suministro]['datos']['estado_cuenta']['filas'])} filas tabla")

        # Guardar TODOS los links de recibo (sin deduplicar por href)
        recibo_raw = []
        if html_ec:
            for a in driver.find_elements(By.TAG_NAME, "a"):
                t = (a.text or "").strip()
                h = a.get_attribute("href") or ""
                oc = a.get_attribute("onclick") or ""
                if "recibo" in t.lower():
                    recibo_raw.append({"texto": t, "href": h, "onclick": oc, "idx": len(recibo_raw)})
                    log(suministro, f"  Link recibo [{len(recibo_raw)-1}]: '{t}' href='{h}' onclick='{oc[:80]}'")

        log(suministro, f"Total links recibo: {len(recibo_raw)}")

        # ── 4. Pagos Registrados ──────────────────────────────────────────────
        log(suministro, "=== Sección: Sus Pagos ===")
        html_pag, soup_pag = navegar_seccion(driver, suministro, "sus pagos")
        if not html_pag:
            html_pag, soup_pag = navegar_seccion(driver, suministro, "pago")
        _shot(driver, suministro, "4_pagos")
        if html_pag:
            with sessions_lock:
                sessions[suministro]["datos"]["pagos"]["html"] = html_pag
                sessions[suministro]["datos"]["pagos"]["filas"] = tabla_a_lista(html_pag)
                sessions[suministro]["html_debug"]["pagos"] = html_pag[:6000]
            log(suministro, f"  Pagos: {len(sessions[suministro]['datos']['pagos']['filas'])} filas")

        # ── 5. Consumos Registrados ───────────────────────────────────────────
        log(suministro, "=== Sección: Sus Consumos ===")
        html_con, soup_con = navegar_seccion(driver, suministro, "sus consumos")
        if not html_con:
            html_con, soup_con = navegar_seccion(driver, suministro, "consumo")
        _shot(driver, suministro, "5_consumos")
        if html_con:
            with sessions_lock:
                sessions[suministro]["datos"]["consumos"]["html"] = html_con
                sessions[suministro]["datos"]["consumos"]["filas"] = tabla_a_lista(html_con)
                sessions[suministro]["html_debug"]["consumos"] = html_con[:6000]
            log(suministro, f"  Consumos: {len(sessions[suministro]['datos']['consumos']['filas'])} filas")

        # ── 6. Volver a Estado de Cuenta para descargar PDFs ─────────────────
        if not recibo_raw:
            log(suministro, "Sin links de recibo — empty")
            with sessions_lock:
                sessions[suministro]["status"] = "empty"
            return

        log(suministro, "=== Descargando PDFs ===")
        html_ec2, _ = navegar_seccion(driver, suministro, "estado de cuenta")
        if not html_ec2:
            with sessions_lock:
                sessions[suministro]["status"] = "empty"
            return

        _shot(driver, suministro, "6_frmMain_descarga")

        # Preparar requests session con cookies
        frame_url  = driver.current_url
        user_agent = driver.execute_script("return navigator.userAgent")
        s = req_lib.Session()
        s.headers.update({"User-Agent": user_agent, "Referer": frame_url})
        for c in driver.get_cookies():
            s.cookies.set(c["name"], c["value"],
                          domain=c.get("domain","").lstrip("."),
                          path=c.get("path","/"))

        soup_main = BeautifulSoup(html_ec2 or "", "html.parser")
        def gv(name):
            el = soup_main.find("input", {"name": name})
            return el["value"] if el and el.get("value") else ""
        vs = gv("__VIEWSTATE"); vsg = gv("__VIEWSTATEGENERATOR"); ev = gv("__EVENTVALIDATION")

        # Re-obtener links frescos del DOM actual
        links_frescos = [a for a in driver.find_elements(By.TAG_NAME, "a")
                         if "recibo" in (a.text or "").lower()]
        log(suministro, f"Links frescos en DOM: {len(links_frescos)}")

        # Asegurar que tenemos los mismos que recibo_raw
        total_pdfs = len(recibo_raw)
        archivos_data = []

        for i in range(total_pdfs):
            info   = recibo_raw[i]
            href   = info["href"]
            texto  = info["texto"]
            onclick = info["onclick"]
            pdf_bytes = None

            log(suministro, f"--- Recibo {i+1}/{total_pdfs}: '{texto}' ---")

            # Estrategia A: URL directa con requests
            if href and not href.startswith("javascript") and href.startswith("http"):
                try:
                    r = s.get(href, verify=False, timeout=30)
                    ct = r.headers.get("content-type","").lower()
                    log(suministro, f"  A: {r.status_code} ct={ct} bytes={len(r.content)}")
                    if r.content[:4] == b"%PDF" or "pdf" in ct:
                        pdf_bytes = r.content
                        log(suministro, "  A ✓ PDF directo")
                except Exception as ex:
                    log(suministro, f"  A ✗ {ex}")

            # Estrategia B: postback ASP.NET
            if not pdf_bytes:
                m = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", onclick + href)
                if m:
                    try:
                        r = s.post(frame_url, verify=False, timeout=30, data={
                            "__EVENTTARGET": m.group(1), "__EVENTARGUMENT": m.group(2),
                            "__VIEWSTATE": vs, "__VIEWSTATEGENERATOR": vsg, "__EVENTVALIDATION": ev,
                        })
                        ct = r.headers.get("content-type","").lower()
                        log(suministro, f"  B: {r.status_code} ct={ct} bytes={len(r.content)}")
                        if r.content[:4] == b"%PDF" or "pdf" in ct:
                            pdf_bytes = r.content
                            log(suministro, "  B ✓ PDF postback")
                    except Exception as ex:
                        log(suministro, f"  B ✗ {ex}")

            # Estrategia C: click Selenium → esperar archivo en disco
            if not pdf_bytes:
                try:
                    log(suministro, f"  C: click link[{i}] → esperar PDF en disco")
                    pdfs_antes = set(glob.glob(os.path.join(download_dir, "*.pdf")))

                    # re-obtener links del DOM
                    fl = [a for a in driver.find_elements(By.TAG_NAME, "a")
                          if "recibo" in (a.text or "").lower()]
                    log(suministro, f"  C: links en DOM={len(fl)}, usando índice {i}")

                    if i < len(fl):
                        ventanas_antes = set(driver.window_handles)
                        driver.execute_script("arguments[0].click();", fl[i])
                        log(suministro, f"  C: click hecho, esperando PDF (40s)...")

                        # Esperar PDF en disco
                        nuevos = wait_for_pdf(download_dir, pdfs_antes, timeout=40)
                        log(suministro, f"  C: PDFs nuevos={nuevos}")

                        if nuevos:
                            path = sorted(nuevos)[0]
                            with open(path, "rb") as f:
                                pdf_bytes = f.read()
                            log(suministro, f"  C ✓ PDF disco: {os.path.basename(path)} ({len(pdf_bytes)}bytes)")

                        else:
                            # Revisar nueva pestaña
                            nuevas_v = set(driver.window_handles) - ventanas_antes
                            log(suministro, f"  C: nuevas pestañas={len(nuevas_v)}")
                            for v in nuevas_v:
                                driver.switch_to.window(v)
                                time.sleep(3)
                                tab_url = driver.current_url
                                tab_html = driver.page_source
                                _shot(driver, suministro, f"7_tab_{i}")
                                log(suministro, f"  C: tab_url={tab_url} html(100)={tab_html[:100]}")
                                with sessions_lock:
                                    sessions[suministro]["html_debug"][f"tab_{i}"] = tab_html[:5000]

                                # Intento requests en tab_url
                                if tab_url and tab_url not in ("about:blank",""):
                                    try:
                                        r = s.get(tab_url, verify=False, timeout=30)
                                        ct = r.headers.get("content-type","").lower()
                                        log(suministro, f"  C: req tab {r.status_code} ct={ct} bytes={len(r.content)}")
                                        if r.content[:4]==b"%PDF" or "pdf" in ct:
                                            pdf_bytes = r.content
                                            log(suministro, "  C ✓ PDF tab requests")
                                    except Exception as ex:
                                        log(suministro, f"  C: req tab error {ex}")

                                # Buscar en HTML de la pestaña
                                if not pdf_bytes and tab_html:
                                    ts = BeautifulSoup(tab_html, "html.parser")
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
                                        except Exception: pass
                                    log(suministro, f"  C: candidatos HTML={cands}")
                                    for purl in cands:
                                        if pdf_bytes: break
                                        try:
                                            r = s.get(purl, verify=False, timeout=30)
                                            if r.content[:4]==b"%PDF" or "pdf" in r.headers.get("content-type","").lower():
                                                pdf_bytes = r.content
                                        except Exception: pass

                                # CDP printToPDF
                                if not pdf_bytes:
                                    try:
                                        res = driver.execute_cdp_cmd("Page.printToPDF",{
                                            "printBackground":True,"preferCSSPageSize":True,
                                            "marginTop":0,"marginBottom":0,"marginLeft":0,"marginRight":0
                                        })
                                        data = base64.b64decode(res.get("data",""))
                                        if len(data) > 500:
                                            pdf_bytes = data
                                            log(suministro, f"  C ✓ printToPDF ({len(data)}bytes)")
                                    except Exception as ex:
                                        log(suministro, f"  C: printToPDF error: {ex}")

                                driver.close()

                        # Volver a frmMain
                        driver.switch_to.window(driver.window_handles[0])
                        try: driver.switch_to.frame("frmMain")
                        except Exception: pass

                    else:
                        log(suministro, f"  C: índice {i} >= links disponibles {len(fl)}")
                except Exception as ex:
                    log(suministro, f"  C ✗ {ex}")
                    try: driver.switch_to.window(driver.window_handles[0])
                    except Exception: pass

            # Guardar PDF
            if pdf_bytes and len(pdf_bytes) > 500:
                nombre = re.sub(r"[^a-zA-Z0-9_\-]", "_", texto) or f"Recibo_{i+1:02d}"
                b64 = base64.b64encode(pdf_bytes).decode()
                archivos_data.append({"id": len(archivos_data), "nombre": f"{nombre}.pdf", "base64": b64})
                log(suministro, f"  GUARDADO: {nombre}.pdf ({len(pdf_bytes)}bytes)")
            else:
                log(suministro, f"  SIN PDF recibo {i+1}")

        # ── Resultado ─────────────────────────────────────────────────────────
        if archivos_data:
            log(suministro, f"ÉXITO: {len(archivos_data)}/{total_pdfs} recibos")
            with sessions_lock:
                sessions[suministro]["archivos"] = archivos_data
                sessions[suministro]["status"] = "done"
        else:
            log(suministro, "Sin PDFs al final")
            with sessions_lock:
                sessions[suministro]["status"] = "empty"

    except Exception as e:
        import traceback
        msg = f"{e}\n{traceback.format_exc()}"
        log(suministro, f"EXCEPCIÓN: {msg}")
        with sessions_lock:
            sessions[suministro]["status"] = "error"
            sessions[suministro]["error"] = str(e)
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
    log_html  = "<br>".join(se.get("log",[]))
    shots_html = "".join(f'<h3>{x["step"]}</h3><img src="data:image/png;base64,{x["b64"]}" style="max-width:100%;margin-bottom:10px;border:1px solid #ccc">'
                          for x in se.get("screenshots",[]))
    dbg_html  = "".join(f'<h3>{k}</h3><pre style="background:#111;color:#0f0;padding:10px;overflow:auto;max-height:350px">{v[:3000]}</pre>'
                         for k,v in se.get("html_debug",{}).items())
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Debug {suministro}</title>
    <style>body{{font-family:monospace;padding:20px;background:#1a1a1a;color:#eee}}
    pre{{background:#111;color:#0f0;padding:10px}}img{{border-radius:8px}}</style></head><body>
    <h1>Debug: {suministro}</h1><h2>Estado: {se.get('status','N/A')}</h2>
    <h2>Log</h2><pre>{log_html}</pre>
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
    datos = s["datos"]["estado_cuenta"]
    return render_template("datos_estado.html", suministro=suministro,
                           filas=datos["filas"], html_raw=datos["html"])

@app.route("/datos/<suministro>/pagos")
def datos_pagos(suministro):
    with sessions_lock:
        s = sessions.get(suministro)
    if not s or s["status"] != "done":
        return render_template("error.html", mensaje="Sesión no disponible.", suministro=suministro)
    datos = s["datos"]["pagos"]
    return render_template("datos_pagos.html", suministro=suministro,
                           filas=datos["filas"], html_raw=datos["html"])

@app.route("/datos/<suministro>/consumos")
def datos_consumos(suministro):
    with sessions_lock:
        s = sessions.get(suministro)
    if not s or s["status"] != "done":
        return render_template("error.html", mensaje="Sesión no disponible.", suministro=suministro)
    datos = s["datos"]["consumos"]
    return render_template("datos_consumos.html", suministro=suministro,
                           filas=datos["filas"], html_raw=datos["html"])

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
