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


# ============================================================
# MOONLIGHT COLOR FONT COMPILER
# PNG glyphs -> installable color TTF
# ============================================================

app = Flask(__name__)
CORS(app)

# Allow large alphabet uploads.
app.config["MAX_CONTENT_LENGTH"] = 250 * 1024 * 1024


# ============================================================
# BASIC ROUTES
# ============================================================

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


# ============================================================
# HELPERS
# ============================================================

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
    Reads an uploaded PNG/image, trims transparent space,
    resizes very large artwork, adds transparent padding,
    and returns normal PNG bytes for the sbix font table.
    """

    raw = uploaded_file.read()

    if not raw:
        raise ValueError(
            f"{uploaded_file.filename or 'Uploaded file'} is empty."
        )

    try:
        with Image.open(io.BytesIO(raw)) as opened:
            image = opened.convert("RGBA")
    except Exception as exc:
        raise ValueError(
            f"{uploaded_file.filename or 'Uploaded file'} "
            f"is not a valid image: {exc}"
        )

    # --------------------------------------------------------
    # Trim transparent empty area
    # --------------------------------------------------------

    alpha = image.getchannel("A")
    bbox = alpha.getbbox()

    if bbox:
        image = image.crop(bbox)

    if image.width < 1 or image.height < 1:
        raise ValueError(
            f"{uploaded_file.filename or 'Uploaded file'} "
            "contains no visible artwork."
        )

    # --------------------------------------------------------
    # Resize large artwork
    #
    # Color fonts do NOT need the original 5000px image inside
    # every glyph. Keeping the embedded PNGs smaller makes the
    # font dramatically faster and lighter.
    # --------------------------------------------------------

    max_dimension = 512

    scale = min(
        max_dimension / image.width,
        max_dimension / image.height,
        1.0
    )

    if scale < 1.0:
        new_width = max(
            1,
            round(image.width * scale)
        )

        new_height = max(
            1,
            round(image.height * scale)
        )

        image = image.resize(
            (new_width, new_height),
            Image.Resampling.LANCZOS
        )

    # --------------------------------------------------------
    # Transparent padding
    # --------------------------------------------------------

    padding = 20

    canvas_width = image.width + (padding * 2)
    canvas_height = image.height + (padding * 2)

    canvas = Image.new(
        "RGBA",
        (canvas_width, canvas_height),
        (0, 0, 0, 0)
    )

    canvas.alpha_composite(
        image,
        (padding, padding)
    )

    # --------------------------------------------------------
    # Convert canvas to ordinary PNG bytes
    #
    # IMPORTANT:
    # Do not use optimize=True here.
    # --------------------------------------------------------

    output = io.BytesIO()

    canvas.save(
        output,
        format="PNG",
        compress_level=6
    )

    png_bytes = output.getvalue()

    output.close()
    image.close()
    canvas.close()

    return (
        png_bytes,
        canvas_width,
        canvas_height
    )


# ============================================================
# FONT COMPILER
# ============================================================

@app.post("/compile")
def compile_font():

    try:
        # ----------------------------------------------------
        # Read form information
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # Temporary working folder
        # ----------------------------------------------------

        workdir = Path(
            tempfile.mkdtemp(
                prefix="moonlight_font_"
            )
        )

        safe_name = clean_font_name(font_name)

        output_path = (
            workdir /
            f"{safe_name}.ttf"
        )

        # ----------------------------------------------------
        # Basic font structure
        # ----------------------------------------------------

        glyph_order = [
            ".notdef",
            "space"
        ]

        character_map = {
            32: "space"
        }

        png_glyphs = {}

        used_codepoints = set()

        # ----------------------------------------------------
        # Process uploaded letters
        # ----------------------------------------------------

        for uploaded, character in zip(
            files,
            characters
        ):

            character = (
                character or ""
            ).strip()

            if not character:
                continue

            char = character[0]
            codepoint = ord(char)

            # Skip duplicate mappings.
            if codepoint in used_codepoints:
                continue

            used_codepoints.add(codepoint)

            glyph_name = (
                f"uni{codepoint:04X}"
            )

            png_data, width, height = (
                prepare_png(uploaded)
            )

            glyph_order.append(
                glyph_name
            )

            character_map[
                codepoint
            ] = glyph_name

            png_glyphs[
                glyph_name
            ] = {
                "png": png_data,
                "width": width,
                "height": height
            }

        if not png_glyphs:
            return jsonify({
                "error":
                    "No valid character PNGs were supplied."
            }), 400

        # ====================================================
        # BUILD TRUE TYPE FONT
        # ====================================================

        units_per_em = 1000

        fb = FontBuilder(
            units_per_em,
            isTTF=True
        )

        fb.setupGlyphOrder(
            glyph_order
        )

        fb.setupCharacterMap(
            character_map
        )

        # ----------------------------------------------------
        # Empty outline glyphs
        #
        # Actual visible artwork comes from the sbix PNG table.
        # ----------------------------------------------------

        glyphs = {}

        for glyph_name in glyph_order:
            glyphs[glyph_name] = (
                make_empty_glyph()
            )

        fb.setupGlyf(
            glyphs
        )

        # ----------------------------------------------------
        # Glyph spacing
        # ----------------------------------------------------

        metrics = {
            ".notdef": (1000, 0),
            "space": (500, 0)
        }

        for glyph_name, data in (
            png_glyphs.items()
        ):

            ratio = (
                data["width"] /
                max(
                    data["height"],
                    1
                )
            )

            advance_width = int(
                max(
                    350,
                    min(
                        1300,
                        900 * ratio
                    )
                )
            )

            metrics[
                glyph_name
            ] = (
                advance_width,
                0
            )

        fb.setupHorizontalMetrics(
            metrics
        )

        fb.setupHorizontalHeader(
            ascent=900,
            descent=-100
        )

        # ----------------------------------------------------
        # Font naming
        # ----------------------------------------------------

        fb.setupNameTable({
            "familyName":
                font_name,

            "styleName":
                "Regular",

            "uniqueFontIdentifier":
                f"{font_name} Moonlight Color",

            "fullName":
                font_name,

            "psName":
                safe_name,

            "version":
                "Version 1.000"
        })

        fb.setupOS2(
            sTypoAscender=900,
            sTypoDescender=-100,
            usWinAscent=1000,
            usWinDescent=200
        )

        fb.setupPost()
        fb.setupMaxp()

        # Save basic TrueType shell.
        fb.save(
            output_path
        )

        # ====================================================
        # ADD COLOR PNG GLYPHS
        # ====================================================

        font = TTFont(
            output_path
        )

        sbix = newTable(
            "sbix"
        )

        sbix.version = 1
        sbix.flags = 1
        sbix.strikes = {}

        strike = Strike(
            ppem=128,
            resolution=72
        )

        strike.glyphs = {}

        # ----------------------------------------------------
        # Required basic glyphs
        # ----------------------------------------------------

        strike.glyphs[
            ".notdef"
        ] = SbixGlyph(
            glyphName=".notdef"
        )

        strike.glyphs[
            "space"
        ] = SbixGlyph(
            glyphName="space"
        )

        # ----------------------------------------------------
        # Add each PNG as a color glyph
        # ----------------------------------------------------

        for glyph_name, data in (
            png_glyphs.items()
        ):

            strike.glyphs[
                glyph_name
            ] = SbixGlyph(
                glyphName=glyph_name,
                graphicType="png ",
                imageData=data["png"],
                originOffsetX=0,
                originOffsetY=0
            )

        sbix.strikes[
            128
        ] = strike

        font["sbix"] = sbix

        # ----------------------------------------------------
        # Save completed color font
        # ----------------------------------------------------

        font.save(
            output_path
        )

        font.close()

        # ====================================================
        # VERIFY FONT
        # ====================================================

        test_font = TTFont(
            output_path
        )

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
                    "Font was created but is "
                    "missing required tables.",

                "details":
                    ", ".join(missing)
            }), 500

        # ----------------------------------------------------
        # Make sure file actually exists
        # ----------------------------------------------------

        if not output_path.exists():
            return jsonify({
                "error":
                    "Font compilation finished "
                    "but the TTF file was not found."
            }), 500

        if output_path.stat().st_size < 1000:
            return jsonify({
                "error":
                    "The generated TTF file "
                    "appears to be invalid."
            }), 500

        # ====================================================
        # SEND FONT
        # ====================================================

        return send_file(
            output_path,
            mimetype="font/ttf",
            as_attachment=True,
            download_name=(
                f"{safe_name}.ttf"
            ),
            max_age=0
        )

    # ========================================================
    # ERROR HANDLING
    # ========================================================

    except Exception as exc:

        app.logger.exception(
            "Moonlight color font compilation failed"
        )

        return jsonify({
            "error":
                "Color font compilation failed.",

            "details":
                str(exc)
        }), 500


# ============================================================
# START SERVER
# ============================================================

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
