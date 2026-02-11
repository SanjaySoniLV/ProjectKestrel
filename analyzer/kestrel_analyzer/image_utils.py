import os
import sys
import numpy as np
import rawpy
from PIL import Image


def read_image(path: str):
    """
    Read an image using rawpy for RAW files or PIL for standard formats.
    Returns a numpy array in RGB format (H, W, 3) or None on failure.
    """
    try:
        print(f"read_image: Reading {path}", flush=True)
        
        # Determine file type by extension
        ext = os.path.splitext(path)[1].lower()
        
        # RAW formats supported by rawpy
        raw_extensions = {'.cr2', '.cr3', '.nef', '.arw', '.dng', '.raf', '.orf', '.rw2', '.srw'}
        
        if ext in raw_extensions:
            # Use rawpy for RAW files
            print(f"read_image: Detected RAW file, using rawpy", flush=True)
            with rawpy.imread(path) as raw:
                # postprocess() applies demosaicing, white balance, color correction, etc.
                # Returns numpy array in RGB format
                rgb = raw.postprocess()
            print(f"read_image: Successfully read RAW image, shape={rgb.shape}", flush=True)
            return rgb
        else:
            # Use PIL for standard image formats (JPEG, PNG, TIFF, etc.)
            print(f"read_image: Using PIL for standard image format", flush=True)
            img = Image.open(path)
            
            # Handle EXIF orientation
            from PIL import ImageOps
            img = ImageOps.exif_transpose(img)
            
            # Convert to RGB (handles grayscale, RGBA, etc.)
            if img.mode != 'RGB':
                print(f"read_image: Converting from {img.mode} to RGB", flush=True)
                img = img.convert('RGB')
            
            # Convert to numpy array
            rgb = np.array(img)
            print(f"read_image: Successfully read image, shape={rgb.shape}", flush=True)
            return rgb
            
    except rawpy.LibRawFileUnsupportedError:
        print(f"read_image: RAW format not supported for {path}", flush=True)
        return None
    except rawpy.LibRawIOError as e:
        print(f"read_image: I/O error reading RAW file {path}: {e}", flush=True)
        return None
    except Exception as e:
        import traceback
        print(f"Error in read_image({path}): {e}", flush=True)
        traceback.print_exc()
        return None
