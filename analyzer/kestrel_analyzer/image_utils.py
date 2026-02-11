import ctypes
import os
import sys
import numpy as np


def _get_magickwand_library():
    """Load the MagickWand library using ctypes."""
    # Try to get the library path from environment variable
    magickwand_path = os.environ.get("MAGICKWAND_LIBRARY")
    
    if magickwand_path and os.path.exists(magickwand_path):
        return ctypes.CDLL(magickwand_path)
    
    # Try common library names
    library_names = []
    if sys.platform == "darwin":  # macOS
        library_names = [
            "libMagickWand-7.Q16HDRI.dylib",
            "libMagickWand-7.dylib",
            "libMagickWand.dylib"
        ]
    elif sys.platform == "win32":  # Windows
        library_names = [
            "CORE_RL_MagickWand_.dll",
            "libMagickWand-7.Q16HDRI.dll"
        ]
    else:  # Linux
        library_names = [
            "libMagickWand-7.Q16HDRI.so",
            "libMagickWand-7.so",
            "libMagickWand.so"
        ]
    
    for lib_name in library_names:
        try:
            return ctypes.CDLL(lib_name)
        except OSError:
            continue
    
    raise RuntimeError("Could not load MagickWand library")


def read_image(path: str):
    """
    Read an image using direct ctypes calls to MagickWand library.
    Returns a numpy array or None on failure.
    """
    try:
        lib = _get_magickwand_library()
        
        # Define MagickWand API functions
        lib.MagickWandGenesis.argtypes = []
        lib.MagickWandGenesis.restype = None
        
        lib.NewMagickWand.argtypes = []
        lib.NewMagickWand.restype = ctypes.c_void_p
        
        lib.MagickReadImage.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.MagickReadImage.restype = ctypes.c_int
        
        lib.MagickGetImageWidth.argtypes = [ctypes.c_void_p]
        lib.MagickGetImageWidth.restype = ctypes.c_size_t
        
        lib.MagickGetImageHeight.argtypes = [ctypes.c_void_p]
        lib.MagickGetImageHeight.restype = ctypes.c_size_t
        
        lib.MagickAutoOrientImage.argtypes = [ctypes.c_void_p]
        lib.MagickAutoOrientImage.restype = ctypes.c_int
        
        lib.MagickExportImagePixels.argtypes = [
            ctypes.c_void_p,  # wand
            ctypes.c_ssize_t,  # x
            ctypes.c_ssize_t,  # y
            ctypes.c_size_t,   # columns
            ctypes.c_size_t,   # rows
            ctypes.c_char_p,   # map
            ctypes.c_int,      # storage type (CharPixel=1, ShortPixel=2, IntegerPixel=3, etc.)
            ctypes.c_void_p    # pixels
        ]
        lib.MagickExportImagePixels.restype = ctypes.c_int
        
        lib.DestroyMagickWand.argtypes = [ctypes.c_void_p]
        lib.DestroyMagickWand.restype = ctypes.c_void_p
        
        lib.MagickWandTerminus.argtypes = []
        lib.MagickWandTerminus.restype = None
        
        # Initialize MagickWand environment
        lib.MagickWandGenesis()
        
        # Create a new wand
        wand = lib.NewMagickWand()
        if not wand:
            return None
        
        try:
            # Read the image
            path_bytes = path.encode('utf-8')
            status = lib.MagickReadImage(wand, path_bytes)
            if not status:
                return None
            
            # Auto-orient the image (handles EXIF orientation)
            lib.MagickAutoOrientImage(wand)
            
            # Get image dimensions
            width = lib.MagickGetImageWidth(wand)
            height = lib.MagickGetImageHeight(wand)
            
            if width == 0 or height == 0:
                return None
            
            # Export pixels as RGB (CharPixel = 1 for unsigned char)
            pixel_data = np.zeros((height, width, 3), dtype=np.uint8)
            status = lib.MagickExportImagePixels(
                wand,
                0,  # x
                0,  # y
                width,
                height,
                b"RGB",  # map
                1,  # CharPixel
                pixel_data.ctypes.data_as(ctypes.c_void_p)
            )
            
            if not status:
                return None
            
            return pixel_data
            
        finally:
            # Clean up
            lib.DestroyMagickWand(wand)
            lib.MagickWandTerminus()
            
    except Exception as e:
        # Log the error if needed, but return None to match original behavior
        return None
