
import asyncio
import base64
import json
import os
import struct
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parent
PSD_PATH = Path(os.getenv("PSD_TEMPLATE", ROOT / "assets" / "template.psd"))
FONT_PATH = Path(os.getenv("FONT_FILE", ROOT / "assets" / "Sequel100Black.ttf"))
LAYER_MATCH = os.getenv("TEXT_LAYER_MATCH", "BIGGESTCHAPOO")
TIMEOUT_MS = int(os.getenv("RENDER_TIMEOUT_MS", "90000"))


async def render_text_png(text: str) -> bytes:
    if not PSD_PATH.is_file():
        raise FileNotFoundError(f"PSD template not found: {PSD_PATH}")

    if not text or not text.strip():
        raise ValueError("Text cannot be empty.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = await browser.new_page(
            viewport={"width": 1400, "height": 1000}
        )

        try:
            # Install the message collector BEFORE Photopea loads.
            await page.set_content("""
                <!doctype html>
                <html>
                <body style="margin:0">
                    <iframe id="photopea"
                        src="https://www.photopea.com/"
                        style="width:100vw;height:100vh;border:0">
                    </iframe>
                    <script>
                        window.ppMessages = [];
                        window.addEventListener("message", (event) => {
                            const d = event.data;

                            if (d instanceof ArrayBuffer) {
                                const bytes = new Uint8Array(d);
                                let binary = "";
                                const chunk = 0x8000;

                                for (let i = 0; i < bytes.length; i += chunk) {
                                    binary += String.fromCharCode(
                                        ...bytes.subarray(i, i + chunk)
                                    );
                                }

                                window.ppMessages.push({
                                    kind: "binary",
                                    data: btoa(binary)
                                });
                            } else if (typeof d === "string") {
                                window.ppMessages.push({
                                    kind: "text",
                                    data: d
                                });
                            }
                        });
                    </script>
                </body>
                </html>
            """)

            # Photopea sends "done" when its scripting interface is ready.
            await page.wait_for_function(
                """() => window.ppMessages?.some(
                    m => m.kind === "text" && m.data === "done"
                )""",
                timeout=60000,
            )
            print("Photopea ready.")

            psd_b64 = base64.b64encode(
                PSD_PATH.read_bytes()
            ).decode("ascii")

            await page.evaluate(
                """(b64) => {
                    const raw = atob(b64);
                    const bytes = new Uint8Array(raw.length);

                    for (let i = 0; i < raw.length; i++) {
                        bytes[i] = raw.charCodeAt(i);
                    }

                    document.getElementById("photopea")
                        .contentWindow.postMessage(bytes.buffer, "*");
                }""",
                psd_b64,
            )
            print("PSD sent to Photopea.")

            # Allow the PSD to finish opening before running the script.
            await page.wait_for_timeout(4000)

            script = """
            try {
                var wanted = __WANTED__;
                var replacement = __REPLACEMENT__;
                var doc = app.activeDocument;

                if (!doc) {
                    throw new Error("No active document. PSD may not have opened.");
                }

                function collectTextLayers(layers, out) {
                    for (var i = 0; i < layers.length; i++) {
                        var layer = layers[i];

                        if (layer.layers) {
                            collectTextLayers(layer.layers, out);
                        }

                        try {
                            if (layer.textItem) {
                                out.push(layer);
                            }
                        } catch (e) {}
                    }
                }

                var candidates = [];
                collectTextLayers(doc.layers, candidates);

                if (candidates.length === 0) {
                    throw new Error("No editable text layers found in PSD.");
                }

                var target = null;

                for (var j = 0; j < candidates.length; j++) {
                    var layer = candidates[j];
                    var name = "";
                    var contents = "";

                    try { name = layer.name || ""; } catch (e) {}
                    try { contents = layer.textItem.contents || ""; } catch (e) {}

                    if (
                        name.toUpperCase().indexOf(wanted.toUpperCase()) >= 0 ||
                        contents.toUpperCase().indexOf(wanted.toUpperCase()) >= 0
                    ) {
                        target = layer;
                        break;
                    }
                }

                if (!target) {
                    throw new Error(
                        "Could not find the expected text layer: " + wanted
                    );
                }

                target.textItem.contents = replacement;

                // Return a diagnostic message before exporting.
                app.echoToOE("TEXT_UPDATED");

                // Photopea sends the exported binary to the embedding page.
                doc.saveToOE("png");
            } catch (e) {
                app.echoToOE("RENDER_ERROR:" + e.message);
            }
            """

            script = script.replace(
                "__WANTED__", json.dumps(LAYER_MATCH)
            ).replace(
                "__REPLACEMENT__", json.dumps(text)
            )

            # Clear old readiness messages so export diagnostics are easier
            # to inspect, without removing the event listener.
            await page.evaluate("window.ppMessages = []")

            await page.evaluate(
                """(script) => {
                    document.getElementById("photopea")
                        .contentWindow.postMessage(script, "*");
                }""",
                script,
            )
            print("Rendering script sent to Photopea.")

            await page.wait_for_function(
                """() => window.ppMessages?.some(
                    m => m.kind === "binary" ||
                         (m.kind === "text" &&
                          m.data.startsWith("RENDER_ERROR:"))
                )""",
                timeout=TIMEOUT_MS,
            )

            messages = await page.evaluate("window.ppMessages")
            errors = [
                m["data"] for m in messages
                if m["kind"] == "text"
                and m["data"].startswith("RENDER_ERROR:")
            ]

            if errors:
                raise RuntimeError(errors[-1])

            exports = [
                m["data"] for m in messages
                if m["kind"] == "binary"
            ]

            if not exports:
                raise RuntimeError(
                    "Photopea returned no PNG data. Messages: "
                    + repr(messages[-10:])
                )

            png = base64.b64decode(exports[-1])

            # Verify the PNG signature and IHDR dimensions.
            if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError(
                    "Photopea returned data that is not a valid PNG."
                )

            if len(png) < 24:
                raise RuntimeError("PNG export is incomplete.")

            width, height = struct.unpack(">II", png[16:24])

            if width <= 0 or height <= 0:
                raise RuntimeError("PNG has invalid dimensions.")

            print(
                f"PNG exported successfully: {width}x{height}, "
                f"{len(png)} bytes."
            )
            return png

        except PlaywrightTimeoutError as exc:
            messages = []
            try:
                messages = await page.evaluate(
                    "() => window.ppMessages || []"
                )
            except Exception:
                pass

            print("Photopea timeout. Recent messages:", messages[-10:])
            raise RuntimeError(
                "Photopea timed out waiting for readiness or PNG export. "
                "Check the preceding worker logs."
            ) from exc

        finally:
            await browser.close()
