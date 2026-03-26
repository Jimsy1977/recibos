import os
import time
import base64
import threading
import io
import re
import urllib3
import requests as req_lib
from bs4 import BeautifulSoup

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

# ── helpers ──────────────────────────────────────────────────────────────────

def log(sumi, msg):
    with sessions_lock:
        sessions[sumi].setdefault("log", []).append(msg)
    print(f"[{sumi}] {msg}", flush=True)

def make_driver():
    o = Options()
    o.add_argument("--headless=new")
    o.add_argument("--no-sandbox")
    o.add_argument("--disable-dev-shm-usage")
    o.add_argument("--disable-gpu")
    o.add_argument("--window-size=1920,1080")
    o.add_argument("--disable-popup-blocking")
    o.add_argument("--disable-blink-features=AutomationControlled")
    o.add_experimental_option("excludeSwitches", ["enable-automation"])
    o.add_experimental_option("useAutomationExtension", False)
    return webdriver.Chrome(options=o)

def make_requests_session(driver, referer=""):
    ua = driver.execute_script("return navigator.userAgent")
    s  = req_lib.Session()
    s.headers.update({"User-Agent": ua, "Referer": referer})
    for c in driver.get_cookies():
        s.cookies.set(c["name"], c["value"],
                      domain=c.get("domain", "").lstrip("."),
                      path=c.get("path", "/"))
    return s

# ── Extracción de datos ASP.NET ───────────────────────────────────────────────

def extraer_campos_hidden(html):
    soup = BeautifulSoup(html, "html.parser")
    def gv(name):
        el = soup.find("input", {"name": name})
        return el["value"] if el and el.get("value") else ""
    return {
        "__VIEWSTATE":          gv("__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": gv("__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION":    gv("__EVENTVALIDATION"),
    }

def extraer_eventtargets_recibos(html):
    """Extrae links 'Ver Recibo': soporta __doPostBack, window.open y URL directa."""
    soup    = BeautifulSoup(html, "html.parser")
    targets = []
    for a in soup.find_all("a"):
        texto  = (a.text or "").strip()
        if "recibo" not in texto.lower():
            continue
        onclick  = a.get("onclick", "") or ""
        href     = a.get("href",    "") or ""
        combined = onclick + href

        # Patrón 1: __doPostBack
        m = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", combined)
        if m:
            targets.append({"tipo": "postback", "target": m.group(1), "arg": m.group(2), "texto": texto})
            continue

        # Patrón 2: window.open('url')
        m2 = re.search(r"window\.open\(['\"]([^'\"]+)['\"]", combined)
        if m2:
            targets.append({"tipo": "open", "url": m2.group(1), "texto": texto})
            continue

        # Patrón 3: href directo (no javascript:)
        if href and not href.startswith("javascript") and (href.startswith("http") or href.startswith("/")):
            targets.append({"tipo": "get", "url": href, "texto": texto})

    return targets


def extraer_tabla(html):
    """Convierte la primera <table> del HTML en lista de dicts."""
    try:
        soup  = BeautifulSoup(html, "html.parser")
        tabla = soup.find("table")
        if not tabla:
            return []
        filas   = tabla.find_all("tr")
        if not filas:
            return []
        headers = [th.get_text(strip=True) for th in filas[0].find_all(["th", "td"])]
        resultado = []
        for fila in filas[1:]:
            celdas = [td.get_text(strip=True) for td in fila.find_all(["td", "th"])]
            if any(celdas):
                d = {headers[j] if j < len(headers) else f"Col{j}": v
                     for j, v in enumerate(celdas)}
                resultado.append(d)
        return resultado
    except Exception:
        return []

def descargar_pdf_postback(session, frame_url, vs, target, arg, sumi):
    """Descarga un PDF via ASP.NET postback."""
    try:
        r = session.post(frame_url, verify=False, timeout=12, data={
            "__EVENTTARGET":        target,
            "__EVENTARGUMENT":      arg,
            "__VIEWSTATE":          vs["__VIEWSTATE"],
            "__VIEWSTATEGENERATOR": vs["__VIEWSTATEGENERATOR"],
            "__EVENTVALIDATION":    vs["__EVENTVALIDATION"],
        })
        ct = r.headers.get("content-type", "").lower()
        log(sumi, f"  postback: {r.status_code} ct={ct} bytes={len(r.content)}")
        if r.content[:4] == b"%PDF" or "pdf" in ct:
            return r.content
        return None
    except Exception as ex:
        log(sumi, f"  postback error: {ex}")
        return None


def descargar_pdf_get(session, url, sumi):
    """Intenta descargar un PDF desde una URL directa."""
    from urllib.parse import urljoin
    try:
        r = session.get(urljoin(BASE_URL, url), verify=False, timeout=12)
        ct = r.headers.get("content-type", "").lower()
        log(sumi, f"  GET: {r.status_code} ct={ct} bytes={len(r.content)}")
        if r.content[:4] == b"%PDF" or "pdf" in ct:
            return r.content
        # Si es HTML, buscar PDF embebido
        soup_html = BeautifulSoup(r.text, "html.parser")
        for tg in soup_html.find_all(["embed", "object", "iframe"]):
            src = tg.get("src") or tg.get("data") or ""
            if src and ".pdf" in src.lower():
                r2 = session.get(urljoin(BASE_URL, src), verify=False, timeout=10)
                if r2.content[:4] == b"%PDF":
                    return r2.content
        return None
    except Exception as ex:
        log(sumi, f"  GET error: {ex}")
        return None

def navegar_a_seccion(driver, wait, texto_menu):
    """Clik en el menú y devuelve el HTML de frmMain."""
    try:
        driver.switch_to.default_content()
        wait.until(EC.frame_to_be_available_and_switch_to_it("frmMenu"))
        for a in driver.find_elements(By.TAG_NAME, "a"):
            if texto_menu.lower() in (a.text or "").lower():
                driver.execute_script("arguments[0].click();", a)
                break
        driver.switch_to.default_content()
        wait.until(EC.frame_to_be_available_and_switch_to_it("frmMain"))
        time.sleep(1)
        return driver.page_source
    except Exception as ex:
        return ""


# ── scraping principal ────────────────────────────────────────────────────────

def scrape_recibos(suministro):
    with sessions_lock:
        sessions[suministro] = {
            "status": "loading", "archivos": [], "error": None,
            "log": [],
            "datos": {
                "estado_cuenta": {"filas": [], "html": ""},
                "pagos":         {"filas": [], "html": ""},
                "consumos":      {"filas": [], "html": ""},
            }
        }

    driver = None
    t0 = time.time()

    try:
        log(suministro, "Iniciando navegador")
        driver = make_driver()
        wait   = WebDriverWait(driver, 15)

        # ── Login ─────────────────────────────────────────────────────────────
        driver.get(LOGIN_URL)
        wait.until(EC.presence_of_element_located((By.ID, "TxtContrato")))
        log(suministro, f"Login page cargada ({time.time()-t0:.1f}s)")

        driver.find_element(By.ID, "TxtContrato").send_keys(suministro)
        driver.find_element(By.ID, "TxtPassword").send_keys(suministro)
        driver.find_element(By.ID, "BotonOK").click()

        wait.until(EC.frame_to_be_available_and_switch_to_it("frmMenu"))
        log(suministro, f"Login OK ({time.time()-t0:.1f}s)")

        # ── Ir a Estado de Cuenta ─────────────────────────────────────────────
        for a in driver.find_elements(By.TAG_NAME, "a"):
            if "estado" in (a.text or "").lower():
                driver.execute_script("arguments[0].click();", a)
                break

        driver.switch_to.default_content()
        wait.until(EC.frame_to_be_available_and_switch_to_it("frmMain"))
        time.sleep(1)

        html_ec = driver.page_source
        # ← CLAVE: usar JS para obtener la URL REAL del frame (driver.current_url da el frameset padre)
        frame_url = driver.execute_script("return window.location.href")
        log(suministro, f"Estado Cuenta cargado ({time.time()-t0:.1f}s) frame_url={frame_url}")

        # Guardar datos de estado cuenta
        with sessions_lock:
            sessions[suministro]["datos"]["estado_cuenta"]["html"]  = html_ec
            sessions[suministro]["datos"]["estado_cuenta"]["filas"] = extraer_tabla(html_ec)

        # ── Transferir cookies a requests ─────────────────────────────────────
        req_s = make_requests_session(driver, referer=frame_url)

        # ── Extraer postbacks de recibos directamente del HTML ────────────────
        vs      = extraer_campos_hidden(html_ec)
        targets = extraer_eventtargets_recibos(html_ec)
        log(suministro, f"Recibos en HTML: {len(targets)} — targets={targets}")

        archivos = []

        if targets:
            # Descarga de PDFs según el tipo de link encontrado
            for t in targets[:6]:
                if time.time() - t0 > 70:
                    log(suministro, "Timeout 70s — deteniendo")
                    break
                pdf = None
                tipo = t.get("tipo", "postback")

                if tipo == "postback":
                    pdf = descargar_pdf_postback(req_s, frame_url, vs, t["target"], t["arg"], suministro)
                elif tipo in ("open", "get"):
                    pdf = descargar_pdf_get(req_s, t["url"], suministro)

                if pdf and len(pdf) > 500:
                    nombre = re.sub(r"[^a-zA-Z0-9_\-]", "_", t["texto"]) or f"Recibo_{len(archivos)+1}"
                    archivos.append({
                        "id":     len(archivos),
                        "nombre": f"{nombre}.pdf",
                        "base64": base64.b64encode(pdf).decode()
                    })
                    log(suministro, f"  ✓ {nombre}.pdf ({len(pdf)}B)")
                else:
                    log(suministro, f"  ✗ Sin PDF para '{t['texto']}' (tipo={tipo})")

        else:
            # No hay postbacks en HTML — buscar links directos en el DOM con Selenium
            log(suministro, "Sin postbacks en HTML — buscando en DOM")
            links_dom = [(a.text.strip(), a.get_attribute("href") or "", a.get_attribute("onclick") or "")
                         for a in driver.find_elements(By.TAG_NAME, "a")
                         if "recibo" in (a.text or "").lower()]
            log(suministro, f"Links DOM: {links_dom}")

            for texto, href, onclick in links_dom[:6]:
                if time.time() - t0 > 75:
                    break
                # Intentar postback desde el onclick del DOM
                m = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", onclick + href)
                if m:
                    pdf = descargar_pdf_postback(req_s, frame_url, vs, m.group(1), m.group(2), suministro)
                    if pdf and len(pdf) > 500:
                        nombre = re.sub(r"[^a-zA-Z0-9_\-]", "_", texto) or f"Recibo_{len(archivos)+1}"
                        archivos.append({
                            "id":     len(archivos),
                            "nombre": f"{nombre}.pdf",
                            "base64": base64.b64encode(pdf).decode()
                        })
                        log(suministro, f"  ✓ DOM {nombre}.pdf")
                elif href and href.startswith("http"):
                    try:
                        r = req_s.get(href, verify=False, timeout=10)
                        ct = r.headers.get("content-type","").lower()
                        if r.content[:4] == b"%PDF" or "pdf" in ct:
                            nombre = re.sub(r"[^a-zA-Z0-9_\-]", "_", texto) or f"Recibo_{len(archivos)+1}"
                            archivos.append({
                                "id": len(archivos), "nombre": f"{nombre}.pdf",
                                "base64": base64.b64encode(r.content).decode()
                            })
                            log(suministro, f"  ✓ GET {nombre}.pdf")
                    except Exception as ex:
                        log(suministro, f"  GET error: {ex}")

        # ── Secciones de datos (Sus Pagos, Sus Consumos) ─────────────────────
        if time.time() - t0 < 70:
            html_pag = navegar_a_seccion(driver, wait, "sus pagos")
            if html_pag:
                with sessions_lock:
                    sessions[suministro]["datos"]["pagos"]["html"]  = html_pag
                    sessions[suministro]["datos"]["pagos"]["filas"] = extraer_tabla(html_pag)
                log(suministro, f"Pagos: {len(sessions[suministro]['datos']['pagos']['filas'])} filas")

            html_con = navegar_a_seccion(driver, wait, "sus consumos")
            if html_con:
                with sessions_lock:
                    sessions[suministro]["datos"]["consumos"]["html"]  = html_con
                    sessions[suministro]["datos"]["consumos"]["filas"] = extraer_tabla(html_con)
                log(suministro, f"Consumos: {len(sessions[suministro]['datos']['consumos']['filas'])} filas")

        # ── Resultado ─────────────────────────────────────────────────────────
        log(suministro, f"Total: {len(archivos)} PDFs en {time.time()-t0:.1f}s")
        with sessions_lock:
            sessions[suministro]["archivos"] = archivos
            sessions[suministro]["status"]   = "done" if archivos else "empty"

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
    sumi = request.form.get("suministro", "").strip()
    if not sumi:
        return render_template("error.html", mensaje="Ingresa un número de suministro.", suministro="")
    threading.Thread(target=scrape_recibos, args=(sumi,), daemon=True).start()
    return render_template("loading.html", suministro=sumi)

@app.route("/estado/<suministro>")
def estado(suministro):
    with sessions_lock:
        s = sessions.get(suministro)
    if not s:
        return jsonify({"status": "not_found"})
    if s["status"] == "done":
        return jsonify({"status": "done", "total": len(s["archivos"]),
                        "archivos": [{"id": a["id"], "nombre": a["nombre"]} for a in s["archivos"]]})
    if s["status"] == "empty":
        return jsonify({"status": "empty"})
    if s["status"] == "error":
        return jsonify({"status": "error", "error": s.get("error", "")})
    return jsonify({"status": "loading"})

@app.route("/debug/<suministro>")
def debug(suministro):
    with sessions_lock:
        se = sessions.get(suministro, {})
    log_txt = "\n".join(se.get("log", []))
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Debug {suministro}</title>
    <style>body{{font-family:monospace;padding:20px;background:#111;color:#0f0}}</style></head><body>
    <h2>Estado: {se.get('status','N/A')}</h2>
    <h2>Log</h2><pre>{log_txt}</pre>
    </body></html>"""

@app.route("/error")
def error_page():
    return render_template("error.html",
                           mensaje=request.args.get("msg", "Error."),
                           suministro=request.args.get("suministro", ""))

@app.route("/recibos/<suministro>")
def recibos(suministro):
    with sessions_lock:
        s = sessions.get(suministro)
    if not s or s["status"] != "done":
        return render_template("error.html",
                               mensaje="Sesión expirada. Realiza una nueva consulta.",
                               suministro=suministro)
    archivos = [{"id": a["id"], "nombre": a["nombre"]} for a in s["archivos"]]
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
                           prev_idx=idx - 1 if idx > 0 else None,
                           next_idx=idx + 1 if idx < total - 1 else None)

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
    return jsonify({"base64": archivos[idx]["base64"], "nombre": archivos[idx]["nombre"]})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)