import numpy as np


def read_image(path: str):
    try:
        from wand.image import Image as WandImage
        with WandImage(filename=path) as img:
            if img.orientation == "left_bottom":
                img.rotate(270)
            elif img.orientation == "right_bottom":
                img.rotate(90)
            elif img.orientation == "bottom":
                img.rotate(180)
            return np.array(img)
    except Exception:
        return None
