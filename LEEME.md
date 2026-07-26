# Monitor de Pokémon Center — TCG Cards

Vigila https://www.pokemoncenter.com/category/tcg-cards y avisa cuando hay:

- 🆕 **Producto nuevo** — un SKU que nunca antes había aparecido en la categoría
- ✅ **Restock** — un producto conocido pasa de agotado a disponible
- 💲 **Cambio de precio** — sube o baja el precio de algo que ya seguías

Avisa por **Telegram**, **email**, **Discord** y (si corre en el Mac) notificación
de macOS. Corre en **GitHub Actions**, así que no necesita tu computadora encendida.

---

## Puesta en marcha en el servidor

### 1. Credenciales → GitHub Secrets

En el repo: **Settings → Secrets and variables → Actions → New repository secret**.

| Secret | Para qué | Cómo se consigue |
|---|---|---|
| `TELEGRAM_TOKEN` | Avisos a tu móvil | Habla con [@BotFather](https://t.me/BotFather) → `/newbot` |
| `TELEGRAM_CHAT_ID` | Idem | Escríbele a tu bot y abre `https://api.telegram.org/bot<TOKEN>/getUpdates`: es el `"chat":{"id":…}` |
| `GMAIL_USER` | Remitente del correo | Tu dirección de Gmail |
| `GMAIL_APP_PASSWORD` | Idem | https://myaccount.google.com/apppasswords (necesita verificación en dos pasos) |
| `EMAIL_DESTINO` | Opcional | Si quieres recibirlo en otra dirección |
| `DISCORD_WEBHOOK` | Opcional | Ajustes del canal → Integraciones → Webhooks |

Los canales sin secret configurado simplemente se saltan.

### 2. Frecuencia

Está en `.github/workflows/monitor.yml`, en `cron`. Por defecto **cada 2 horas**.

Cada revisión tarda ~4 minutos. En un repo **privado** GitHub da 2.000 minutos
gratis al mes, así que cada 2 horas (~1.450 min/mes) es lo máximo cómodo. Si
haces el repo **público**, los minutos son ilimitados y puedes bajar a
`*/15 * * * *` (cada 15 minutos) — en el repo no hay nada sensible, las
credenciales viven en Secrets.

### 3. Primera corrida

La primera ejecución solo guarda la línea base (594 productos) y **no avisa de
nada**; si no, te llegarían 594 falsos "productos nuevos". Desde la segunda ya
avisa. Lánzala a mano desde la pestaña **Actions → Monitor Pokémon Center → Run
workflow**.

---

## Cómo funciona (y por qué así)

Pokémon Center está protegido por **Imperva/Incapsula + DataDome**. Comprobado
durante el desarrollo:

| Método | Resultado |
|---|---|
| `curl` / `fetch` desde tu Mac | ❌ página de bloqueo |
| `fetch` desde un servidor (Vercel) | ❌ "Pardon Our Interruption" |
| Chrome lanzado por Playwright (`launch()`), IP residencial | ❌ bloqueado |
| **Chrome normal con `--remote-debugging-port` + CDP** | ✅ funciona |

Por eso el script **nunca** usa `playwright.launch()`: arranca Chrome como un
proceso cualquiera y se conecta por CDP. En el servidor lo envuelve en `xvfb-run`
(pantalla virtual) en lugar de usar modo headless, que sí es detectable.

De cada página de categoría se extraen:

- **nombre, precio y URL** → bloques `<script type="application/ld+json">`
- **stock** → badge `SOLD OUT` de la tarjeta en el DOM

⚠️ El campo `availability` del `ld+json` **no sirve**: en las páginas de categoría
viene siempre como `OutOfStock`, incluso para productos disponibles. Verificado en
`/category/plush`, donde el `ld+json` marcaba las 32 tarjetas como agotadas
mientras el DOM mostraba correctamente solo 1.

Recorre `?page=1` … hasta que una página no aporta SKUs nuevos: hoy son 19
páginas y 594 productos.

---

## Uso local (opcional)

```bash
pip install playwright
cp config.example.json config.json     # y rellena lo que quieras usar
python3 monitor.py                     # una revisión
python3 monitor.py --ver-navegador     # con la ventana de Chrome a la vista
python3 monitor.py --probar-avisos     # manda un aviso de prueba por cada canal
```

`ACTIVAR_MONITOR.command` programa la revisión local cada 10 minutos con launchd
(y `DESACTIVAR_MONITOR.command` la quita). Solo tiene sentido si prefieres
correrlo en el Mac en vez de en el servidor.

## Archivos

| Archivo | Qué es |
|---|---|
| `monitor.py` | El bot (mismo código en local y en servidor) |
| `.github/workflows/monitor.yml` | La ejecución programada en GitHub Actions |
| `estado.json` | Snapshot del catálogo; es la memoria del bot, se commitea en cada corrida |
| `config.json` | Credenciales para uso local (ignorado por git) |
| `monitor.log` | Historial de revisiones y cambios detectados |

## Notas

- Si borras `estado.json`, la siguiente corrida vuelve a ser línea base.
- Los productos que desaparecen de la categoría se conservan en `estado.json`,
  para que no se reporten como "nuevos" si el sitio los vuelve a listar.
- Si un día empieza a fallar por bloqueo, el workflow sube `monitor.log` y el log
  de Chrome como artefacto de la ejecución fallida.
