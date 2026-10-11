
"""
MODEX Text Effect Bot — Photopea rendering worker.

Runs Photopea in headless Chromium, opens the PSD template, replaces the
intended editable text layer, and exports PNG bytes.

Environment variables:
    PSD_TEMPLATE
    FONT_FILE
    TEXT_LAYER_MATCH
    RENDER_TIMEOUT_MS
"""

import asyncio
import base64
import json
import logging
import os
import struct
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parent

PSD_PATH = Path(
    os.getenv("PSD_TEMPLATE", str(ROOT / "assets" / "template.psd"))
)
FONT_PATH = Path(
    os.getenv("FONT_FILE", str(ROOT / "assets" / "Sequel100Black.ttf"))
)
LAYER_MATCH = os.getenv("TEXT_LAYER_MATCH", "BIGGESTCHAPOO")
TIMEOUT_MS = int(os.getenv("RENDER_TIMEOUT_MS", "90000"))

PHOTOPEA_URL = "https://www.photopea.com/"

logger = logging.getLogger(__name__)


async def render_text_png(text: str) -> bytes:
    """Render replacement text using the supplied PSD template."""

    if not PSD_PATH.is_file():
        raise FileNotFoundError(f"PSD template not found: {PSD_PATH}")

    if not text or not text.strip():
        raise ValueError("Please provide some text to render.")

    logger.info("Starting Photopea render.")
    logger.info("PSD path: %s", PSD_PATH)
    logger.info("PSD size: %s bytes", PSD_PATH.stat().st_size)
    logger.info("Configured text-layer marker: %s", LAYER_MATCH)
    logger.info("Font file exists: %s", FONT_PATH.is_file())

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        page = await browser.new_page(
            viewport={"width": 1400, "height": 1000},
            accept_downloads=False,
        )

        # Keep diagnostics useful in Railway logs.
        page.on(
            "console",
            lambda msg: logger.info(
                "Browser console [%s]: %s", msg.type, msg.text
            ) if msg.type == "error" else None,
        )
        page.on(
            "pageerror",
            lambda exc: logger.error("Browser page error: %s", exc),
        )
        page.on(
            "requestfailed",
            lambda req: logger.warning(
                "Browser request failed: %s (%s)",
                req.url,
                req.failure,
            ),
        )

        try:
            # Install the message listener BEFORE creating the iframe.
            # Photopea's API uses "done" as its readiness response,
            # app.echoToOE() for text diagnostics, and ArrayBuffer for exports.
            await page.set_content(
                """
                <!doctype html>
                <html>
                <head>
                    <meta charset="utf-8">
                    <title>MODEX Photopea Worker</title>
                    <style>
                        html, body {
                            margin: 0;
                            padding: 0;
                            width: 100%;
                            height: 100%;
                            overflow: hidden;
                        }
                        iframe {
                            width: 100%;
                            height: 100%;
                            border: 0;
                        }
                    </style>
                </head>
                <body>
                    <script>
                        window.ppMessages = [];

                        window.addEventListener("message", function(event) {
                            const data = event.data;

                            if (data instanceof ArrayBuffer) {
                                const bytes = new Uint8Array(data);
                                let binary = "";
                                const chunkSize = 0x8000;

                                for (
                                    let i = 0;
                                    i < bytes.length;
                                    i += chunkSize
                                ) {
                                    binary += String.fromCharCode(
                                        ...bytes.subarray(
                                            i,
                                            Math.min(i + chunkSize, bytes.length)
                                        )
                                    );
                                }

                                window.ppMessages.push({
                                    kind: "binary",
                                    data: btoa(binary),
                                    timestamp: Date.now()
                                });
                            } else if (typeof data === "string") {
                                window.ppMessages.push({
                                    kind: "text",
                                    data: data,
                                    timestamp: Date.now()
                                });
                            } else {
                                window.ppMessages.push({
                                    kind: "other",
                                    data: String(data),
                                    timestamp: Date.now()
                                });
                            }
                        });
                    </script>

                    <iframe
                        id="photopea"
                        src="https://www.photopea.com/"
                        allow="clipboard-read; clipboard-write"
                    ></iframe>
                </body>
                </html>
                """
            )

            logger.info("Photopea iframe created.")

            # Wait for Photopea's documented initialization message.
            logger.info("Waiting for Photopea readiness message.")

            await page.wait_for_function(
                """
                () => window.ppMessages.some(
                    m => m.kind === "text" && m.data === "done"
                )
                """,
                timeout=60000,
            )

            logger.info("Photopea ready.")

            title = await page.title()
            logger.info("Embedding page title: %s", title)

            # Load the PSD into Photopea as an ArrayBuffer.
            psd_b64 = base64.b64encode(
                PSD_PATH.read_bytes()
            ).decode("ascii")

            await page.evaluate(
                """
                (encoded) => {
                    const raw = atob(encoded);
                    const bytes = new Uint8Array(raw.length);

                    for (let i = 0; i < raw.length; i++) {
                        bytes[i] = raw.charCodeAt(i);
                    }

                    const frame = document.getElementById("photopea");

                    frame.contentWindow.postMessage(bytes.buffer, "*");
                }
                """,
                psd_b64,
            )

            logger.info("PSD sent to Photopea.")

            # Give Photopea time to parse the PSD. The rendering script
            # also checks that an active document and target layer exist.
            await page.wait_for_timeout(5000)

            # Remove old messages but keep the message listener active.
            await page.evaluate("window.ppMessages = []")

            script = r"""
            try {
                var wanted = __WANTED__;
                var replacement = __REPLACEMENT__;

                if (!app.documents || app.documents.length === 0) {
                    throw new Error(
                        "No document is open. Photopea may not have loaded the PSD."
                    );
                }

                var doc = app.activeDocument;
                var candidates = [];

                function collectTextLayers(layers) {
                    for (var i = 0; i < layers.length; i++) {
                        var layer = layers[i];

                        try {
                            if (layer.layers) {
                                collectTextLayers(layer.layers);
                            }
                        } catch (e) {}

                        try {
                            if (layer.textItem) {
                                candidates.push(layer);
                            }
                        } catch (e) {}
                    }
                }

                collectTextLayers(doc.layers);

                if (candidates.length === 0) {
                    throw new Error(
                        "No editable text layers found in the PSD."
                    );
                }

                var target = null;

                for (var j = 0; j < candidates.length; j++) {
                    var candidate = candidates[j];
                    var name = "";
                    var contents = "";

                    try {
                        name = candidate.name || "";
                    } catch (e) {}

                    try {
                        contents = candidate.textItem.contents || "";
                    } catch (e) {}

                    if (
                        name.toUpperCase().indexOf(
                            wanted.toUpperCase()
                        ) !== -1 ||
                        contents.toUpperCase().indexOf(
                            wanted.toUpperCase()
                        ) !== -1
                    ) {
                        target = candidate;
                        break;
                    }
                }

                if (!target) {
                    var names = [];

                    for (var k = 0; k < candidates.length; k++) {
                        try {
                            names.push(candidates[k].name);
                        } catch (e) {
                            names.push("(unnamed layer)");
                        }
                    }

                    throw new Error(
                        "Target layer not found: " + wanted +
                        ". Editable text layers: " + names.join(", ")
                    );
                }

                target.textItem.contents = replacement;

                app.echoToOE("MODEX_TEXT_UPDATED");

                // Export through Photopea's embedding interface.
                doc.saveToOE("png");

            } catch (error) {
                app.echoToOE("MODEX_RENDER_ERROR:" + error.message);
            }
            """

            script = script.replace(
                "__WANTED__", json.dumps(LAYER_MATCH)
            ).replace(
                "__REPLACEMENT__", json.dumps(text)
            )

            await page.evaluate(
                """
                (scriptText) => {
                    document.getElementById("photopea")
                        .contentWindow.postMessage(scriptText, "*");
                }
                """,
                script,
            )

            logger.info("Rendering script sent to Photopea.")

            # Wait for an export or an explicit script error.
            await page.wait_for_function(
                """
                () => window.ppMessages.some(m =>
                    m.kind === "binary" ||
                    (
                        m.kind === "text" &&
                        m.data.startsWith("MODEX_RENDER_ERROR:")
                    )
                )
                """,
                timeout=TIMEOUT_MS,
            )

            messages = await page.evaluate("window.ppMessages")

            for message in messages:
                if message["kind"] == "text":
                    logger.info(
                        "Photopea message: %s", message["data"]
                    )

            errors = [
                m["data"]
                for m in messages
                if m["kind"] == "text"
                and m["data"].startswith("MODEX_RENDER_ERROR:")
            ]

            if errors:
                raise RuntimeError(errors[-1])

            exports = [
                m["data"]
                for m in messages
                if m["kind"] == "binary"
            ]

            if not exports:
                raise RuntimeError(
                    "Photopea did not return PNG binary data."
                )

            png = base64.b64decode(exports[-1])

            # Validate PNG signature and dimensions.
            if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError(
                    "Export response was not a valid PNG."
                )

            if len(png) < 24:
                raise RuntimeError("PNG export is incomplete.")

            width, height = struct.unpack(">II", png[16:24])

            if width <= 0 or height <= 0:
                raise RuntimeError("PNG dimensions are invalid.")

            logger.info(
                "PNG exported successfully: %sx%s, %s bytes",
                width,
                height,
                len(png),
            )

            return png

        except PlaywrightTimeoutError as exc:
            try:
                messages = await page.evaluate(
                    "() => window.ppMessages || []"
                )
            except Exception:
                messages = []

            logger.error(
                "Photopea timeout. Recent messages: %s",
                messages[-15:],
            )

            try:
                iframe_info = await page.locator(
                    "#photopea"
                ).evaluate(
                    """
                    frame => ({
                        src: frame.src,
                        connected: frame.isConnected,
                        hasContentWindow: !!frame.contentWindow
                    })
                    """
                )
                logger.error("Photopea iframe info: %s", iframe_info)
            except Exception as diagnostic_error:
                logger.error(
                    "Could not inspect iframe: %s",
                    diagnostic_error,
                )

            raise RuntimeError(
                "Photopea timed out waiting for readiness or export. "
                "See the preceding Railway logs for browser diagnostics."
            ) from exc

        except Exception:
            logger.exception("Photopea render failed.")
            raise

        finally:
            await browser.close()
