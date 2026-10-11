
"""
MODEX Text Effect Bot — Photopea rendering worker.

Designed for Railway + Playwright Chromium.

Environment variables:
    PSD_TEMPLATE
    FONT_FILE
    TEXT_LAYER_MATCH
    RENDER_TIMEOUT_MS
    PHOTOPEA_READY_TIMEOUT_MS
"""

import base64
import json
import logging
import os
import struct
from pathlib import Path

from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

ROOT = Path(__file__).resolve().parent

PSD_PATH = Path(
    os.getenv(
        "PSD_TEMPLATE",
        str(ROOT / "assets" / "template.psd"),
    )
)

FONT_PATH = Path(
    os.getenv(
        "FONT_FILE",
        str(ROOT / "assets" / "Sequel100Black.ttf"),
    )
)

LAYER_MATCH = os.getenv(
    "TEXT_LAYER_MATCH",
    "BIGGESTCHAPOO",
)

TIMEOUT_MS = int(
    os.getenv("RENDER_TIMEOUT_MS", "90000")
)

READY_TIMEOUT_MS = int(
    os.getenv("PHOTOPEA_READY_TIMEOUT_MS", "90000")
)

logger = logging.getLogger(__name__)

# Playwright will serve the wrapper locally through route interception.
# The request is fulfilled by this worker, not by a real example.com site.
WRAPPER_URL = "https://example.com/modex-renderer.html"

WRAPPER_HTML = r"""
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
            const data = event.data;
            let description;

            if (typeof data === "string") {
                description = data;
            } else if (data instanceof ArrayBuffer) {
                description =
                    "ArrayBuffer (" + data.byteLength + " bytes)";
            } else {
                description = Object.prototype.toString.call(data);
            }

            console.log(
                "Photopea message from " + event.origin +
                ": " + description
            );

            if (data instanceof ArrayBuffer) {
                const bytes = new Uint8Array(data);
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
                            Math.min(i + chunkSize, bytes.length)
                        )
                    );
                }

                window.ppMessages.push({
                    kind: "binary",
                    data: btoa(binary),
                    time: Date.now()
                });

            } else if (ArrayBuffer.isView(data)) {
                const bytes = new Uint8Array(
                    data.buffer,
                    data.byteOffset,
                    data.byteLength
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
                            Math.min(i + chunkSize, bytes.length)
                        )
                    );
                }

                window.ppMessages.push({
                    kind: "binary",
                    data: btoa(binary),
                    time: Date.now()
                });

            } else if (typeof data === "string") {
                window.ppMessages.push({
                    kind: "text",
                    data: data,
                    time: Date.now()
                });

            } else {
                window.ppMessages.push({
                    kind: "other",
                    data: description,
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


async def _serve_wrapper(route):
    """Serve the embedding page through Playwright interception."""
    await route.fulfill(
        status=200,
        content_type="text/html; charset=utf-8",
        body=WRAPPER_HTML,
    )


async def _get_messages(page):
    try:
        return await page.evaluate(
            "() => window.ppMessages || []"
        )
    except Exception:
        return []


async def _clear_messages(page):
    await page.evaluate("window.ppMessages = []")


async def _send_to_photopea(page, payload):
    """Send a string script or binary ArrayBuffer to Photopea."""
    await page.evaluate(
        """
        (value) => {
            const frame = document.getElementById("photopea");

            if (!frame || !frame.contentWindow) {
                throw new Error("Photopea iframe is unavailable.");
            }

            frame.contentWindow.postMessage(value, "*");
        }
        """,
        payload,
    )


async def _wait_for_done(page, timeout_ms, stage):
    """Wait for a fresh Photopea 'done' message."""
    try:
        await page.wait_for_function(
            """
            () => window.ppMessages.some(
                message =>
                    message.kind === "text" &&
                    message.data === "done"
            )
            """,
            timeout=timeout_ms,
        )
    except PlaywrightTimeoutError as exc:
        await _log_diagnostics(page, stage)
        raise RuntimeError(
            f"Photopea did not send its 'done' message during: {stage}. "
            "Inspect the browser and network diagnostics in Railway logs."
        ) from exc


async def _log_diagnostics(page, stage):
    """Log enough state to identify browser and messaging failures."""
    logger.error("Photopea diagnostics for stage: %s", stage)

    messages = await _get_messages(page)
    logger.error("Photopea messages: %s", messages[-20:])

    try:
        frames = []

        for frame in page.frames:
            info = {"url": frame.url}

            try:
                info["title"] = await frame.title()
            except Exception as exc:
                info["title_error"] = str(exc)

            frames.append(info)

        logger.error("Browser frames: %s", frames)

    except Exception:
        logger.exception("Could not inspect browser frames.")

    try:
        info = await page.locator("#photopea").evaluate(
            """
            frame => ({
                src: frame.src,
                connected: frame.isConnected,
                hasContentWindow: !!frame.contentWindow
            })
            """
        )
        logger.error("Photopea iframe: %s", info)

    except Exception:
        logger.exception("Could not inspect Photopea iframe.")


async def _wait_for_export_or_error(page):
    """Wait for exported bytes or an explicit script error."""
    await page.wait_for_function(
        """
        () => window.ppMessages.some(message =>
            message.kind === "binary" ||
            (
                message.kind === "text" &&
                message.data.startsWith("MODEX_RENDER_ERROR:")
            )
        )
        """,
        timeout=TIMEOUT_MS,
    )


def _build_render_script(text):
    """Build the Photopea script that replaces the target text layer."""
    script = r"""
try {
    var wanted = __WANTED__;
    var replacement = __REPLACEMENT__;

    var doc = app.activeDocument;

    if (!doc) {
        throw new Error("No active document is open.");
    }

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
            "No editable text layers were found in the PSD."
        );
    }

    var target = null;
    var available = [];

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

        available.push(name + " [" + contents + "]");

        if (
            name.toUpperCase().indexOf(wanted.toUpperCase()) !== -1 ||
            contents.toUpperCase().indexOf(wanted.toUpperCase()) !== -1
        ) {
            target = candidate;
            break;
        }
    }

    if (!target) {
        throw new Error(
            "Could not find target layer '" + wanted +
            "'. Editable text layers: " + available.join("; ")
        );
    }

    target.textItem.contents = replacement;

    app.echoToOE("MODEX_TEXT_UPDATED");

    doc.saveToOE("png");

} catch (error) {
    app.echoToOE("MODEX_RENDER_ERROR:" + error.message);
}
"""

    return script.replace(
        "__WANTED__", json.dumps(LAYER_MATCH)
    ).replace(
        "__REPLACEMENT__", json.dumps(text)
    )


async def render_text_png(text: str) -> bytes:
    """Open the template in Photopea, replace text, and return PNG bytes."""

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

        # Capture browser errors and network activity in Railway logs.
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
                "Browser JavaScript error: %s", error
            ),
        )

        page.on(
            "console",
            lambda message: logger.warning(
                "Browser console error: %s", message.text
            )
            if message.type == "error"
            else None,
        )

        page.on(
            "framenavigated",
            lambda frame: logger.info(
                "Frame navigated: %s", frame.url
            ),
        )

        try:
            # Use a non-opaque HTTPS origin for the outer page.
            await page.route(
                WRAPPER_URL,
                _serve_wrapper,
            )

            await page.goto(
                WRAPPER_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )

            logger.info("MODEX wrapper loaded at %s", page.url)
            logger.info("Waiting for Photopea readiness.")

            # Step 1: Wait for Photopea to initialize.
            await _wait_for_done(
                page,
                READY_TIMEOUT_MS,
                "initial Photopea startup",
            )

            logger.info("Photopea ready.")

            # Step 2: Send PSD bytes.
            await _clear_messages(page)

            psd_bytes = PSD_PATH.read_bytes()
            psd_b64 = base64.b64encode(psd_bytes).decode("ascii")

            # Turn the PSD into an ArrayBuffer inside Chromium.
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
                psd_b64,
            )

            logger.info("PSD sent to Photopea.")

            # Photopea sends "done" after processing the opened file.
            await _wait_for_done(
                page,
                TIMEOUT_MS,
                "PSD loading",
            )

            logger.info("Photopea finished processing the PSD.")

            # Step 3: Replace text and export PNG.
            await _clear_messages(page)

            script = _build_render_script(text)

            await _send_to_photopea(page, script)

            logger.info("Rendering script sent to Photopea.")

            try:
                await _wait_for_export_or_error(page)

            except PlaywrightTimeoutError as exc:
                await _log_diagnostics(page, "PNG export")
                raise RuntimeError(
                    "Photopea timed out during text replacement or PNG export."
                ) from exc

            messages = await _get_messages(page)

            for message in messages:
                if message.get("kind") == "text":
                    logger.info(
                        "Photopea message: %s",
                        message.get("data"),
                    )

            errors = [
                message["data"]
                for message in messages
                if (
                    message.get("kind") == "text"
                    and message.get("data", "").startswith(
                        "MODEX_RENDER_ERROR:"
                    )
                )
            ]

            if errors:
                raise RuntimeError(errors[-1])

            exports = [
                message["data"]
                for message in messages
                if message.get("kind") == "binary"
            ]

            if not exports:
                raise RuntimeError(
                    "Photopea returned no binary export."
                )

            png = base64.b64decode(exports[-1])

            # Validate PNG signature and dimensions.
            if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError(
                    "Photopea returned data that is not a PNG."
                )

            if len(png) < 24:
                raise RuntimeError("PNG export appears incomplete.")

            width, height = struct.unpack(">II", png[16:24])

            if width <= 0 or height <= 0:
                raise RuntimeError("PNG dimensions are invalid.")

            logger.info(
                "PNG exported successfully: %sx%s, %s bytes.",
                width,
                height,
                len(png),
            )

            return png

        finally:
            await browser.close()
