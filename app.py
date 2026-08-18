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
# Server-side TTF + OTF export
# 5000 x 5000 transparent PNG glyph workflow
# ============================================================

app = Flask(__name__)
CORS(app)

app.config["MAX_CONTENT_LENGTH"] = 250 * 1024 * 1024

VERSION = "5.0-server-fonts"

SOURCE_SIZE = 5000
BITMAP_SIZE = 512

UNITS_PER_EM = 1000
ASCENT = 850
DESCENT = -150
LINE_GAP = 100

GLYPH_ADVANCE = 1000
SPACE_ADVANCE = 500


# ============================================================
# STATUS
# ============================================================

@app.get("/")
def home():
    return jsonify({
        "status": "ok",
        "service": "Moonlight Color Font Compiler",
        "version": VERSION,
        "formats": ["ttf", "otf"]
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "healthy",
        "version": VERSION
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
    Preserve the complete transparent square.

    IMPORTANT:
    - Do NOT crop artwork.
    - Do NOT trim transparent margins.
    - Do NOT resize based on visible artwork.
    - Every glyph receives exactly the same transformation.

    Standard Moonlight source:
        5000 x 5000 transparent PNG

    Font bitmap:
        complete canvas -> 512 x 512
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

    alpha = image.getchannel("A")

    if alpha.getbbox() is None:
        image.close()

        raise ValueError(
            f"{uploaded_file.filename or 'Uploaded file'} "
            "contains no visible artwork."
        )

    # --------------------------------------------------------
    # STANDARDIZE THE COMPLETE CANVAS
    # --------------------------------------------------------

    if image.size != (SOURCE_SIZE, SOURCE_SIZE):

        normalized = Image.new(
            "RGBA",
            (SOURCE_SIZE, SOURCE_SIZE),
            (0, 0, 0, 0)
        )

        scale = min(
            SOURCE_SIZE / max(image.width, 1),
            SOURCE_SIZE / max(image.height, 1)
        )

        width = max(
            1,
            round(image.width * scale)
        )

        height = max(
            1,
            round(image.height * scale)
        )

        resized = image.resize(
            (width, height),
            Image.Resampling.LANCZOS
        )

        x = (SOURCE_SIZE - width) // 2
        y = (SOURCE_SIZE - height) // 2

        normalized.alpha_composite(
            resized,
            (x, y)
        )

        resized.close()
        image.close()

        image = normalized

    # --------------------------------------------------------
    # DOWNSAMPLE COMPLETE 5000 x 5000 CANVAS
    # --------------------------------------------------------

    bitmap = image.resize(
        (BITMAP_SIZE, BITMAP_SIZE),
        Image.Resampling.LANCZOS
    )

    output = io.BytesIO()

    bitmap.save(
        output,
        format="PNG",
        compress_level=6
    )

    result = output.getvalue()

    output.close()
    bitmap.close()
    image.close()

    return result


def receive_glyphs():
    """
    Read glyph uploads and their explicit Unicode assignments.

    The browser sends:
        glyphs=A.png
        characters=A

    We map:
        A -> U+0041
        B -> U+0042
        etc.

    No filename guessing is used.
    """

    font_name = request.form.get(
        "font_name",
        "Moonlight Color Alpha"
    ).strip()

    if not font_name:
        font_name = "Moonlight Color Alpha"

    files = request.files.getlist("glyphs")
    characters = request.form.getlist("characters")

    if not files:
        raise ValueError(
            "No glyph PNG files were uploaded."
        )

    if not characters:
        raise ValueError(
            "No character assignments were supplied."
        )

    if len(files) != len(characters):
        raise ValueError(
            f"Received {len(files)} images but "
            f"{len(characters)} character assignments."
        )

    glyph_data = []
    used_codepoints = set()

    for uploaded, assignment in zip(
        files,
        characters
    ):

        assignment = (
            assignment or ""
        ).strip()

        if not assignment:
            continue

        # Actual Unicode character from the mapping box.
        char = next(iter(assignment))

        codepoint = ord(char)

        if codepoint == 32:
            continue

        if codepoint in used_codepoints:
            continue

        used_codepoints.add(codepoint)

        glyph_data.append({
            "character": char,
            "codepoint": codepoint,
            "glyph_name": f"uni{codepoint:04X}",
            "png": prepare_png(uploaded)
        })

    if not glyph_data:
        raise ValueError(
            "No valid mapped character PNGs were supplied."
        )

    return font_name, glyph_data


# ============================================================
# BUILD COLOR FONT
# ============================================================

def build_color_font(
    font_name,
    glyph_data,
    extension
):

    safe_name = clean_font_name(font_name)

    workdir = Path(
        tempfile.mkdtemp(
            prefix="moonlight_font_"
        )
    )

    output_path = (
        workdir /
        f"{safe_name}.{extension}"
    )

    # --------------------------------------------------------
    # GLYPH ORDER
    # --------------------------------------------------------

    glyph_order = [
        ".notdef",
        "space"
    ]

    glyph_order.extend(
        item["glyph_name"]
        for item in glyph_data
    )

    # --------------------------------------------------------
    # CHARACTER MAP
    # --------------------------------------------------------

    character_map = {
        32: "space"
    }

    for item in glyph_data:
        character_map[
            item["codepoint"]
        ] = item["glyph_name"]

    # --------------------------------------------------------
    # TRUE TYPE SHELL
    #
    # sbix is a TrueType/OpenType bitmap-color mechanism.
    # Both download routes use this same tested internal font.
    # --------------------------------------------------------

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

    glyphs = {
        name: make_empty_glyph()
        for name in glyph_order
    }

    fb.setupGlyf(
        glyphs
    )

    # --------------------------------------------------------
    # FIXED METRICS
    #
    # No glyph-specific sizing.
    # --------------------------------------------------------

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

    for item in glyph_data:

        metrics[
            item["glyph_name"]
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
        lineGap=LINE_GAP
    )

    # --------------------------------------------------------
    # FONT NAME TABLE
    # --------------------------------------------------------

    fb.setupNameTable({
        "familyName": font_name,
        "styleName": "Regular",
        "uniqueFontIdentifier":
            f"{font_name} Moonlight {VERSION}",
        "fullName": font_name,
        "psName": safe_name,
        "version": "Version 5.000"
    })

    # --------------------------------------------------------
    # OS/2
    # --------------------------------------------------------

    fb.setupOS2(
        sTypoAscender=ASCENT,
        sTypoDescender=DESCENT,
        sTypoLineGap=LINE_GAP,

        usWinAscent=1000,
        usWinDescent=200,

        sxHeight=500,
        sCapHeight=700
    )

    fb.setupPost()
    fb.setupMaxp()

    # --------------------------------------------------------
    # SAVE BASE FONT
    # --------------------------------------------------------

    fb.save(
        output_path
    )

    # ========================================================
    # SBIX COLOR PNG TABLE
    # ========================================================

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
        ppem=BITMAP_SIZE,
        resolution=72
    )

    strike.glyphs = {}

    strike.glyphs[".notdef"] = SbixGlyph(
        glyphName=".notdef"
    )

    strike.glyphs["space"] = SbixGlyph(
        glyphName="space"
    )

    # --------------------------------------------------------
    # IDENTICAL PLACEMENT FOR EVERY LETTER
    # --------------------------------------------------------

    for item in glyph_data:

        strike.glyphs[
            item["glyph_name"]
        ] = SbixGlyph(

            glyphName=item["glyph_name"],

            graphicType="png ",

            imageData=item["png"],

            originOffsetX=0,

            originOffsetY=0
        )

    sbix.strikes[
        BITMAP_SIZE
    ] = strike

    font["sbix"] = sbix

    font.save(
        output_path
    )

    font.close()

    # ========================================================
    # VERIFY FONT
    # ========================================================

    verify = TTFont(
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

    missing_tables = [
        table
        for table in required_tables
        if table not in verify
    ]

    cmap = (
        verify.getBestCmap()
        or {}
    )

    missing_characters = [
        item["codepoint"]
        for item in glyph_data
        if item["codepoint"] not in cmap
    ]

    verify.close()

    if missing_tables:

        raise ValueError(
            "Generated font is missing tables: "
            + ", ".join(missing_tables)
        )

    if missing_characters:

        readable = ", ".join(
            f"U+{codepoint:04X}"
            for codepoint in missing_characters
        )

        raise ValueError(
            "Character mapping verification failed: "
            + readable
        )

    if not output_path.exists():

        raise ValueError(
            "Font output file was not created."
        )

    if output_path.stat().st_size < 1000:

        raise ValueError(
            "Generated font appears invalid."
        )

    return output_path


# ============================================================
# TTF
# ============================================================

@app.post("/compile/ttf")
def compile_ttf():

    try:

        font_name, glyph_data = (
            receive_glyphs()
        )

        output_path = build_color_font(
            font_name,
            glyph_data,
            "ttf"
        )

        return send_file(
            output_path,
            mimetype="font/ttf",
            as_attachment=True,
            download_name=output_path.name,
            max_age=0
        )

    except ValueError as exc:

        return jsonify({
            "error": str(exc)
        }), 400

    except Exception as exc:

        app.logger.exception(
            "Moonlight TTF compilation failed"
        )

        return jsonify({
            "error":
                "TTF compilation failed.",
            "details":
                str(exc)
        }), 500


# ============================================================
# OTF
# ============================================================

@app.post("/compile/otf")
def compile_otf():

    try:

        font_name, glyph_data = (
            receive_glyphs()
        )

        output_path = build_color_font(
            font_name,
            glyph_data,
            "otf"
        )

        return send_file(
            output_path,
            mimetype="font/otf",
            as_attachment=True,
            download_name=output_path.name,
            max_age=0
        )

    except ValueError as exc:

        return jsonify({
            "error": str(exc)
        }), 400

    except Exception as exc:

        app.logger.exception(
            "Moonlight OTF compilation failed"
        )

        return jsonify({
            "error":
                "OTF compilation failed.",
            "details":
                str(exc)
        }), 500


# ============================================================
# OLD ROUTE COMPATIBILITY
#
# Your older index versions can still call /compile.
# ============================================================

@app.post("/compile")
def compile_legacy():

    try:

        font_name, glyph_data = (
            receive_glyphs()
        )

        output_path = build_color_font(
            font_name,
            glyph_data,
            "ttf"
        )

        return send_file(
            output_path,
            mimetype="font/ttf",
            as_attachment=True,
            download_name=output_path.name,
            max_age=0
        )

    except ValueError as exc:

        return jsonify({
            "error": str(exc)
        }), 400

    except Exception as exc:

        app.logger.exception(
            "Moonlight legacy compilation failed"
        )

        return jsonify({
            "error":
                "Font compilation failed.",
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
