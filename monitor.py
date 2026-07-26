#!/usr/bin/env python3
"""
Monitor de https://www.pokemoncenter.com/category/tcg-cards

Detecta y avisa de:
  - Productos nuevos (SKU que nunca antes apareció en la categoría)
  - Restock (un producto conocido pasa de agotado a disponible)
  - Cambios de precio

Corre igual en el Mac y en un servidor (GitHub Actions).

--- Por qué está hecho así ---

Pokémon Center está protegido por Imperva/Incapsula + DataDome:

  * `curl`/`requests` reciben una página de bloqueo ("Pardon Our Interruption").
  * Un Chrome lanzado por Playwright (`p.chromium.launch`) también se detecta y
    se bloquea, incluso desde una IP residencial y con `headless=False`.
  * Lo único que pasa el filtro es un Chrome **lanzado como proceso normal** con
    `--remote-debugging-port`, al que nos conectamos por CDP.

Por eso este script nunca usa `launch()`: arranca Chrome él mismo y se conecta.
En Linux sin pantalla lo envuelve en `xvfb-run` en lugar de usar headless.

Los datos salen de dos sitios de cada página de categoría:
  * nombre, precio y URL → bloques <script type="application/ld+json">
  * stock                → badge "SOLD OUT" de la tarjeta en el DOM

El `availability` del ld+json NO sirve: en las páginas de categoría viene
siempre como OutOfStock, incluso para productos disponibles.
"""

import json
import os
import re
import shutil
import smtplib
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage

from playwright.sync_api import sync_playwright

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
ESTADO_PATH = os.path.join(BASE, "estado.json")
LOG_PATH = os.path.join(BASE, "monitor.log")

PERFIL = os.path.expanduser("~/.chrome-pokemon")
PUERTO = 9333
CDP = f"http://localhost:{PUERTO}"

URL_CATEGORIA = "https://www.pokemoncenter.com/category/tcg-cards"
# mismo listado ordenado por fecha de lanzamiento: lo recién salido va arriba
URL_NOVEDADES = URL_CATEGORIA + "?sort=launch_date%2Bdesc"
MAX_PAGINAS = 30  # tope de seguridad; el recorrido para cuando no hay más

# En modo vigilancia: cada cuánto el chequeo rápido y cada cuánto el completo
SEG_RAPIDO = 60
SEG_COMPLETO = 600

EN_SERVIDOR = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))


# --------------------------------------------------------------------------
# utilidades
# --------------------------------------------------------------------------

def log(msg):
    linea = f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC] {msg}"
    print(linea, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(linea + "\n")
    except OSError:
        pass


def cargar_config():
    """Config desde variables de entorno (servidor) o config.json (local)."""
    cfg = {"avisar_agotados": False, "notificaciones": {}}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    n = cfg.setdefault("notificaciones", {})

    env = os.environ.get
    if env("TELEGRAM_TOKEN") and env("TELEGRAM_CHAT_ID"):
        n["telegram"] = {"activo": True, "token": env("TELEGRAM_TOKEN"),
                         "chat_id": env("TELEGRAM_CHAT_ID")}
    if env("GMAIL_USER") and env("GMAIL_APP_PASSWORD"):
        n["email"] = {"activo": True, "usuario": env("GMAIL_USER"),
                      "password_app": env("GMAIL_APP_PASSWORD"),
                      "destino": env("EMAIL_DESTINO") or env("GMAIL_USER"),
                      "smtp": "smtp.gmail.com", "puerto": 465}
    if env("DISCORD_WEBHOOK"):
        n["discord"] = {"activo": True, "webhook": env("DISCORD_WEBHOOK")}
    if EN_SERVIDOR:
        n.pop("macos", None)  # no hay escritorio en el servidor
    return cfg


def cargar_estado():
    if os.path.exists(ESTADO_PATH):
        with open(ESTADO_PATH) as f:
            return json.load(f)
    return {"productos": {}, "primera_corrida": True}


def guardar_estado(estado):
    tmp = ESTADO_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(estado, f, indent=1, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, ESTADO_PATH)


# --------------------------------------------------------------------------
# navegador
# --------------------------------------------------------------------------

def ruta_chrome():
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if os.path.exists(mac):
        return mac
    for nombre in ("google-chrome", "google-chrome-stable", "chromium",
                   "chromium-browser"):
        ruta = shutil.which(nombre)
        if ruta:
            return ruta
    raise RuntimeError("No encuentro Chrome instalado")


def chrome_vivo():
    try:
        with urllib.request.urlopen(f"{CDP}/json/version", timeout=3):
            return True
    except Exception:
        return False


def asegurar_chrome(oculto=True):
    """Arranca Chrome como proceso normal con depuración remota.

    Nunca usar playwright.launch(): las flags de automatización que añade hacen
    que el anti-bot bloquee la sesión.
    """
    if chrome_vivo():
        return

    args = [
        ruta_chrome(),
        f"--user-data-dir={PERFIL}",
        "--profile-directory=Default",
        f"--remote-debugging-port={PUERTO}",
        "--no-first-run",
        "--no-default-browser-check",
        "--restore-last-session=false",
        "--window-size=1400,900",
    ]
    if oculto and not EN_SERVIDOR:
        # ventana fuera de la pantalla visible, en vez de headless (detectable)
        args.append("--window-position=-3000,-3000")

    if EN_SERVIDOR:
        args += ["--no-sandbox", "--disable-dev-shm-usage"]
        if not os.environ.get("DISPLAY") and shutil.which("xvfb-run"):
            # pantalla virtual: sigue siendo un Chrome completo, no headless
            args = ["xvfb-run", "-a", "--server-args=-screen 0 1400x900x24"] + args

    log("Arrancando Chrome con depuración remota…")
    with open("/tmp/chrome-pokemon.log", "a") as salida:
        subprocess.Popen(args, stdout=salida, stderr=salida)

    for _ in range(30):
        time.sleep(1)
        if chrome_vivo():
            log("Chrome listo.")
            return
    raise RuntimeError(f"Chrome no arrancó en el puerto {PUERTO} "
                       f"(ver /tmp/chrome-pokemon.log)")


RE_LD = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)

# Una entrada por tarjeta visible: sku, si tiene el badge SOLD OUT y si el
# propio sitio la marca como "New!".
JS_TARJETAS = """() => {
  const out = [];
  document.querySelectorAll('div[class*="product-box"]').forEach(box => {
    const a = box.querySelector('a[href*="/product/"]');
    if (!a) return;
    const m = (a.getAttribute('href') || '').match(/\\/product\\/([^/?#]+)/);
    if (!m) return;
    const txt = (box.innerText || '').toUpperCase();
    out.push({
      sku: m[1],
      agotado: txt.includes('SOLD OUT'),
      etiqueta_nuevo: txt.includes('NEW!'),
    });
  });
  return out;
}"""


def productos_de_pagina(page, html):
    """Combina el ld+json (nombre, precio, URL) con el DOM (stock real)."""
    fuera = {}
    for bloque in RE_LD.findall(html):
        try:
            d = json.loads(bloque)
        except json.JSONDecodeError:
            continue
        if d.get("@type") != "Product":
            continue
        sku = d.get("mpn")
        if not sku:
            continue
        oferta = d.get("offers") or {}
        fuera[sku] = {
            "nombre": d.get("name", "").strip(),
            "precio": oferta.get("price"),
            "moneda": oferta.get("priceCurrency", "USD"),
            "disponible": False,
            "etiqueta_nuevo": False,
            "url": oferta.get("url") or f"https://www.pokemoncenter.com/product/{sku}",
        }

    try:
        tarjetas = page.evaluate(JS_TARJETAS)
    except Exception as e:
        log(f"  no se pudo leer el stock del DOM: {e}")
        tarjetas = []

    for t in tarjetas:
        p = fuera.get(t["sku"])
        if p is None:
            continue
        p["disponible"] = not t["agotado"]
        p["etiqueta_nuevo"] = t["etiqueta_nuevo"]

    return fuera


def bloqueado(html):
    return ("_Incapsula_Resource" in html
            or "Pardon Our Interruption" in html
            or "Request unsuccessful" in html)


class Sesion:
    """Mantiene abierta una pestaña contra el Chrome con depuración remota.

    Se reutiliza entre revisiones: arrancar Chrome y abrir pestaña cuesta
    segundos que, en el modo vigilancia, se pagarían en cada ciclo.
    """

    def __init__(self, oculto=True):
        self.oculto = oculto
        self._pw = None
        self.page = None

    def __enter__(self):
        asegurar_chrome(oculto=self.oculto)
        self._pw = sync_playwright().start()
        navegador = self._pw.chromium.connect_over_cdp(CDP)
        ctx = navegador.contexts[0] if navegador.contexts else navegador.new_context()
        self.page = ctx.new_page()
        return self

    def __exit__(self, *_):
        try:
            self.page.close()
        except Exception:
            pass
        try:
            self._pw.stop()
        except Exception:
            pass

    def cargar(self, url, intentos=3):
        """Abre una URL y devuelve su HTML, reintentando si el WAF bloquea."""
        for i in range(intentos):
            self.page.goto(url, wait_until="domcontentloaded", timeout=90000)
            self.page.wait_for_timeout(6000)
            html = self.page.content()
            if not bloqueado(html):
                return html
            log(f"  bloqueo anti-bot, reintento {i + 1}/{intentos}")
            self.page.wait_for_timeout(10000)
        raise RuntimeError(f"El anti-bot bloqueó los {intentos} intentos en {url}")

    def novedades(self):
        """Primera página ordenada por fecha de lanzamiento: lo más nuevo arriba.

        Cuesta ~7 s frente a los ~4 min del barrido completo, así que permite
        vigilar productos nuevos casi en tiempo real.
        """
        html = self.cargar(URL_NOVEDADES)
        return productos_de_pagina(self.page, html)

    def barrido_completo(self):
        """Recorre todas las páginas de la categoría y devuelve {sku: datos}."""
        todos = {}
        for n in range(1, MAX_PAGINAS + 1):
            url = URL_CATEGORIA if n == 1 else f"{URL_CATEGORIA}?page={n}"
            pagina = productos_de_pagina(self.page, self.cargar(url))

            # Parar SOLO con una página vacía. Antes se paraba en cuanto una
            # página no aportaba SKUs nuevos, y una carga defectuosa que
            # repitiera resultados dejaba fuera todas las páginas siguientes.
            if not pagina:
                break

            nuevos = {k: v for k, v in pagina.items() if k not in todos}
            log(f"  página {n}: {len(pagina)} productos ({len(nuevos)} no vistos aún)")
            todos.update(nuevos)
        return todos


# --------------------------------------------------------------------------
# comparación
# --------------------------------------------------------------------------

def comparar(antes, ahora, avisar_agotados=False):
    nuevos, restock, precios, agotados = [], [], [], []
    for sku, act in ahora.items():
        viejo = antes.get(sku)
        if viejo is None:
            nuevos.append((sku, act))
            continue
        if act["disponible"] and not viejo.get("disponible"):
            restock.append((sku, act))
        elif avisar_agotados and not act["disponible"] and viejo.get("disponible"):
            agotados.append((sku, act))
        p_viejo, p_nuevo = viejo.get("precio"), act.get("precio")
        if p_viejo is not None and p_nuevo is not None and float(p_viejo) != float(p_nuevo):
            precios.append((sku, act, float(p_viejo)))
    return nuevos, restock, precios, agotados


def formatear(nuevos, restock, precios, agotados):
    """Devuelve (titulo_corto, cuerpo_texto, cuerpo_html)."""
    partes, html, resumen = [], [], []

    def bloque(titulo, emoji, items, linea_extra=None, singular=None):
        if not items:
            return
        etiqueta = singular if (len(items) == 1 and singular) else titulo.lower()
        resumen.append(f"{len(items)} {etiqueta}")
        partes.append(f"{emoji} {titulo.upper()}")
        html.append(f"<h3>{emoji} {titulo}</h3><ul>")
        for item in items:
            sku, p = item[0], item[1]
            precio = f"${p['precio']:.2f}" if p.get("precio") is not None else "s/precio"
            stock = "EN STOCK" if p["disponible"] else "agotado"
            extra = linea_extra(item) if linea_extra else ""
            partes.append(f"  • {p['nombre']}\n    {precio} · {stock}{extra}\n    {p['url']}")
            html.append(
                f"<li><a href=\"{p['url']}\"><b>{p['nombre']}</b></a><br>"
                f"{precio} · {stock}{extra} · <code>{sku}</code></li>"
            )
        html.append("</ul>")

    bloque("Productos nuevos", "🆕", nuevos, singular="producto nuevo")
    bloque("Restock", "✅", restock, singular="restock")
    bloque("Cambios de precio", "💲", precios,
           lambda it: f" (antes ${it[2]:.2f})", singular="cambio de precio")
    bloque("Se agotaron", "❌", agotados, singular="se agotó")

    return "Pokémon Center TCG: " + ", ".join(resumen), "\n".join(partes), "".join(html)


# --------------------------------------------------------------------------
# canales de aviso
# --------------------------------------------------------------------------

def avisar_macos(titulo, cuerpo, cfg):
    if not cfg.get("activo", True):
        return

    # ensure_ascii=False: con los escapes \uXXXX de los emoji, AppleScript falla
    def txt(s):
        return json.dumps(s, ensure_ascii=False)

    subtitulo = cuerpo.strip().split("\n")[0][:120] if cuerpo.strip() else ""
    script = (f'display notification {txt(subtitulo)} with title {txt(titulo[:120])} '
              f'sound name {txt(cfg.get("sonido", "Glass"))}')
    try:
        subprocess.run(["osascript", "-e", script], check=True, timeout=15)
        log("  aviso macOS enviado")
    except Exception as e:
        log(f"  macOS falló: {e}")


def avisar_telegram(titulo, cuerpo, cfg):
    token, chat = cfg.get("token"), cfg.get("chat_id")
    if not cfg.get("activo", True) or not token or not chat:
        return
    datos = urllib.parse.urlencode({
        "chat_id": chat,
        "text": f"*{titulo}*\n\n{cuerpo}"[:4000],
        "parse_mode": "Markdown",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=datos)
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        log("  aviso Telegram enviado")
    except Exception as e:
        log(f"  Telegram falló: {e}")


def avisar_discord(titulo, cuerpo, cfg):
    url = cfg.get("webhook")
    if not cfg.get("activo", True) or not url:
        return
    datos = json.dumps({"content": f"**{titulo}**\n```\n{cuerpo[:1700]}\n```"}).encode()
    try:
        req = urllib.request.Request(url, data=datos,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        log("  aviso Discord enviado")
    except Exception as e:
        log(f"  Discord falló: {e}")


def avisar_email(titulo, cuerpo, cuerpo_html, cfg):
    if not cfg.get("activo", True) or not cfg.get("usuario") or not cfg.get("password_app"):
        return
    msg = EmailMessage()
    msg["Subject"] = titulo[:160]
    msg["From"] = cfg["usuario"]
    msg["To"] = cfg.get("destino") or cfg["usuario"]
    msg.set_content(cuerpo)
    msg.add_alternative(
        f"<html><body style='font-family:-apple-system,sans-serif'>"
        f"<h2>{titulo}</h2>{cuerpo_html}"
        f"<p style='color:#888;font-size:12px'>Monitor de "
        f"pokemoncenter.com/category/tcg-cards</p></body></html>", subtype="html")
    try:
        with smtplib.SMTP_SSL(cfg.get("smtp", "smtp.gmail.com"),
                              cfg.get("puerto", 465), timeout=30) as s:
            s.login(cfg["usuario"], cfg["password_app"])
            s.send_message(msg)
        log("  aviso email enviado")
    except Exception as e:
        log(f"  email falló: {e}")


def notificar(titulo, cuerpo, cuerpo_html, config):
    canales = config.get("notificaciones", {})
    if "macos" in canales:
        avisar_macos(titulo, cuerpo, canales["macos"])
    avisar_telegram(titulo, cuerpo, canales.get("telegram", {}))
    avisar_discord(titulo, cuerpo, canales.get("discord", {}))
    avisar_email(titulo, cuerpo, cuerpo_html, canales.get("email", {}))


# --------------------------------------------------------------------------
# principal
# --------------------------------------------------------------------------

def main():
    config = cargar_config()

    if "--probar-avisos" in sys.argv:
        log("Enviando aviso de prueba por todos los canales configurados…")
        notificar("Pokémon Center TCG: prueba de avisos",
                  "🆕 PRODUCTOS NUEVOS\n  • Ejemplo de producto\n    $59.99 · EN STOCK\n"
                  "    https://www.pokemoncenter.com/category/tcg-cards",
                  "<h3>🆕 Prueba</h3><ul><li>Si ves esto, el canal funciona.</li></ul>",
                  config)
        return

def procesar(ahora, config, parcial=False):
    """Compara lo leído contra el estado guardado, avisa y guarda.

    `parcial=True` cuando solo se ha mirado la página de novedades: en ese caso
    los productos ausentes no significan nada, solo se aporta lo visto.
    """
    estado = cargar_estado()
    antes = estado.get("productos", {})

    if estado.get("primera_corrida"):
        if parcial:
            return False  # la línea base necesita el catálogo entero
        guardar_estado({"productos": ahora, "primera_corrida": False,
                        "ultima_revision": ahora_iso()})
        log(f"Primera corrida: línea base con {len(ahora)} productos guardada. "
            f"Desde la próxima revisión ya avisa de cambios.")
        return False

    nuevos, restock, precios, agotados = comparar(
        antes, ahora, avisar_agotados=config.get("avisar_agotados", False))

    hubo_cambios = bool(nuevos or restock or precios or agotados)
    if hubo_cambios:
        titulo, cuerpo, cuerpo_html = formatear(nuevos, restock, precios, agotados)
        log(titulo)
        log(cuerpo)
        notificar(titulo, cuerpo, cuerpo_html, config)

    # conserva los productos que desaparecieron de la categoría, para no
    # reportarlos como "nuevos" si el sitio los vuelve a listar
    fusionado = dict(antes)
    fusionado.update(ahora)
    estado["productos"] = fusionado
    estado["primera_corrida"] = False
    estado["ultima_revision"] = ahora_iso()
    guardar_estado(estado)
    return hubo_cambios


def ahora_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def revisar_una_vez(config, oculto=True):
    log("=== Revisando pokemoncenter.com/category/tcg-cards ===")
    with Sesion(oculto=oculto) as s:
        ahora = s.barrido_completo()
    if not ahora:
        log("No se extrajo ningún producto — posible bloqueo o cambio del sitio. "
            "Se conserva el estado anterior.")
        sys.exit(2)
    log(f"Total en la categoría: {len(ahora)} productos")
    if not procesar(ahora, config):
        log("Sin cambios.")


def vigilar(config, minutos, oculto=True):
    """Vigila en bucle durante `minutos`, sin dejar ventanas ciegas.

    Alterna dos ritmos: la página de novedades cada minuto (barata, detecta
    productos nuevos casi al instante) y el catálogo completo cada 10 minutos
    (necesario para restock y cambios de precio, que pueden estar en cualquier
    página).
    """
    fin = time.monotonic() + minutos * 60
    proximo_completo = 0.0
    ciclos = fallos = 0

    log(f"=== Vigilancia continua durante {minutos} min "
        f"(novedades cada {SEG_RAPIDO}s, catálogo completo cada {SEG_COMPLETO}s) ===")

    with Sesion(oculto=oculto) as s:
        while time.monotonic() < fin:
            arranque = time.monotonic()
            ciclos += 1
            try:
                if arranque >= proximo_completo:
                    ahora = s.barrido_completo()
                    proximo_completo = time.monotonic() + SEG_COMPLETO
                    log(f"catálogo completo: {len(ahora)} productos")
                    if ahora:
                        procesar(ahora, config)
                else:
                    ahora = s.novedades()
                    if ahora and procesar(ahora, config, parcial=True):
                        log("cambios detectados en la página de novedades")
                    elif ciclos % 10 == 0:
                        log(f"vigilando… ciclo {ciclos}, sin novedades")
                fallos = 0
            except Exception as e:
                fallos += 1
                log(f"ciclo con error ({fallos} seguidos): {str(e)[:200]}")
                if fallos >= 5:
                    raise RuntimeError(
                        f"5 ciclos seguidos fallando; el último: {e}") from e
                time.sleep(30)

            espera = SEG_RAPIDO - (time.monotonic() - arranque)
            if espera > 0 and time.monotonic() + espera < fin:
                time.sleep(espera)

    log(f"Vigilancia terminada: {ciclos} ciclos.")


def avisar_de_fallo(config, error):
    """Un fallo no puede quedar en silencio: sin aviso parece 'sin novedades'."""
    cuerpo = (f"El monitor falló y esta ronda no se ha revisado.\n\n{error}\n\n"
              f"Si se repite, mira los registros de la última ejecución en GitHub.")
    try:
        notificar("Pokémon Center TCG: ⚠️ el monitor falló", cuerpo,
                  f"<p>{cuerpo}</p>", config)
    except Exception as e:
        log(f"  además falló el aviso del fallo: {e}")


def main():
    config = cargar_config()
    oculto = "--ver-navegador" not in sys.argv

    if "--probar-avisos" in sys.argv:
        log("Enviando aviso de prueba por todos los canales configurados…")
        notificar("Pokémon Center TCG: prueba de avisos",
                  "🆕 PRODUCTOS NUEVOS\n  • Ejemplo de producto\n    $59.99 · EN STOCK\n"
                  "    https://www.pokemoncenter.com/category/tcg-cards",
                  "<h3>🆕 Prueba</h3><ul><li>Si ves esto, el canal funciona.</li></ul>",
                  config)
        return

    minutos = 0
    if "--vigilar" in sys.argv:
        i = sys.argv.index("--vigilar")
        minutos = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 55

    try:
        if minutos:
            vigilar(config, minutos, oculto=oculto)
        else:
            revisar_una_vez(config, oculto=oculto)
    except SystemExit:
        raise
    except Exception as e:
        log(f"FALLO: {e}")
        avisar_de_fallo(config, e)
        raise


if __name__ == "__main__":
    main()
