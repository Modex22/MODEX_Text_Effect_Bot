
"""
MODEX Text Effect Bot — Photopea rendering worker.

Loads the PSD template in Photopea, replaces the intended editable text
layer, exports a PNG, and provides detailed diagnostics for Railway.

Environment variables:
    PSD_TEMPLATE
    FONT_FILE
    TEXT_LAYER_MATCH
    RENDER_TIMEOUT_MS
    PHOTOPEA_READY_TIMEOUT_MS
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
READY_TIMEOUT_MS = int(
    os.getenv("PHOTOPEA_READY_TIMEOUT_MS", "90000")
)

PHOTOPEA_URL = "https://www.photopea.com/"

logger = logging.getLogger(__name__)


async def render_text_png(text: str) -> bytes:
    """Render the requested text and return PNG bytes."""

    if not PSD_PATH.is_file():
        raise FileNotFoundError(
            f"PSD template not found: {PSD_PATH}"
        )

    if not text or not text.strip():
        raise ValueError("Text cannot be empty.")

    logger.info("Starting Photopea render.")
    logger.info("PSD path: %s", PSD_PATH)
    logger.info("PSD size: %s bytes", PSD_PATH.stat().st_size)
    logger.info("Font file exists: %s", FONT_PATH.is_file())
    logger.info("Text-layer marker: %s", LAYER_MATCH)

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
            viewport={"width": 1400, "height": 1000}
        )

        # Network diagnostics for Railway.
        page.on(
            "response",
            lambda response: logger.info(
                "HTTP %s %s",
                response.status,
                response.url,
            )
            if "photopea.com" in response.url
            else None,
        )

        page.on(
            "requestfailed",
            lambda request: logger.warning(
                "Request failed: %s | %s",
                request.url,
                request.failure,
            ),
        )

        page.on(
            "pageerror",
            lambda error: logger.error(
                "Browser page error: %s", error
            ),
        )

        page.on(
            "console",
            lambda msg: logger.warning(
                "Browser console error: %s", msg.text
            )
            if msg.type == "error"
            else None,
        )

        page.on(
            "framenavigated",
            lambda frame: logger.info(
                "Frame navigated: %s", frame.url
            ),
        )

        try:
            # Register the outer-page listener BEFORE the iframe loads.
            # Photopea communicates using postMessage:
            # "done" = processing finished,
            # ArrayBuffer = exported file,
            # other strings = echoToOE messages.
            await page.set_content(
                """
                <!doctype html>
                <html>
                <head>
                    <meta charset="utf-8">
                    <title>MODEX Renderer</title>
                    <style>
                        html, body {
                            margin: 0;
                            padding: 0;
                            width: 100%;
                            height: 100%;
                            overflow: hidden;
                        }
                        #photopea {
                            display: block;
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
                            const value = event.data;

                            if (value instanceof ArrayBuffer) {
                                const bytes = new Uint8Array(value);
                                let binary = "";
                                const chunkSize = 8192;

                                for (
                                    let i = 0;
                                    i < bytes.length;
                                    i += chunkSize
                                ) {
                                    const part = bytes.subarray(
                                        i,
                                        Math.min(
                                            i + chunkSize,
                                            bytes.length
                                        )
                                    );

                                    binary += String.fromCharCode(
                                        ...part
                                    );
                                }

                                window.ppMessages.push({
                                    kind: "binary",
                                    data: btoa(binary),
                                    time: Date.now()
                                });
                            } else if (
                                ArrayBuffer.isView(value)
                            ) {
                                const bytes = new Uint8Array(
                                    value.buffer,
                                    value.byteOffset,
                                    value.byteLength
                                );

                                let binary = "";
                                const chunkSize = 8192;

                                for (
                                    let i = 0;
                                    i < bytes.length;
                                    i += chunkSize
                                ) {
                                    binary += String.fromCharCode(
                                        ...bytes.subarray(
                                            i,
                                            Math.min(
                                                i + chunkSize,
                                                bytes.length
                                            )
                                        )
                                    );
                                }

                                window.ppMessages.push({
                                    kind: "binary",
                                    data: btoa(binary),
                                    time: Date.now()
                                });
                            } else if (
                                typeof value === "string"
                            ) {
                                window.ppMessages.push({
                                    kind: "text",
                                    data: value,
                                    time: Date.now()
                                });
                            } else {
                                window.ppMessages.push({
                                    kind: "other",
                                    data: String(value),
                                    time: Date.now()
                                });
                            }
                        });
                    </script>

                    <iframe
                        id="photopea"
                        src="https://www.photopea.com/"
                    ></iframe>
                </body>
                </html>
                """
            )

            logger.info("Photopea iframe created.")
            logger.info("Waiting for Photopea readiness.")

            # Do not continue until Photopea sends its documented "done".
            try:
                await page.wait_for_function(
                    """
                    () => window.ppMessages.some(
                        message =>
                            message.kind === "text" &&
                            message.data === "done"
                    )
                    """,
                    timeout=READY_TIMEOUT_MS,
                )
            except PlaywrightTimeoutError as exc:
                await _log_diagnostics(page)
                raise RuntimeError(
                    "Photopea did not send its initial readiness "
                    "message. Check HTTP responses, failed requests, "
                    "and frame URLs in the Railway logs."
                ) from exc

            logger.info("Photopea ready.")

            # Send the PSD as binary data. Photopea should send "done"
            # once the file has been processed.
            await page.evaluate(
                """
                (encoded) => {
                    const raw = atob(encoded);
                    const bytes = new Uint8Array(raw.length);

                    for (let i = 0; i < raw.length; i++) {
                        bytes[i] = raw.charCodeAt(i);
                    }

                    document.getElementById("photopea")
                        .contentWindow.postMessage(
                            bytes.buffer,
                            "*"
                        );
                }
                """,
                base64.b64encode(
                    PSD_PATH.read_bytes()
                ).decode("ascii"),
            )

            logger.info("PSD sent to Photopea.")

            try:
                await page.wait_for_function(
                    """
                    () => window.ppMessages.some(
                        message =>
                            message.kind === "text" &&
                            message.data === "done"
                    )
                    """,
                    timeout=TIMEOUT_MS,
                )
            except PlaywrightTimeoutError as exc:
                await _log_diagnostics(page)
                raise RuntimeError(
                    "Photopea did not confirm PSD processing."
                ) from exc

            logger.info("Photopea finished processing the PSD.")

            # Clear old messages, preserving the event listener.
            await page.evaluate("window.ppMessages = []")

            # Match the marker by layer name OR original text contents.
            # Do not silently edit an unrelated text layer.
            script = r"""
            try {
                var wanted = __WANTED__;
                var replacement = __REPLACEMENT__;
                var doc = app.activeDocument;

                if (!doc) {
                    throw new Error("No active document.");
                }

                var textLayers = [];

                function collect(layers) {
                    for (var i = 0; i < layers.length; i++) {
                        var layer = layers[i];

                        try {
                            if (layer.layers) {
                                collect(layer.layers);
                            }
                        } catch (e) {}

                        try {
                            if (layer.textItem) {
                                textLayers.push(layer);
                            }
                        } catch (e) {}
                    }
                }

                collect(doc.layers);

                if (textLayers.length === 0) {
                    throw new Error(
                        "No editable text layers found in the PSD."
                    );
                }

                var target = null;
                var layerNames = [];

                for (var j = 0; j < textLayers.length; j++) {
                    var candidate = textLayers[j];
                    var name = "";
                    var contents = "";

                    try {
                        name = candidate.name || "";
                    } catch (e) {}

                    try {
                        contents =
                            candidate.textItem.contents || "";
                    } catch (e) {}

                    layerNames.push(
                        name + " [" + contents + "]"
                    );

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
                    throw new Error(
                        "Could not find text layer matching '" +
                        wanted + "'. Found: " +
                        layerNames.join("; ")
                    );
                }

                target.textItem.contents = replacement;

                app.echoToOE("MODEX_TEXT_UPDATED");

                // Photopea returns exported PNG bytes as an ArrayBuffer.
                doc.saveToOE("png");

            } catch (error) {
                app.echoToOE(
                    "MODEX_RENDER_ERROR:" + error.message
                );
            }
            """

            script = script.replace(
                "__WANTED__", json.dumps(LAYER_MATCH)
            ).replace(
                "__REPLACEMENT__", json.dumps(text)
            )

            logger.info("Sending text replacement script.")

            await page.evaluate(
                """
                (scriptText) => {
                    document.getElementById("photopea")
                        .contentWindow.postMessage(
                            scriptText,
                            "*"
                        );
                }
                """,
                script,
            )

            # Wait until Photopea returns PNG bytes or an explicit error.
            try:
                await page.wait_for_function(
                    """
                    () => window.ppMessages.some(message =>
                        message.kind === "binary" ||
                        (
                            message.kind === "text" &&
                            message.data.startsWith(
                                "MODEX_RENDER_ERROR:"
                            )
                        )
                    )
                    """,
                    timeout=TIMEOUT_MS,
                )
            except PlaywrightTimeoutError as exc:
                await _log_diagnostics(page)
                raise RuntimeError(
                    "Photopea timed out while updating the text "
                    "or exporting the PNG."
                ) from exc

            messages = await page.evaluate(
                "() => window.ppMessages"
            )

            for message in messages:
                if message["kind"] == "text":
                    logger.info(
                        "Photopea message: %s",
                        message["data"],
                    )

            errors = [
                message["data"]
                for message in messages
                if (
                    message["kind"] == "text"
                    and message["data"].startswith(
                        "MODEX_RENDER_ERROR:"
                    )
                )
            ]

            if errors:
                raise RuntimeError(errors[-1])

            exports = [
                message["data"]
                for message in messages
                if message["kind"] == "binary"
            ]

            if not exports:
                raise RuntimeError(
                    "Photopea returned no binary PNG data."
                )

            png = base64.b64decode(exports[-1])

            if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError(
                    "The exported data is not a valid PNG."
                )

            if len(png) < 24:
                raise RuntimeError("PNG export is incomplete.")

            width, height = struct.unpack(">II", png[16:24])

            if width <= 0 or height <= 0:
                raise RuntimeError(
                    "PNG export has invalid dimensions."
                )

            logger.info(
                "PNG exported successfully: %sx%s, %s bytes.",
                width,
                height,
                len(png),
            )

            return png

        finally:
            await browser.close()


async def _log_diagnostics(page):
    """Write browser and iframe information to the Railway logs."""

    try:
        messages = await page.evaluate(
            "() => window.ppMessages || []"
        )
        logger.error(
            "Photopea messages received: %s",
            messages[-20:],
        )
    except Exception as exc:
        logger.error("Could not inspect messages: %s", exc)

    try:
        frames = []

        for frame in page.frames:
            details = {"url": frame.url}

            try:
                details["title"] = await frame.title()
            except Exception as exc:
                details["title_error"] = str(exc)

            frames.append(details)

        logger.error("Browser frames: %s", frames)
    except Exception as exc:
        logger.error("Could not inspect browser frames: %s", exc)

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
    except Exception as exc:
        logger.error("Could not inspect Photopea iframe: %s", exc)
