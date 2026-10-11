"""
Photopea browser worker.

This proof-of-concept uses Photopea's embedded scripting interface. The PSD is
loaded into Photopea, the matching text layer is updated, and a PNG is exported.
It requires a working browser runtime (Playwright + Chromium) and internet access
to load Photopea.
"""
import asyncio
import base64
import os
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent
PSD_PATH = Path(os.getenv("PSD_TEMPLATE", ROOT / "assets" / "template.psd"))
FONT_PATH = Path(os.getenv("FONT_FILE", ROOT / "assets" / "Sequel100Black.ttf"))
LAYER_MATCH = os.getenv("TEXT_LAYER_MATCH", "BIGGESTCHAPOO")
TIMEOUT_MS = int(os.getenv("RENDER_TIMEOUT_MS", "90000"))

async def render_text_png(text: str) -> bytes:
    if not PSD_PATH.exists():
        raise FileNotFoundError(f"PSD template not found: {PSD_PATH}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 1400, "height": 1000})
        await page.set_content(
            '<!doctype html><html><body style="margin:0">'
            '<iframe id="photopea" src="https://www.photopea.com/" '
            'style="width:100vw;height:100vh;border:0"></iframe>'
            '</body></html>'
        )
        try:
            await page.wait_for_function(
                "() => { const f=document.getElementById('photopea'); return f && f.contentWindow; }",
                timeout=15000
            )
            # Wait for Photopea app to finish booting.
            await page.wait_for_timeout(9000)

            psd_b64 = base64.b64encode(PSD_PATH.read_bytes()).decode("ascii")
            await page.evaluate(
                """(b64) => {
                    const raw = atob(b64), bytes = new Uint8Array(raw.length);
                    for (let i=0; i<raw.length; i++) bytes[i] = raw.charCodeAt(i);
                    document.getElementById('photopea').contentWindow.postMessage(bytes.buffer, '*');
                }""",
                psd_b64
            )
            # Photopea needs time to parse the PSD.
            await page.wait_for_timeout(6000)

            # Photopea scripting: replace the text in the layer whose name or text
            # matches the configured marker, then export PNG bytes to the parent.
            # The output message is captured by the listener installed below.
            await page.evaluate(
                """() => {
                    window.__ppMessages = [];
                    window.addEventListener('message', (event) => {
                        const d = event.data;
                        if (d instanceof ArrayBuffer) {
                            let binary = '';
                            const bytes = new Uint8Array(d);
                            const chunk = 0x8000;
                            for (let i=0; i<bytes.length; i+=chunk)
                                binary += String.fromCharCode.apply(null, bytes.subarray(i, i+chunk));
                            window.__ppMessages.push({kind:'binary', data:btoa(binary)});
                        } else if (typeof d === 'string') {
                            window.__ppMessages.push({kind:'text', data:d});
                        }
                    });
                }"""
            )
            # The script is sent to Photopea as a message. Use a literal-safe JSON
            # string for user text and layer marker.
            import json
            script = f"""
var wanted = {json.dumps(LAYER_MATCH)};
var replacement = {json.dumps(text)};
function walk(layers, out) {{
  for (var i=0; i<layers.length; i++) {{
    var L = layers[i];
    if (L.layers) walk(L.layers, out);
    try {{
      if (L.textItem && ((L.name && L.name.toUpperCase().indexOf(wanted.toUpperCase()) >= 0) ||
          (L.textItem.contents && L.textItem.contents.toUpperCase().indexOf(wanted.toUpperCase()) >= 0))) {{
        out.push(L);
      }}
    }} catch(e) {{}}
  }}
}}
var found = [];
walk(app.activeDocument.layers, found);
if (found.length === 0) {{
  // Fallback: choose the first editable text layer.
  function allText(layers, out) {{
    for (var j=0; j<layers.length; j++) {{
      var X = layers[j];
      if (X.layers) allText(X.layers, out);
      try {{ if (X.textItem) out.push(X); }} catch(e) {{}}
    }}
  }}
  allText(app.activeDocument.layers, found);
}}
if (found.length === 0) throw new Error("No editable text layer found in PSD.");
found[0].textItem.contents = replacement;
app.activeDocument.saveToOE("png");
"""
            await page.evaluate(
                """(script) => {
                    document.getElementById('photopea').contentWindow.postMessage(script, '*');
                }""",
                script
            )

            # Wait for Photopea to post a binary export back.
            await page.wait_for_function(
                "() => window.__ppMessages && window.__ppMessages.some(m => m.kind === 'binary')",
                timeout=TIMEOUT_MS
            )
            b64 = await page.evaluate(
                "() => window.__ppMessages.find(m => m.kind === 'binary').data"
            )
            return base64.b64decode(b64)
        finally:
            await browser.close()
