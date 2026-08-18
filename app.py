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
# 5000x5000 TRANSPARENT PNG ALPHA -> TYPEABLE COLOR TTF
# ============================================================

app = Flask(__name__)
CORS(app)

# Allow large alphabet uploads.
app.config["MAX_CONTENT_LENGTH"] = 250 * 1024 * 1024


# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def home():
    return jsonify({
        "status": "ok",
        "service": "Moonlight Color Font Compiler",
        "version": "4.0-5000-canvas"
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "healthy",
        "version": "4.0-5000-canvas"
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
    Prepare a Moonlight PNG for the sbix color font.

    STANDARD:
    - Source artwork should be 5000 x 5000 px.
    - Transparent background.
    - Full square canvas is preserved.
    - Transparent margins are NEVER cropped.
    - Every glyph receives identical scaling.

    The full 5000 x 5000 canvas is reduced to a 512 x 512
    bitmap for the font's sbix strike.
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
    # CHECK FOR VISIBLE ARTWORK
    # --------------------------------------------------------

    alpha = image.getchannel("A")

    if alpha.getbbox() is None:
        image.close()

        raise ValueError(
            f"{uploaded_file.filename or 'Uploaded file'} "
            "contains no visible artwork."
        )

    # --------------------------------------------------------
    # STANDARD MOONLIGHT SOURCE CANVAS
    # --------------------------------------------------------

    SOURCE_SIZE = 5000
    FONT_BITMAP_SIZE = 512

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # DO NOT CROP TRANSPARENT SPACE.
    #
    # If the uploaded image is already 5000x5000, we leave
    # its entire canvas exactly as supplied.
    # --------------------------------------------------------

    if image.size != (SOURCE_SIZE, SOURCE_SIZE):

        normalized = Image.new(
            "RGBA",
            (
                SOURCE_SIZE,
                SOURCE_SIZE
            ),
            (
                0,
                0,
                0,
                0
            )
        )

        # Fit the COMPLETE source image into the square.
        # Never stretch it.
        scale = min(
            SOURCE_SIZE / max(image.width, 1),
            SOURCE_SIZE / max(image.height, 1)
        )

        new_width = max(
            1,
            round(image.width * scale)
        )

        new_height = max(
            1,
            round(image.height * scale)
        )

        resized = image.resize(
            (
                new_width,
                new_height
            ),
            Image.Resampling.LANCZOS
        )

        # Center complete source canvas.
        x = (
            SOURCE_SIZE -
            new_width
        ) // 2

        y = (
            SOURCE_SIZE -
            new_height
        ) // 2

        normalized.alpha_composite(
            resized,
            (
                x,
                y
            )
        )

        resized.close()
        image.close()

        image = normalized

    # --------------------------------------------------------
    # FONT BITMAP
    #
    # Scale the COMPLETE square canvas.
    #
    # A, B, C, etc. therefore all receive the exact same
    # transformation.
    # --------------------------------------------------------

    font_image = image.resize(
        (
            FONT_BITMAP_SIZE,
            FONT_BITMAP_SIZE
        ),
        Image.Resampling.LANCZOS
    )

    output = io.BytesIO()

    font_image.save(
        output,
        format="PNG",
        compress_level=6
    )

    png_bytes = output.getvalue()

    output.close()
    font_image.close()
    image.close()

    return png_bytes


# ============================================================
# COMPILE FONT
# ============================================================

@app.post("/compile")
def compile_font():

    try:

        # ----------------------------------------------------
        # RECEIVE FORM DATA
        # ----------------------------------------------------

        font_name = request.form.get(
            "font_name",
            "Moonlight Color Alpha"
        ).strip()

        if not font_name:
            font_name = "Moonlight Color Alpha"

        files = request.files.getlist(
            "glyphs"
        )

        characters = request.form.getlist(
            "characters"
        )

        # ----------------------------------------------------
        # VALIDATE UPLOAD
        # ----------------------------------------------------

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
        # TEMPORARY OUTPUT FOLDER
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
        # GLYPH SETUP
        # ----------------------------------------------------

        glyph_order = [
            ".notdef",
            "space"
        ]

        # Space maps to U+0020.
        character_map = {
            32: "space"
        }

        png_glyphs = {}

        used_codepoints = set()

        # ----------------------------------------------------
        # PROCESS EACH CHARACTER
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

            # Use the actual first Unicode character assigned
            # by the Moonlight front-end.
            char = next(
                iter(character)
            )

            codepoint = ord(
                char
            )

            # Prevent duplicate mappings.
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

            # THIS is what makes the font typeable.
            #
            # Example:
            # A = U+0041 -> uni0041
            # B = U+0042 -> uni0042
            # C = U+0043 -> uni0043
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
        # FONT METRICS
        # ====================================================

        UNITS_PER_EM = 1000

        ASCENT = 850
        DESCENT = -150

        # Every letter occupies the same horizontal cell.
        GLYPH_ADVANCE = 1000

        SPACE_ADVANCE = 500

        # ====================================================
        # BUILD TRUETYPE SHELL
        # ====================================================

        fb = FontBuilder(
            UNITS_PER_EM,
            isTTF=True
        )

        # ----------------------------------------------------
        # GLYPH ORDER
        # ----------------------------------------------------

        fb.setupGlyphOrder(
            glyph_order
        )

        # ----------------------------------------------------
        # UNICODE CHARACTER MAP
        #
        # This cmap is what connects keyboard characters
        # to the correct Moonlight PNG glyph.
        # ----------------------------------------------------

        fb.setupCharacterMap(
            character_map
        )

        # ----------------------------------------------------
        # EMPTY GLYF OUTLINES
        #
        # Visible artwork comes from sbix PNGs.
        # ----------------------------------------------------

        glyphs = {
            glyph_name: make_empty_glyph()
            for glyph_name in glyph_order
        }

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

        # ----------------------------------------------------
        # HORIZONTAL HEADER
        # ----------------------------------------------------

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
                f"{font_name} Moonlight V4",

            "fullName":
                font_name,

            "psName":
                safe_name,

            "version":
                "Version 4.000"
        })

        # ----------------------------------------------------
        # OS/2
        # ----------------------------------------------------

        fb.setupOS2(

            sTypoAscender=ASCENT,

            sTypoDescender=DESCENT,

            sTypoLineGap=100,

            usWinAscent=1000,

            usWinDescent=200,

            sxHeight=500,

            sCapHeight=700
        )

        fb.setupPost()

        fb.setupMaxp()

        # ----------------------------------------------------
        # SAVE BASE TTF
        # ----------------------------------------------------

        fb.save(
            output_path
        )

        # ====================================================
        # ADD COLOR PNG SBIX TABLE
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
        # BITMAP STRIKE
        #
        # Every source PNG has been uniformly converted from
        # its full 5000x5000 canvas to 512x512.
        # ----------------------------------------------------

        strike = Strike(
            ppem=512,
            resolution=72
        )

        strike.glyphs = {}

        # ----------------------------------------------------
        # REQUIRED EMPTY GLYPHS
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
        # ADD PNG LETTERS
        #
        # All glyphs receive IDENTICAL origin positioning.
        # No letter-specific calculations.
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

                originOffsetX=0,

                originOffsetY=0
            )

        sbix.strikes[
            512
        ] = strike

        font["sbix"] = sbix

        # ----------------------------------------------------
        # SAVE FINAL TTF
        # ----------------------------------------------------

        font.save(
            output_path
        )

        font.close()

        # ====================================================
        # VERIFY GENERATED FONT
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

        cmap = (
            test_font.getBestCmap()
            or {}
        )

        # Make sure every uploaded character made it into
        # the final cmap.
        missing_mappings = [
            cp
            for cp in used_codepoints
            if cp not in cmap
        ]

        test_font.close()

        # ----------------------------------------------------
        # TABLE VALIDATION
        # ----------------------------------------------------

        if missing:

            return jsonify({

                "error":
                    "Font is missing required tables.",

                "details":
                    ", ".join(missing)

            }), 500

        # ----------------------------------------------------
        # CHARACTER MAP VALIDATION
        # ----------------------------------------------------

        if missing_mappings:

            readable = ", ".join(
                f"U+{cp:04X}"
                for cp in missing_mappings
            )

            return jsonify({

                "error":
                    "Some characters were not mapped.",

                "details":
                    readable

            }), 500

        # ----------------------------------------------------
        # OUTPUT VALIDATION
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

            mimetype="font/ttf",

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
