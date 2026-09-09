"""Generate shipped ICO/PNG resources from the approved assets/logo.svg."""

from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ASSETS = Path(__file__).resolve().parents[1] / "assets"
SIZES = (16, 20, 24, 32, 48, 64, 72, 96, 128, 256)


def main() -> None:
    renderer = QSvgRenderer(str(ASSETS / "logo.svg"))
    if not renderer.isValid():
        raise ValueError("Invalid assets/logo.svg")
    with TemporaryDirectory(prefix="fluentytdl-icons-") as directory:
        staging = Path(directory)
        frames = {}
        for size in SIZES:
            image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            try:
                renderer.render(painter)
            finally:
                painter.end()
            path = staging / f"{size}.png"
            if not image.save(str(path)):
                raise OSError(f"Could not render {size}px icon")
            with Image.open(path) as rendered:
                frames[size] = rendered.convert("RGBA")
        icon_path = staging / "FluentYTDL_v2.ico"
        frames[256].save(
            icon_path,
            format="ICO",
            sizes=[(size, size) for size in SIZES],
            append_images=[frames[size] for size in SIZES if size != 256],
        )
        with Image.open(icon_path) as icon:
            if icon.ico.sizes() != {(size, size) for size in SIZES}:
                raise ValueError("ICO does not contain all requested sizes")
        outputs = {"FluentYTDL_v2.ico": icon_path, "logo.png": staging / "256.png"}
        for size in (16, 32, 64, 128):
            outputs[f"logo_{size}.png"] = staging / f"{size}.png"
        bounds = frames[256].getbbox()
        if bounds is None:
            raise ValueError("SVG rendered an empty image")
        # Keep a square, tightly cropped legacy fallback from the same artwork.
        crop = frames[256].crop(bounds)
        scale = 256 / max(crop.size)
        crop = crop.resize(
            (round(crop.width * scale), round(crop.height * scale)), Image.Resampling.LANCZOS
        )
        tight = Image.new("RGBA", (256, 256))
        tight.paste(crop, ((256 - crop.width) // 2, (256 - crop.height) // 2))
        tight_path = staging / "logo_tight.png"
        tight.save(tight_path)
        outputs["logo_tight.png"] = tight_path
        for name, source in outputs.items():
            (ASSETS / name).write_bytes(source.read_bytes())
    print(f"Generated {len(outputs)} assets from logo.svg; ICO contains {len(SIZES)} sizes.")


if __name__ == "__main__":
    main()
