import io
import os
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

app.config["MAX_CONTENT_LENGTH"] = 250 * 1024 * 1024


@app.get("/")
def home():
    return jsonify({
        "status": "ok",
        "service": "Moonlight PNG Color Font Compiler"
    })


@app.get("/health")
def health():
    return jsonify({"status": "healthy"})


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
    Prepare one PNG for the sbix font.

    Every glyph is normalized to the SAME visual height.
    Its original aspect ratio is preserved.
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
            f"is not a valid image: {exc}"
        )

    # Trim only transparent empty area.
    alpha = image.getchannel("A")
    bbox = alpha.getbbox()

    if bbox:
        image = image.crop(bbox)

    if image.width <= 0 or image.height <= 0:
        raise ValueError("A glyph image contained no visible artwork.")

    # ---------------------------------------------------------
    # IMPORTANT:
    # Every letter gets the same target artwork height.
    # This prevents A, B, C, etc. from randomly changing size.
    # ---------------------------------------------------------

    target_height = 900

    scale = target_height / image.height

    new_width = max(
        1,
        round(image.width * scale)
    )

    new_height = target_height

    image = image.resize(
        (new_width, new_height),
        Image.Resampling.LANCZOS
    )

    # Small, consistent transparent side bearing.
    side_padding = 55

    canvas_width = new_width + (side_padding * 2)
    canvas_height = 1000

    canvas = Image.new(
        "RGBA",
        (canvas_width, canvas_height),
        (0, 0, 0, 0)
    )

    # Put every glyph on the SAME baseline.
    #
    # 50 px top margin
    # 900 px artwork
    # 50 px bottom margin

    x = side_padding
    y = 50

    canvas.alpha_composite(
        image,
        (x, y)
    )

    output = io.BytesIO()

    canvas.save(
        output,
        format="PNG",
        optimize=True
    )

    return {
        "png": output.getvalue(),
        "canvas_width": canvas_width,
        "canvas_height": canvas_height,
        "art_width": new_width,
        "art_height": new_height
    }


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

        glyph_order = [
            ".notdef",
            "space"
        ]

        character_map = {}
        png_glyphs = {}
        used_codepoints = set()

        for uploaded, character in zip(
            files,
            characters
        ):

            character = (character or "").strip()

            if not character:
                continue

            char = character[0]
            codepoint = ord(char)

            if codepoint in used_codepoints:
                continue

            used_codepoints.add(codepoint)

            glyph_name = f"uni{codepoint:04X}"

            prepared = prepare_png(uploaded)

            glyph_order.append(glyph_name)

            character_map[codepoint] = glyph_name

            png_glyphs[glyph_name] = prepared

        if not png_glyphs:
            return jsonify({
                "error": "No valid character PNGs were supplied."
            }), 400

        # =========================================================
        # TRUE TYPE FONT SHELL
        # =========================================================

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

        # =========================================================
        # SPACING
        # =========================================================

        metrics = {
            ".notdef": (1000, 0),
            "space": (450, 0)
        }

        for glyph_name, data in png_glyphs.items():

            # Because the PNG canvas and font both use a 1000-unit
            # coordinate concept, the advance now follows the
            # visible bitmap width naturally.

            advance_width = data["canvas_width"]

            # Prevent ridiculous spacing from unusually wide artwork.
            advance_width = max(
                350,
                min(1500, advance_width)
            )

            metrics[glyph_name] = (
                int(advance_width),
                0
            )

        fb.setupHorizontalMetrics(metrics)

        # =========================================================
        # VERTICAL METRICS
        # =========================================================

        fb.setupHorizontalHeader(
            ascent=950,
            descent=-50,
            lineGap=100
        )

        fb.setupNameTable({
            "familyName": font_name,
            "styleName": "Regular",

            "uniqueFontIdentifier":
                f"{font_name} Moonlight Color 1.1",

            "fullName": font_name,

            "psName": safe_name,

            "version": "Version 1.100"
        })

        fb.setupOS2(
            sTypoAscender=950,
            sTypoDescender=-50,
            sTypoLineGap=100,

            usWinAscent=1000,
            usWinDescent=100
        )

        fb.setupPost()
        fb.setupMaxp()

        fb.save(output_path)

        # =========================================================
        # SBIX COLOR BITMAP TABLE
        # =========================================================

        font = TTFont(output_path)

        sbix = newTable("sbix")

        sbix.version = 1
        sbix.flags = 1
        sbix.strikes = {}

        # ---------------------------------------------------------
        # KEY FIX:
        #
        # Our bitmap canvas is based around a 1000-unit design.
        # Using 1000 ppem makes its relationship to the font's
        # 1000 units-per-em predictable.
        # ---------------------------------------------------------

        strike_ppem = 1000

        strike = Strike(
            ppem=strike_ppem,
            resolution=72
        )

        strike.glyphs = {}

        strike.glyphs[".notdef"] = SbixGlyph(
            glyphName=".notdef"
        )

        strike.glyphs["space"] = SbixGlyph(
            glyphName="space"
        )

        for glyph_name, data in png_glyphs.items():

            # sbix uses a baseline origin.
            #
            # Artwork occupies y=50 through y=950 in the PNG.
            # Move the bitmap origin so all letters share the
            # same baseline.

            glyph = SbixGlyph(
                glyphName=glyph_name,
                graphicType="png ",
                imageData=data["png"],

                originOffsetX=0,

                # Position bitmap consistently relative to baseline.
                originOffsetY=-50
            )

            strike.glyphs[glyph_name] = glyph

        sbix.strikes[strike_ppem] = strike

        font["sbix"] = sbix

        font.save(output_path)
        font.close()

        # =========================================================
        # VERIFY FONT
        # =========================================================

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
            "error":
                "Color font compilation failed.",

            "details":
                str(exc)
        }), 500


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
