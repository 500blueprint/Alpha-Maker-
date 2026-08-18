import os
import shutil
import subprocess
import tempfile
import base64
from pathlib import Path

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS


app = Flask(__name__)
CORS(app)


@app.get("/")
def home():
    return jsonify({
        "status": "ok",
        "service": "Moonlight Color Font Compiler"
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "healthy"
    })


@app.post("/compile")
def compile_font():
    workdir = None

    try:
        font_name = request.form.get(
            "font_name",
            "Moonlight Color Alpha"
        ).strip()

        files = request.files.getlist("glyphs")
        characters = request.form.getlist("characters")

        if not files:
            return jsonify({
                "error": "No glyph images were uploaded."
            }), 400

        if len(characters) != len(files):
            return jsonify({
                "error": "Each PNG needs a character assignment."
            }), 400

        workdir = Path(tempfile.mkdtemp())

        source_dir = workdir / "source"
        output_dir = workdir / "output"

        source_dir.mkdir()
        output_dir.mkdir()

        svg_files = []

        for uploaded, character in zip(files, characters):

            if not character:
                continue

            codepoint = ord(character[0])

            png_name = f"emoji_u{codepoint:x}.png"
            png_path = source_dir / png_name

            uploaded.save(png_path)

            encoded = base64.b64encode(
                png_path.read_bytes()
            ).decode("ascii")

            svg_name = f"emoji_u{codepoint:x}.svg"
            svg_path = source_dir / svg_name

            svg = f"""<svg
xmlns="http://www.w3.org/2000/svg"
xmlns:xlink="http://www.w3.org/1999/xlink"
viewBox="0 0 1000 1000">

<image
width="1000"
height="1000"
preserveAspectRatio="xMidYMid meet"
href="data:image/png;base64,{encoded}"
xlink:href="data:image/png;base64,{encoded}"
/>

</svg>"""

            svg_path.write_text(
                svg,
                encoding="utf-8"
            )

            svg_files.append(
                str(svg_path)
            )

        if not svg_files:
            return jsonify({
                "error": "No valid characters were supplied."
            }), 400

        safe_name = "".join(
            c for c in font_name
            if c.isalnum() or c in ("-", "_")
        )

        if not safe_name:
            safe_name = "MoonlightColorAlpha"

        config = workdir / "config.toml"

        config.write_text(
            f'''
family = "{font_name}"
output_file = "{output_dir / (safe_name + ".ttf")}"
color_format = "glyf_colr_1"
''',
            encoding="utf-8"
        )

        command = [
            "nanoemoji",
            "--config_file",
            str(config),
            *svg_files
        ]

        result = subprocess.run(
            command,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode != 0:

            details = (
                result.stdout
                or result.stderr
                or "Unknown nanoemoji error."
            )

            return jsonify({
                "error": "Font compiler failed.",
                "details": details[-8000:]
            }), 500

        fonts = list(
            output_dir.glob("*.ttf")
        )

        if not fonts:
            fonts = list(
                workdir.rglob("*.ttf")
            )

        if not fonts:
            return jsonify({
                "error":
                "Compiler finished but no TTF was created.",
                "details":
                result.stdout[-8000:]
            }), 500

        finished_font = fonts[0]

        final_path = (
            Path(tempfile.gettempdir())
            / f"{safe_name}.ttf"
        )

        shutil.copyfile(
            finished_font,
            final_path
        )

        return send_file(
            final_path,
            mimetype="font/ttf",
            as_attachment=True,
            download_name=f"{safe_name}.ttf"
        )

    except subprocess.TimeoutExpired:

        return jsonify({
            "error":
            "Font compilation timed out."
        }), 504

    except Exception as exc:

        return jsonify({
            "error": str(exc)
        }), 500

    finally:

        if workdir and workdir.exists():

            shutil.rmtree(
                workdir,
                ignore_errors=True
            )


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
