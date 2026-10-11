# MODEX Text Effect Telegram Bot

A proof-of-concept Telegram bot that accepts text and attempts to render it using a supplied Photopea PSD template, returning a PNG.

## Important status

This is a starter MVP, not a verified production renderer. Photopea's scripting/message flow and the PSD's text-layer structure must be tested against your actual template. If the text layer is rasterized, replacement will not work until the PSD is adjusted to keep editable text.

The template and supplied Sequel font are included in `assets/`. Photopea may need the font installed/loaded in its session; simply bundling the font file does not automatically install it inside Photopea. If the template's text layer already preserves the style and the font is available, replacing the editable text should preserve the layer effects.

## Run locally

1. Create a Telegram bot using `@BotFather` and copy its token.
2. Install Docker, or install Python 3.12 and the packages in `requirements.txt`.
3. Copy `.env.example` to `.env` and set `BOT_TOKEN`. The app reads environment variables from the host; it does not automatically load `.env`, so export the variable in your shell or use a dotenv loader.
4. Run with Docker:

   ```bash
   docker build -t modex-text-effect .
   docker run --rm -e BOT_TOKEN="YOUR_TOKEN" modex-text-effect
   ```

## Deploy to Railway

1. Push this folder to a private GitHub repository.
2. Create a Railway service from that repository.
3. Railway should detect and build the included Dockerfile.
4. Add `BOT_TOKEN` under Variables.
5. Deploy and test by sending `/start`, then a short word.

Only run one polling instance for this bot token at a time. Stop any local instance before starting another.

## Current limitations to verify

- Photopea must load successfully in a headless Chromium session.
- The PSD needs an editable text layer. The worker first searches for a layer/text matching `TEXT_LAYER_MATCH`, then falls back to the first editable text layer.
- The script currently replaces the first matched text layer and exports the active document. Multi-line text, long-word fitting, and exact font availability are not yet handled.
- Exported PNG transparency depends on the PSD's background layer visibility. If the PSD has an opaque black background layer, that layer must be hidden in the template or the export won't be transparent.
- Browser automation can be memory-intensive on a small Railway instance. Test resource usage before making the bot public.
- Treat this as a prototype until the supplied PSD has been tested end-to-end.
