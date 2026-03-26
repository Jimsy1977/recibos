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


# ── helper de log ─────────────────────────────────────────────────────────────
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


# ── configuración Chrome ──────────────────────────────────────────────────────
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
    # ← CLAVE: forzar descarga de PDF en lugar de renderizarlo en el browser
    o.add_experimental_option("prefs", {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,   # descarga PDF en vez de abrirlo
        "safebrowsing.enabled": True,
    })
    driver = webdriver.Chrome(options=o)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    })
    return driver


def wait_for_pdf(download_dir, known_before, timeout=35):
    """Espera hasta `timeout` segundos a que aparezca un PDF nuevo."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = set(glob.glob(os.path.join(download_dir, "*.pdf")))
        nuevos  = current - known_before
        # asegurarse de que no hay archivos parciales
        parciales = glob.glob(os.path.join(download_dir, "*.crdownload"))
        if nuevos and not parciales:
            return nuevos
        time.sleep(1)
    return set()


# ── scraping principal ────────────────────────────────────────────────────────
def scrape_recibos(suministro):
    download_dir = tempfile.mkdtemp(prefix=f"chavi_{suministro}_")
    with sessions_lock:
        sessions[suministro] = {
            "status": "loading", "archivos": [], "error": None,
            "log": [], "screenshots": [], "html_debug": {}
        }

    driver = None
    try:
        log(suministro, f"Chrome → download_dir={download_dir}")
        driver = make_driver(download_dir)
        wait  = WebDriverWait(driver, 25)

        # ── 1. Login ──────────────────────────────────────────────────────────
        log(suministro, "Abriendo portal...")
        driver.get(LOGIN_URL)
        wait.until(EC.presence_of_element_located((By.ID, "TxtContrato")))
        _shot(driver, suministro, "1_login")

        driver.find_element(By.ID, "TxtContrato").send_keys(suministro)
        driver.find_element(By.ID, "TxtPassword").send_keys(suministro)
        driver.find_element(By.ID, "BotonOK").click()
        time.sleep(7)

        if driver.find_elements(By.ID, "TxtContrato"):
            log(suministro, "Login fallido — seguimos en página de login")
            with sessions_lock:
                sessions[suministro]["status"] = "empty"
            return

        log(suministro, f"Login OK — url={driver.current_url}")
        _shot(driver, suministro, "2_post_login")

        # ── 2. Frame menú ─────────────────────────────────────────────────────
        frames = [f.get_attribute("name") or "?" for f in
                  driver.find_elements(By.TAG_NAME, "frame") +
                  driver.find_elements(By.TAG_NAME, "iframe")]
        log(suministro, f"Frames detectados: {frames}")

        try:
            wait.until(EC.frame_to_be_available_and_switch_to_it("frmMenu"))
        except Exception as e:
            log(suministro, f"frmMenu no disponible: {e}")
            with sessions_lock:
                sessions[suministro]["status"] = "empty"
            return

        _shot(driver, suministro, "3_frmMenu")
        links_menu = [(a.text.strip(), a.get_attribute("href"))
                      for a in driver.find_elements(By.TAG_NAME, "a")]
        log(suministro, f"Links menú: {links_menu}")

        # ── 3. Click Estado de Cuenta ─────────────────────────────────────────
        clicked = False
        for a in driver.find_elements(By.TAG_NAME, "a"):
            if "estado" in (a.text or "").lower():
                log(suministro, f"Click en '{a.text.strip()}'")
                driver.execute_script("arguments[0].click();", a)
                clicked = True
                break

        if not clicked:
            log(suministro, "No se encontró 'Estado de Cuenta' en el menú")
            with sessions_lock:
                sessions[suministro]["status"] = "empty"
            return

        driver.switch_to.default_content()
        time.sleep(6)

        # ── 4. Frame principal ────────────────────────────────────────────────
        try:
            wait.until(EC.frame_to_be_available_and_switch_to_it("frmMain"))
        except Exception as e:
            log(suministro, f"frmMain no disponible: {e}")
            with sessions_lock:
                sessions[suministro]["status"] = "empty"
            return

        # Espera activa hasta 20 s por links de recibo
        log(suministro, "Esperando links 'Ver Recibo' (máx 20s)...")
        for tick in range(20):
            ck = driver.find_elements(By.TAG_NAME, "a")
            if any("recibo" in (a.text or "").lower() for a in ck):
                log(suministro, f"Links encontrados en t={tick+1}s")
                break
            time.sleep(1)

        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1)
        except Exception:
            pass

        _shot(driver, suministro, "4_frmMain")
        main_html = driver.page_source
        with sessions_lock:
            sessions[suministro]["html_debug"]["frmMain"] = main_html[:6000]
        log(suministro, f"frmMain HTML (200): {main_html[:200]}")

        # ── 5. Recopilar links de recibo (Selenium + JS) ──────────────────────
        recibo_links = []
        seen_hrefs = set()

        # Selenium directo
        for a in driver.find_elements(By.TAG_NAME, "a"):
            t = (a.text or "").strip()
            h = a.get_attribute("href") or ""
            oc = a.get_attribute("onclick") or ""
            if "recibo" in t.lower() or "recibo" in h.lower():
                key = h or t
                if key not in seen_hrefs:
                    seen_hrefs.add(key)
                    recibo_links.append({"texto": t, "href": h, "onclick": oc})
                    log(suministro, f"  Link: '{t}' href='{h}' onclick='{oc[:80]}'")

        # JavaScript como fallback
        if not recibo_links:
            try:
                js_links = driver.execute_script("""
                    return Array.from(document.querySelectorAll('a')).map(a=>({
                        text: a.innerText.trim(), href: a.href||'',
                        onclick: a.getAttribute('onclick')||''
                    }));
                """)
                for lj in js_links:
                    if "recibo" in lj["text"].lower() or "recibo" in lj["href"].lower():
                        key = lj["href"] or lj["text"]
                        if key not in seen_hrefs:
                            seen_hrefs.add(key)
                            recibo_links.append(lj)
                            log(suministro, f"  JS Link: '{lj['text']}' href='{lj['href']}'")
            except Exception as ejs:
                log(suministro, f"JS links error: {ejs}")

        log(suministro, f"Total links recibo: {len(recibo_links)}")

        if not recibo_links:
            log(suministro, "Sin links de recibo — empty")
            # Guardar HTML completo para diagnóstico
            soup = BeautifulSoup(main_html, "html.parser")
            all_a = [(a.get_text(strip=True), a.get("href","")) for a in soup.find_all("a")]
            log(suministro, f"BS4 links: {all_a}")
            with sessions_lock:
                sessions[suministro]["status"] = "empty"
            return

        # ── 6. Obtener cookies para requests ──────────────────────────────────
        frame_url = driver.current_url
        user_agent = driver.execute_script("return navigator.userAgent")
        s = req_lib.Session()
        s.headers.update({
            "User-Agent": user_agent,
            "Referer": frame_url,
            "Accept": "application/pdf,*/*;q=0.8",
        })
        for c in driver.get_cookies():
            s.cookies.set(c["name"], c["value"],
                          domain=c.get("domain", "").lstrip("."),
                          path=c.get("path", "/"))

        soup_main = BeautifulSoup(main_html, "html.parser")
        def gv(name):
            el = soup_main.find("input", {"name": name})
            return el["value"] if el and el.get("value") else ""
        vs = gv("__VIEWSTATE"); vsg = gv("__VIEWSTATEGENERATOR"); ev = gv("__EVENTVALIDATION")

        # ── 7. Descargar cada recibo ──────────────────────────────────────────
        archivos_data = []

        for i, info in enumerate(recibo_links):
            href = info["href"]; texto = info["texto"]; onclick = info.get("onclick","")
            pdf_bytes = None
            log(suministro, f"--- Recibo {i+1}: '{texto}' ---")

            # Estrategia A: URL directa con requests
            if href and not href.startswith("javascript") and href.startswith("http"):
                try:
                    log(suministro, f"  A: GET {href}")
                    r = s.get(href, verify=False, timeout=30)
                    ct = r.headers.get("content-type","").lower()
                    log(suministro, f"  A: status={r.status_code} ct={ct} bytes={len(r.content)} magic={r.content[:8]}")
                    if r.content[:4] == b"%PDF" or "pdf" in ct:
                        pdf_bytes = r.content
                        log(suministro, "  A ✓ PDF directo")
                except Exception as ex:
                    log(suministro, f"  A ✗ {ex}")

            # Estrategia B: ASP.NET postback con requests
            if not pdf_bytes:
                m = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", onclick + href)
                if m:
                    try:
                        log(suministro, f"  B: postback et='{m.group(1)}'")
                        r = s.post(frame_url, verify=False, timeout=30, data={
                            "__EVENTTARGET": m.group(1), "__EVENTARGUMENT": m.group(2),
                            "__VIEWSTATE": vs, "__VIEWSTATEGENERATOR": vsg, "__EVENTVALIDATION": ev,
                        })
                        ct = r.headers.get("content-type","").lower()
                        log(suministro, f"  B: status={r.status_code} ct={ct} bytes={len(r.content)} magic={r.content[:8]}")
                        if r.content[:4] == b"%PDF" or "pdf" in ct:
                            pdf_bytes = r.content
                            log(suministro, "  B ✓ PDF postback")
                    except Exception as ex:
                        log(suministro, f"  B ✗ {ex}")

            # Estrategia C: Selenium click → esperar PDF en download_dir
            if not pdf_bytes:
                try:
                    log(suministro, "  C: click → esperar descarga en disco")
                    pdfs_antes = set(glob.glob(os.path.join(download_dir, "*.pdf")))
                    fresh = [a for a in driver.find_elements(By.TAG_NAME, "a")
                             if "recibo" in (a.text or "").lower()]
                    if i < len(fresh):
                        ventanas_antes = set(driver.window_handles)
                        driver.execute_script("arguments[0].click();", fresh[i])
                        log(suministro, "  C: click realizado, esperando PDF (35s)...")

                        # Esperar PDF nuevo en disco
                        nuevos_pdf = wait_for_pdf(download_dir, pdfs_antes, timeout=35)
                        log(suministro, f"  C: PDFs nuevos en disco={nuevos_pdf}")

                        # Si se descargó a disco ← éxito
                        if nuevos_pdf:
                            path = sorted(nuevos_pdf)[0]
                            with open(path, "rb") as f:
                                pdf_bytes = f.read()
                            log(suministro, f"  C ✓ PDF desde disco: {path} ({len(pdf_bytes)} bytes)")

                        else:
                            # Si no hay PDF en disco, analizar la nueva pestaña
                            ventanas_nuevas = set(driver.window_handles) - ventanas_antes
                            log(suministro, f"  C: Nuevas pestañas={len(ventanas_nuevas)}")
                            for v in ventanas_nuevas:
                                driver.switch_to.window(v)
                                time.sleep(3)
                                tab_url = driver.current_url
                                log(suministro, f"  C: tab URL={tab_url}")
                                _shot(driver, suministro, f"5_tab_{i}")
                                tab_html = driver.page_source
                                with sessions_lock:
                                    sessions[suministro]["html_debug"][f"tab_{i}"] = tab_html[:6000]
                                log(suministro, f"  C: tab HTML(200)={tab_html[:200]}")

                                # Intentar descargar la URL de la pestaña
                                if tab_url and tab_url not in ("about:blank", ""):
                                    try:
                                        r = s.get(tab_url, verify=False, timeout=30)
                                        ct = r.headers.get("content-type","").lower()
                                        log(suministro, f"  C: req tab_url status={r.status_code} ct={ct} bytes={len(r.content)}")
                                        if r.content[:4] == b"%PDF" or "pdf" in ct:
                                            pdf_bytes = r.content
                                            log(suministro, "  C ✓ PDF de tab_url con requests")
                                    except Exception as ex:
                                        log(suministro, f"  C: req tab_url error: {ex}")

                                # Buscar PDF en HTML de la pestaña (embed/iframe/JS)
                                if not pdf_bytes and tab_html:
                                    ts = BeautifulSoup(tab_html, "html.parser")
                                    pdf_candidates = []
                                    for tg in ts.find_all(["embed","object"]):
                                        src = tg.get("src") or tg.get("data","")
                                        if src: pdf_candidates.append(urljoin(tab_url, src))
                                    for tg in ts.find_all("iframe"):
                                        src = tg.get("src","")
                                        if src and "blank" not in src: pdf_candidates.append(urljoin(tab_url, src))
                                    for j in re.findall(r'["\']([^"\']+\.pdf[^"\']*)["\']', tab_html, re.I):
                                        pdf_candidates.append(urljoin(tab_url, j))
                                    b64m = re.search(r'data:application/pdf;base64,([A-Za-z0-9+/=]+)', tab_html)
                                    if b64m:
                                        try:
                                            pdf_bytes = base64.b64decode(b64m.group(1))
                                            log(suministro, f"  C ✓ PDF de data-URI ({len(pdf_bytes)} bytes)")
                                        except Exception: pass
                                    log(suministro, f"  C: candidatos HTML={pdf_candidates}")
                                    for purl in pdf_candidates:
                                        if pdf_bytes: break
                                        try:
                                            r = s.get(purl, verify=False, timeout=30)
                                            if r.content[:4] == b"%PDF" or "pdf" in r.headers.get("content-type","").lower():
                                                pdf_bytes = r.content
                                                log(suministro, f"  C ✓ PDF candidato {purl}")
                                        except Exception: pass

                                # Último recurso: CDP printToPDF sobre la pestaña
                                if not pdf_bytes:
                                    try:
                                        result = driver.execute_cdp_cmd("Page.printToPDF", {
                                            "printBackground": True,
                                            "preferCSSPageSize": True,
                                            "marginTop": 0, "marginBottom": 0,
                                            "marginLeft": 0, "marginRight": 0,
                                        })
                                        data = base64.b64decode(result.get("data",""))
                                        if len(data) > 500:
                                            pdf_bytes = data
                                            log(suministro, f"  C ✓ PDF de printToPDF ({len(pdf_bytes)} bytes)")
                                    except Exception as ex:
                                        log(suministro, f"  C: printToPDF error: {ex}")

                                driver.close()

                        # Volver a ventana/frame principal
                        driver.switch_to.window(driver.window_handles[0])
                        try: driver.switch_to.frame("frmMain")
                        except Exception: pass

                except Exception as ex:
                    log(suministro, f"  C ✗ {ex}")
                    try: driver.switch_to.window(driver.window_handles[0])
                    except Exception: pass

            # Guardar resultado
            if pdf_bytes and len(pdf_bytes) > 500:
                nombre = re.sub(r"[^a-zA-Z0-9_\-]", "_", texto) or f"Recibo_{i+1:02d}"
                b64 = base64.b64encode(pdf_bytes).decode()
                archivos_data.append({"id": len(archivos_data), "nombre": f"{nombre}.pdf", "base64": b64})
                log(suministro, f"  GUARDADO: {nombre}.pdf ({len(pdf_bytes)} bytes)")
            else:
                log(suministro, f"  SIN PDF para recibo {i+1}")

        # ── Resultado final ───────────────────────────────────────────────────
        if archivos_data:
            log(suministro, f"ÉXITO: {len(archivos_data)} recibo(s)")
            with sessions_lock:
                sessions[suministro]["archivos"] = archivos_data
                sessions[suministro]["status"] = "done"
        else:
            log(suministro, "Sin PDFs al final")
            with sessions_lock:
                sessions[suministro]["status"] = "empty"

    except Exception as e:
        import traceback
        log(suministro, f"EXCEPCIÓN: {e}\n{traceback.format_exc()}")
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
    dbg_html  = "".join(f'<h3>{k}</h3><pre style="background:#111;color:#0f0;padding:12px;overflow:auto;max-height:350px">{v}</pre>'
                         for k,v in se.get("html_debug",{}).items())
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Debug {suministro}</title>
    <style>body{{font-family:monospace;padding:20px}}pre{{background:#111;color:#0f0;padding:10px}}</style></head><body>
    <h1>Debug: {suministro}</h1><h2>Estado: {se.get('status','N/A')}</h2>
    <h2>Log</h2><pre>{log_html}</pre>
    <h2>Screenshots</h2>{shots_html}
    <h2>HTML capturado</h2>{dbg_html}
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
    return jsonify({"base64": archivos[idx]["base64"], "nombre": archivos[idx]["nombre"]})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
