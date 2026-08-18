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
# PNG ALPHA -> COLOR TTF
# ============================================================

app = Flask(__name__)
CORS(app)

app.config["MAX_CONTENT_LENGTH"] = 250 * 1024 * 1024


# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def home():
    return jsonify({
        "status": "ok",
        "service": "Moonlight Color Font Compiler",
        "version": "3.0-metrics-fix"
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "healthy",
        "version": "3.0-metrics-fix"
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
    Prepare a PNG specifically for an sbix color font.

    IMPORTANT:

    Every glyph is placed on the SAME SIZE canvas.

    This gives the alphabet consistent visual sizing instead
    of allowing every individual PNG to create its own size.
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
    # TRIM TRANSPARENT SPACE
    # --------------------------------------------------------

    alpha = image.getchannel("A")

    bbox = alpha.getbbox()

    if bbox:

        image = image.crop(bbox)

    else:

        raise ValueError(
            f"{uploaded_file.filename or 'Uploaded file'} "
            "contains no visible artwork."
        )

    # --------------------------------------------------------
    # STANDARD FONT CANVAS
    #
    # ALL letters use the same 512 x 512 canvas.
    # --------------------------------------------------------

    CANVAS_SIZE = 512

    SIDE_PADDING = 48

    TOP_PADDING = 38

    BOTTOM_PADDING = 55

    available_width = (
        CANVAS_SIZE -
        SIDE_PADDING * 2
    )

    available_height = (
        CANVAS_SIZE -
        TOP_PADDING -
        BOTTOM_PADDING
    )

    # --------------------------------------------------------
    # SCALE ARTWORK
    #
    # Preserve aspect ratio.
    # --------------------------------------------------------

    scale = min(
        available_width / max(image.width, 1),
        available_height / max(image.height, 1)
    )

    new_width = max(
        1,
        round(
            image.width * scale
        )
    )

    new_height = max(
        1,
        round(
            image.height * scale
        )
    )

    image = image.resize(
        (
            new_width,
            new_height
        ),
        Image.Resampling.LANCZOS
    )

    # --------------------------------------------------------
    # CREATE STANDARD TRANSPARENT CANVAS
    # --------------------------------------------------------

    canvas = Image.new(
        "RGBA",
        (
            CANVAS_SIZE,
            CANVAS_SIZE
        ),
        (
            0,
            0,
            0,
            0
        )
    )

    # --------------------------------------------------------
    # CENTER HORIZONTALLY
    # ALIGN TO COMMON BOTTOM
    #
    # This is important for a consistent baseline.
    # --------------------------------------------------------

    x = (
        CANVAS_SIZE -
        new_width
    ) // 2

    y = (
        CANVAS_SIZE -
        BOTTOM_PADDING -
        new_height
    )

    canvas.alpha_composite(
        image,
        (
            x,
            y
        )
    )

    # --------------------------------------------------------
    # SAVE NORMAL PNG BYTES
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

    return png_bytes


# ============================================================
# COMPILE
# ============================================================

@app.post("/compile")
def compile_font():

    try:

        # ----------------------------------------------------
        # FORM DATA
        # ----------------------------------------------------

        font_name = request.form.get(
            "font_name",
            "Moonlight Color Alpha"
        ).strip()

        if not font_name:

            font_name = (
                "Moonlight Color Alpha"
            )

        files = request.files.getlist(
            "glyphs"
        )

        characters = request.form.getlist(
            "characters"
        )

        if not files:

            return jsonify({
                "error":
                    "No glyph PNG files were uploaded."
            }), 400

        if not characters:

            return jsonify({
                "error":
                    "No character assignments were supplied."
            }), 400

        if len(files) != len(characters):

            return jsonify({
                "error":
                    f"Received {len(files)} images but "
                    f"{len(characters)} character assignments."
            }), 400

        # ----------------------------------------------------
        # TEMP FOLDER
        # ----------------------------------------------------

        workdir = Path(
            tempfile.mkdtemp(
                prefix="moonlight_font_"
            )
        )

        safe_name = clean_font_name(
            font_name
        )

        output_path = (
            workdir /
            f"{safe_name}.ttf"
        )

        # ----------------------------------------------------
        # FONT INFORMATION
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
        # PROCESS EACH LETTER
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

            if codepoint in used_codepoints:

                continue

            used_codepoints.add(
                codepoint
            )

            glyph_name = (
                f"uni{codepoint:04X}"
            )

            png_data = prepare_png(
                uploaded
            )

            glyph_order.append(
                glyph_name
            )

            character_map[
                codepoint
            ] = glyph_name

            png_glyphs[
                glyph_name
            ] = png_data

        if not png_glyphs:

            return jsonify({
                "error":
                    "No valid character PNGs were supplied."
            }), 400

        # ====================================================
        # BUILD FONT
        # ====================================================

        UNITS_PER_EM = 1000

        ASCENT = 800

        DESCENT = -200

        # ----------------------------------------------------
        # IMPORTANT METRICS FIX
        #
        # Every alpha gets a predictable advance width.
        #
        # This prevents the following character from being
        # positioned inside the bitmap.
        # ----------------------------------------------------

        GLYPH_ADVANCE = 1000

        SPACE_ADVANCE = 500

        fb = FontBuilder(
            UNITS_PER_EM,
            isTTF=True
        )

        fb.setupGlyphOrder(
            glyph_order
        )

        fb.setupCharacterMap(
            character_map
        )

        # ----------------------------------------------------
        # GLYF
        # ----------------------------------------------------

        glyphs = {}

        for glyph_name in glyph_order:

            glyphs[
                glyph_name
            ] = make_empty_glyph()

        fb.setupGlyf(
            glyphs
        )

        # ----------------------------------------------------
        # HORIZONTAL METRICS
        # ----------------------------------------------------

        metrics = {
            ".notdef": (
                GLYPH_ADVANCE,
                0
            ),

            "space": (
                SPACE_ADVANCE,
                0
            )
        }

        for glyph_name in png_glyphs:

            metrics[
                glyph_name
            ] = (
                GLYPH_ADVANCE,
                0
            )

        fb.setupHorizontalMetrics(
            metrics
        )

        fb.setupHorizontalHeader(
            ascent=ASCENT,
            descent=DESCENT,
            lineGap=100
        )

        # ----------------------------------------------------
        # FONT NAMES
        # ----------------------------------------------------

        fb.setupNameTable({

            "familyName":
                font_name,

            "styleName":
                "Regular",

            "uniqueFontIdentifier":
                f"{font_name} Moonlight V3",

            "fullName":
                font_name,

            "psName":
                safe_name,

            "version":
                "Version 3.000"
        })

        # ----------------------------------------------------
        # OS/2 METRICS
        # ----------------------------------------------------

        fb.setupOS2(

            sTypoAscender=ASCENT,

            sTypoDescender=DESCENT,

            sTypoLineGap=100,

            usWinAscent=900,

            usWinDescent=200,

            sxHeight=500,

            sCapHeight=700
        )

        fb.setupPost()

        fb.setupMaxp()

        # ----------------------------------------------------
        # SAVE BASIC TTF
        # ----------------------------------------------------

        fb.save(
            output_path
        )

        # ====================================================
        # SBIX COLOR TABLE
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

        # ----------------------------------------------------
        # 512 PPEm STRIKE
        #
        # Our images are 512x512.
        # ----------------------------------------------------

        strike = Strike(
            ppem=512,
            resolution=72
        )

        strike.glyphs = {}

        # ----------------------------------------------------
        # EMPTY REQUIRED GLYPHS
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
        # ADD COLOR GLYPHS
        #
        # THE POSITION FIX IS HERE.
        #
        # originOffsetX centers the bitmap in the glyph.
        #
        # originOffsetY moves it relative to the baseline.
        # ----------------------------------------------------

        for glyph_name, png_data in (
            png_glyphs.items()
        ):

            strike.glyphs[
                glyph_name
            ] = SbixGlyph(

                glyphName=glyph_name,

                graphicType="png ",

                imageData=png_data,

                # Center image horizontally.
                originOffsetX=0,

                # Lift image above baseline.
                originOffsetY=55
            )

        sbix.strikes[
            512
        ] = strike

        font["sbix"] = sbix

        # ----------------------------------------------------
        # SAVE FINAL COLOR FONT
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

        # Check mapping too.

        cmap = test_font.getBestCmap()

        mapped_characters = len(
            [
                cp
                for cp in used_codepoints
                if cp in cmap
            ]
        )

        test_font.close()

        if missing:

            return jsonify({

                "error":
                    "Font is missing required tables.",

                "details":
                    ", ".join(missing)

            }), 500

        if mapped_characters == 0:

            return jsonify({

                "error":
                    "Font was created but character "
                    "mapping failed."

            }), 500

        # ----------------------------------------------------
        # FILE CHECK
        # ----------------------------------------------------

        if not output_path.exists():

            return jsonify({

                "error":
                    "TTF output file was not created."

            }), 500

        if output_path.stat().st_size < 1000:

            return jsonify({

                "error":
                    "Generated TTF appears invalid."

            }), 500

        # ====================================================
        # DOWNLOAD
        # ====================================================

        return send_file(

            output_path,

            mimetype=(
                "font/ttf"
            ),

            as_attachment=True,

            download_name=(
                f"{safe_name}.ttf"
            ),

            max_age=0
        )

    # ========================================================
    # ERROR HANDLER
    # ========================================================

    except Exception as exc:

        app.logger.exception(
            "Moonlight font compilation failed"
        )

        return jsonify({

            "error":
                "Color font compilation failed.",

            "details":
                str(exc)

        }), 500


# ============================================================
# RUN
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
