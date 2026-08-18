def prepare_png(uploaded_file):
    """
    Moonlight standardized glyph preparation.

    Expected source:
    - 5000 x 5000 px
    - transparent PNG
    - artwork already positioned correctly on its canvas

    IMPORTANT:
    We NEVER crop/trim transparent margins.
    Every letter keeps the exact same square relationship.
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

    # Reject completely invisible images.
    if image.getchannel("A").getbbox() is None:
        image.close()
        raise ValueError(
            f"{uploaded_file.filename or 'Uploaded file'} "
            "contains no visible artwork."
        )

    # --------------------------------------------------------
    # PRESERVE THE WHOLE SQUARE CANVAS
    # --------------------------------------------------------

    SOURCE_SIZE = 5000
    FONT_BITMAP_SIZE = 512

    # If a file somehow isn't 5000x5000, place the WHOLE
    # image onto a 5000x5000 transparent square.
    # Do not crop visible artwork or transparent margins.
    if image.size != (SOURCE_SIZE, SOURCE_SIZE):

        normalized = Image.new(
            "RGBA",
            (SOURCE_SIZE, SOURCE_SIZE),
            (0, 0, 0, 0)
        )

        scale = min(
            SOURCE_SIZE / image.width,
            SOURCE_SIZE / image.height
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
            (new_width, new_height),
            Image.Resampling.LANCZOS
        )

        x = (SOURCE_SIZE - new_width) // 2
        y = (SOURCE_SIZE - new_height) // 2

        normalized.alpha_composite(
            resized,
            (x, y)
        )

        resized.close()
        image.close()

        image = normalized

    # --------------------------------------------------------
    # FONT COPY
    #
    # Resize the COMPLETE 5000x5000 canvas.
    # Every letter therefore receives identical scaling.
    # --------------------------------------------------------

    font_image = image.resize(
        (FONT_BITMAP_SIZE, FONT_BITMAP_SIZE),
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
