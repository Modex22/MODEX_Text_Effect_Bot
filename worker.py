
"""
MODEX Text Effect Bot — Photopea rendering worker.

Loads the custom Sequel font into Photopea, opens the PSD template,
replaces the intended text layer, and exports a PNG.

Railway environment variables:
    PSD_TEMPLATE
    FONT_FILE
    TEXT_LAYER_MATCH
    TEXT_FONT_POSTSCRIPT_NAME
    RENDER_TIMEOUT_MS
    PHOTOPEA_READY_TIMEOUT_MS
"""

import base64
import json
import logging
import os
import struct
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

ROOT = Path(__file__).resolve().parent

PSD_PATH = Path(
    os.getenv("PSD_TEMPLATE", str(ROOT / "assets" / "template.psd"))
)

FONT_PATH = Path(
    os.getenv("FONT_FILE", str(ROOT / "assets" / "Sequel100Black.ttf"))
)

LAYER_MATCH = os.getenv("TEXT_LAYER_MATCH", "BIGGESTCHAPOO")

# PostScript name inside your supplied Sequel font file.
FONT_POSTSCRIPT_NAME = os.getenv(
    "TEXT_FONT_POSTSCRIPT_NAME",
    "Sequel100Black-86",
)

TIMEOUT_MS = int(os.getenv("RENDER_TIMEOUT_MS", "90000"))
READY_TIMEOUT_MS = int(
    os.getenv("PHOTOPEA_READY_TIMEOUT_MS", "90000")
)

logger = logging.getLogger(__name__)

# Playwright intercepts this URL and serves the wrapper locally.
WRAPPER_URL = "https://example.com/modex-renderer.html"

WRAPPER_HTML = r"""<!doctype html>
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

      if (value instanceof ArrayBuffer || ArrayBuffer.isView(value)) {
        const bytes = value instanceof ArrayBuffer
          ? new Uint8Array(value)
          : new Uint8Array(
              value.buffer,
              value.byteOffset,
              value.byteLength
            );

        let binary = "";
        const chunkSize = 8192;

        for (let i = 0; i < bytes.length; i += chunkSize) {
          binary += String.fromCharCode(
            ...bytes.subarray(i, Math.min(i + chunkSize, bytes.length))
          );
        }

        window.ppMessages.push({
          kind: "binary",
          data: btoa(binary),
          time: Date.now()
        });

        console.log("Photopea binary response:", bytes.length, "bytes");

      } else if (typeof value === "string") {
        window.ppMessages.push({
          kind: "text",
          data: value,
          time: Date.now()
        });

        console.log("Photopea message:", value);

      } else {
        window.ppMessages.push({
          kind: "other",
          data: Object.prototype.toString.call(value),
          time: Date.now()
        });
      }
    });
  </script>

  <iframe id="photopea" src="__PHOTOPEA_URL__"></iframe>
</body>
</html>"""


async def _serve_wrapper(route, html: str) -> None:
    await route.fulfill(
        status=200,
        content_type="text/html; charset=utf-8",
        body=html,
    )


async def _get_messages(page):
    try:
        return await page.evaluate(
            "() => window.ppMessages || []"
        )
    except Exception:
        return []


async def _clear_messages(page) -> None:
    await page.evaluate("window.ppMessages = []")


async def _log_diagnostics(page, stage: str) -> None:
    logger.error("Photopea diagnostic stage: %s", stage)

    messages = await _get_messages(page)
    logger.error("Photopea messages: %s", messages[-20:])

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

    except Exception:
        logger.exception("Could not inspect browser frames")

    try:
        iframe_info = await page.locator(
            "#photopea"
        ).evaluate(
            """frame => ({
                src: frame.src,
                connected: frame.isConnected,
                hasContentWindow: !!frame.contentWindow
            })"""
        )

        logger.error("Photopea iframe: %s", iframe_info)

    except Exception:
        logger.exception("Could not inspect Photopea iframe")


async def _wait_for_done(page, timeout_ms: int, stage: str) -> None:
    """Wait for Photopea's documented completion message."""
    try:
        await page.wait_for_function(
            """() => window.ppMessages.some(
                m => m.kind === "text" && m.data === "done"
            )""",
            timeout=timeout_ms,
        )

    except PlaywrightTimeoutError as exc:
        await _log_diagnostics(page, stage)

        raise RuntimeError(
            f"Photopea did not send 'done' during {stage}. "
            "See the Railway diagnostics."
        ) from exc


async def _send_binary(page, path: Path) -> None:
    """Send a local binary file to Photopea through the iframe API."""
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")

    await page.evaluate(
        """encoded => {
            const raw = atob(encoded);
            const bytes = new Uint8Array(raw.length);

            for (let i = 0; i < raw.length; i++) {
                bytes[i] = raw.charCodeAt(i);
            }

            const frame = document.getElementById("photopea");

            if (!frame || !frame.contentWindow) {
                throw new Error("Photopea iframe is unavailable.");
            }

            frame.contentWindow.postMessage(bytes.buffer, "*");
        }""",
        encoded,
    )


async def _send_script(page, script: str) -> None:
    await page.evaluate(
        """scriptText => {
            const frame = document.getElementById("photopea");

            if (!frame || !frame.contentWindow) {
                throw new Error("Photopea iframe is unavailable.");
            }

            frame.contentWindow.postMessage(scriptText, "*");
        }""",
        script,
    )


def _make_render_script(text: str) -> str:
    """Find the desired text layer, apply the custom font, and export."""
    script = r"""
try {
    var wanted = __WANTED__;
    var replacement = __REPLACEMENT__;
    var fontName = __FONT_POSTSCRIPT_NAME__;

    if (!app.documents || app.documents.length === 0) {
        throw new Error("No PSD document is open.");
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
    var descriptions = [];

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

        descriptions.push(name + " [" + contents + "]");

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
            "Target text layer '" + wanted +
            "' not found. Editable layers: " +
            descriptions.join("; ")
        );
    }

    // Apply your loaded Sequel font explicitly.
    // Photoshop-compatible scripts use the font's PostScript name.
    target.textItem.font = fontName;
    target.textItem.contents = replacement;

    app.echoToOE("MODEX_TEXT_UPDATED");

    // Photopea sends the PNG binary back to the outer page.
    doc.saveToOE("png");

} catch (error) {
    app.echoToOE("MODEX_RENDER_ERROR:" + error.message);
}
"""

    return (
        script.replace("__WANTED__", json.dumps(LAYER_MATCH))
        .replace("__REPLACEMENT__", json.dumps(text))
        .replace(
            "__FONT_POSTSCRIPT_NAME__",
            json.dumps(FONT_POSTSCRIPT_NAME),
        )
    )


async def render_text_png(text: str) -> bytes:
    """Load the custom font and template, then render a transparent PNG."""

    if not PSD_PATH.is_file():
        raise FileNotFoundError(
            f"PSD template not found: {PSD_PATH}"
        )

    if not FONT_PATH.is_file():
        raise FileNotFoundError(
            f"Font file not found: {FONT_PATH}"
        )

    if not text or not text.strip():
        raise ValueError("Text cannot be empty.")

    logger.info("Starting Photopea render")
    logger.info("PSD path: %s", PSD_PATH)
    logger.info("PSD size: %s bytes", PSD_PATH.stat().st_size)
    logger.info("Font path: %s", FONT_PATH)
    logger.info("Font size: %s bytes", FONT_PATH.stat().st_size)
    logger.info("Font PostScript name: %s", FONT_POSTSCRIPT_NAME)
    logger.info("Text-layer marker: %s", LAYER_MATCH)

    photopea_config = {"environment": {}}

    photopea_url = (
        "https://www.photopea.com/#"
        + quote(
            json.dumps(photopea_config, separators=(",", ":")),
            safe="~()*!.'-",
        )
    )

    wrapper_html = WRAPPER_HTML.replace(
        "__PHOTOPEA_URL__",
        photopea_url,
    )

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

        page.on(
            "response",
            lambda response: logger.info(
                "HTTP %s %s",
                response.status,
                response.url,
            )
            if "photopea.com" in response.url else None,
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
                "Browser JavaScript error: %s",
                error,
            ),
        )

        page.on(
            "console",
            lambda message: logger.warning(
                "Browser console error: %s",
                message.text,
            )
            if message.type == "error" else None,
        )

        page.on(
            "framenavigated",
            lambda frame: logger.info(
                "Frame navigated: %s",
                frame.url,
            ),
        )

        try:
            async def serve(route):
                await _serve_wrapper(route, wrapper_html)

            await page.route(WRAPPER_URL, serve)

            await page.goto(
                WRAPPER_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )

            logger.info("MODEX wrapper loaded: %s", page.url)
            logger.info("Configured Photopea URL: %s", photopea_url)
            logger.info("Waiting for Photopea readiness")

            # 1. Wait for Photopea to initialize.
            await _wait_for_done(
                page,
                READY_TIMEOUT_MS,
                "initial Photopea startup",
            )

            logger.info("Photopea ready")

            # 2. Load the custom font BEFORE opening the PSD.
            await _clear_messages(page)

            await _send_binary(page, FONT_PATH)

            logger.info(
                "Custom font sent to Photopea: %s",
                FONT_PATH.name,
            )

            await _wait_for_done(
                page,
                TIMEOUT_MS,
                "custom font loading",
            )

            logger.info(
                "Photopea finished processing the custom font"
            )

            # 3. Open the PSD after the font is loaded.
            await _clear_messages(page)

            await _send_binary(page, PSD_PATH)

            logger.info("PSD sent to Photopea")

            await _wait_for_done(
                page,
                TIMEOUT_MS,
                "PSD loading",
            )

            logger.info("Photopea finished processing PSD")

            # 4. Replace the text and export PNG.
            await _clear_messages(page)

            await _send_script(
                page,
                _make_render_script(text),
            )

            logger.info("Text replacement script sent")

            try:
                await page.wait_for_function(
                    """() => window.ppMessages.some(m =>
                        m.kind === "binary" ||
                        (
                            m.kind === "text" &&
                            m.data.startsWith("MODEX_RENDER_ERROR:")
                        )
                    )""",
                    timeout=TIMEOUT_MS,
                )

            except PlaywrightTimeoutError as exc:
                await _log_diagnostics(page, "PNG export")

                raise RuntimeError(
                    "Photopea timed out during text replacement "
                    "or PNG export."
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
                    "Photopea returned no PNG binary data."
                )

            png = base64.b64decode(exports[-1])

            # Validate that Photopea returned a PNG.
            if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError(
                    "Photopea returned data that is not a PNG."
                )

            if len(png) < 24:
                raise RuntimeError("PNG export is incomplete.")

            width, height = struct.unpack(">II", png[16:24])

            if width <= 0 or height <= 0:
                raise RuntimeError(
                    "PNG dimensions are invalid."
                )

            logger.info(
                "PNG exported successfully: %sx%s, %s bytes",
                width,
                height,
                len(png),
            )

            return png

        except Exception:
            logger.exception("Photopea render failed")
            raise

        finally:
            await browser.close()
