import io
import os
import shutil
import tempfile
from pathlib import Path

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from PIL import Image

from fontTools.fontBuilder import FontBuilder
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables._g_l_y_f import Glyph
from fontTools.ttLib.tables.sbixGlyph import Glyph as SbixGlyph
from fontTools.ttLib.tables.sbixStrike import Strike


app = Flask(__name__)
CORS(app)

# Allow large alphabet uploads.
app.config["MAX_CONTENT_LENGTH"] = 250 * 1024 * 1024


@app.get("/")
def home():
    return jsonify({
        "status": "ok",
        "service": "Moonlight PNG Color Font Compiler"
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "healthy"
    })


def clean_font_name(name):
    cleaned = "".join(
        c for c in name
        if c.isalnum() or c in ("-", "_")
    )

    return cleaned or "MoonlightColorAlpha"


def make_empty_glyph():
    glyph = Glyph()
    glyph.numberOfContours = 0
    return glyph


def prepare_png(uploaded_file):
    """
    Normalize each uploaded glyph into a transparent RGBA PNG.

    The artwork itself is preserved. We only trim unused transparent
    space and place it on a predictable transparent canvas.
    """

    raw = uploaded_file.read()

    if not raw:
        raise ValueError(
            f"{uploaded_file.filename or 'Uploaded file'} is empty."
        )

    try:
        image = Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception as exc:
        raise ValueError(
            f"{uploaded_file.filename or 'Uploaded file'} "
            f"is not a valid PNG/image: {exc}"
        )

    alpha = image.getchannel("A")
    bbox = alpha.getbbox()

    if bbox:
        image = image.crop(bbox)

    # Keep the glyph reasonably sized inside the font.
    max_dimension = 900

    scale = min(
        max_dimension / max(image.width, 1),
        max_dimension / max(image.height, 1),
        1.0
    )

    if scale < 1:
        image = image.resize(
            (
                max(1, round(image.width * scale)),
                max(1, round(image.height * scale))
            ),
            Image.Resampling.LANCZOS
        )

    # Transparent padding prevents artwork from touching the edge.
    padding = 40

    canvas = Image.new(
        "RGBA",
        (
            image.width + padding * 2,
            image.height + padding * 2
        ),
        (0, 0, 0, 0)
    )

    canvas.alpha_composite(
        image,
        (padding, padding)
    )

    output = io.BytesIO()

    canvas.save(
        output,
        format="PNG",
        optimize=True
    )

    return output.getvalue(), canvas.width, canvas.height


@app.post("/compile")
def compile_font():
    workdir = None

    try:
        font_name = request.form.get(
            "font_name",
            "Moonlight Color Alpha"
        ).strip()

        if not font_name:
            font_name = "Moonlight Color Alpha"

        files = request.files.getlist("glyphs")
        characters = request.form.getlist("characters")

        if not files:
            return jsonify({
                "error": "No glyph PNG files were uploaded."
            }), 400

        if not characters:
            return jsonify({
                "error": "No character assignments were supplied."
            }), 400

        if len(files) != len(characters):
            return jsonify({
                "error":
                    f"Received {len(files)} images but "
                    f"{len(characters)} character assignments."
            }), 400

        workdir = Path(tempfile.mkdtemp())

        safe_name = clean_font_name(font_name)

        output_path = workdir / f"{safe_name}.ttf"

        glyph_order = [".notdef", "space"]

        character_map = {}
        png_glyphs = {}

        used_codepoints = set()

        for uploaded, character in zip(files, characters):

            character = (character or "").strip()

            if not character:
                continue

            char = character[0]
            codepoint = ord(char)

            # Avoid duplicate character mappings.
            if codepoint in used_codepoints:
                continue

            used_codepoints.add(codepoint)

            glyph_name = f"uni{codepoint:04X}"

            png_data, width, height = prepare_png(uploaded)

            glyph_order.append(glyph_name)

            character_map[codepoint] = glyph_name

            png_glyphs[glyph_name] = {
                "png": png_data,
                "width": width,
                "height": height
            }

        if not png_glyphs:
            return jsonify({
                "error": "No valid character PNGs were supplied."
            }), 400

        # ----------------------------------------------------------
        # Build a normal TrueType shell.
        # The visible artwork is stored in the sbix color table.
        # ----------------------------------------------------------

        units_per_em = 1000

        fb = FontBuilder(
            units_per_em,
            isTTF=True
        )

        fb.setupGlyphOrder(glyph_order)

        fb.setupCharacterMap(character_map)

        glyphs = {
            glyph_name: make_empty_glyph()
            for glyph_name in glyph_order
        }

        fb.setupGlyf(glyphs)

        metrics = {}

        metrics[".notdef"] = (1000, 0)
        metrics["space"] = (500, 0)

        for glyph_name, data in png_glyphs.items():

            # Width follows the original artwork proportions.
            advance_width = int(
                max(
                    300,
                    min(
                        1400,
                        1000 * data["width"] /
                        max(data["height"], 1)
                    )
                )
            )

            metrics[glyph_name] = (
                advance_width,
                0
            )

        fb.setupHorizontalMetrics(metrics)

        fb.setupHorizontalHeader(
            ascent=900,
            descent=-100
        )

        fb.setupNameTable({
            "familyName": font_name,
            "styleName": "Regular",
            "uniqueFontIdentifier":
                f"{font_name} Moonlight Color",
            "fullName": font_name,
            "psName": safe_name,
            "version": "Version 1.000"
        })

        fb.setupOS2(
            sTypoAscender=900,
            sTypoDescender=-100,
            usWinAscent=1000,
            usWinDescent=200
        )

        fb.setupPost()

        fb.setupMaxp()

        fb.save(output_path)

        # ----------------------------------------------------------
        # Add sbix PNG color glyph table.
        # ----------------------------------------------------------

        font = TTFont(output_path)

        sbix = newTable("sbix")

        sbix.version = 1
        sbix.flags = 1
        sbix.strikes = {}

        strike = Strike(
            ppem=128,
            resolution=72
        )

        strike.glyphs = {}

        # Required empty glyphs.
        strike.glyphs[".notdef"] = SbixGlyph(
            glyphName=".notdef"
        )

        strike.glyphs["space"] = SbixGlyph(
            glyphName="space"
        )

        for glyph_name, data in png_glyphs.items():

            glyph = SbixGlyph(
                glyphName=glyph_name,
                graphicType="png ",
                imageData=data["png"],
                originOffsetX=0,
                originOffsetY=0
            )

            strike.glyphs[glyph_name] = glyph

        sbix.strikes[128] = strike

        font["sbix"] = sbix

        font.save(output_path)

        # Verify that the resulting font can actually be reopened.
        test_font = TTFont(output_path)

        required_tables = {
            "cmap",
            "glyf",
            "head",
            "hhea",
            "hmtx",
            "maxp",
            "name",
            "OS/2",
            "post",
            "sbix"
        }

        missing = [
            table
            for table in required_tables
            if table not in test_font
        ]

        test_font.close()

        if missing:
            return jsonify({
                "error":
                    "Font was created but is missing tables: "
                    + ", ".join(missing)
            }), 500

        return send_file(
            output_path,
            mimetype="font/ttf",
            as_attachment=True,
            download_name=f"{safe_name}.ttf"
        )

    except Exception as exc:

        app.logger.exception(
            "Color font compilation failed"
        )

        return jsonify({
            "error": "Color font compilation failed.",
            "details": str(exc)
        }), 500

    finally:

        # send_file may still need the file while constructing the
        # response, so cleanup is intentionally skipped here.
        # Render's temporary filesystem handles these temporary files.
        pass


if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
