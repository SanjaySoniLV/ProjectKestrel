import ctypes
import os
import sys
import numpy as np


def _get_magickwand_library():
    """Load the MagickWand library using ctypes."""
    # On Unix systems, we need RTLD_GLOBAL so MagickWand can find MagickCore symbols
    if sys.platform != "win32":
        # RTLD flags are in os module, not ctypes
        mode = os.RTLD_GLOBAL | os.RTLD_NOW
    else:
        mode = ctypes.DEFAULT_MODE
    
    # Try to get the library path from environment variable
    magickwand_path = os.environ.get("MAGICKWAND_LIBRARY")
    
    if magickwand_path and os.path.exists(magickwand_path):
        print(f"Loading MagickWand from MAGICKWAND_LIBRARY: {magickwand_path}", flush=True)
        return ctypes.CDLL(magickwand_path, mode=mode)
    
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
            print(f"Trying to load MagickWand library: {lib_name}", flush=True)
            lib = ctypes.CDLL(lib_name, mode=mode)
            print(f"Successfully loaded: {lib_name}", flush=True)
            return lib
        except OSError as e:
            print(f"Failed to load {lib_name}: {e}", flush=True)
            continue
    
    raise RuntimeError("Could not load MagickWand library")


def read_image(path: str):
    """
    Read an image using direct ctypes calls to MagickWand library.
    Returns a numpy array or None on failure.
    """
    try:
        print(f"read_image: Loading MagickWand library...", flush=True)
        lib = _get_magickwand_library()
        print(f"read_image: Library loaded successfully", flush=True)
        
        # Define MagickWand API functions
        print(f"read_image: Setting up function signatures...", flush=True)
        
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
        
        # Error handling functions
        lib.MagickGetException.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        lib.MagickGetException.restype = ctypes.c_char_p
        
        lib.MagickRelinquishMemory.argtypes = [ctypes.c_void_p]
        lib.MagickRelinquishMemory.restype = ctypes.c_void_p
        
        print(f"read_image: Calling MagickWandGenesis...", flush=True)
        # Initialize MagickWand environment
        lib.MagickWandGenesis()
        print(f"read_image: MagickWandGenesis completed", flush=True)
        
        print(f"read_image: Creating new wand...", flush=True)
        # Create a new wand
        wand = lib.NewMagickWand()
        if not wand:
            print(f"read_image: NewMagickWand returned NULL", flush=True)
            return None
        print(f"read_image: Wand created: {hex(wand)}", flush=True)
        
        try:
            # Read the image
            path_bytes = path.encode('utf-8')
            print(f"read_image: About to call MagickReadImage with path: {path}", flush=True)
            print(f"read_image: path_bytes = {path_bytes}", flush=True)
            print(f"read_image: wand = {hex(wand)}", flush=True)
            
            status = lib.MagickReadImage(wand, path_bytes)
            print(f"read_image: MagickReadImage returned status: {status}", flush=True)
            
            if not status:
                # Get error message
                severity = ctypes.c_int()
                error_msg_ptr = lib.MagickGetException(wand, ctypes.byref(severity))
                if error_msg_ptr:
                    error_msg = error_msg_ptr.decode('utf-8', errors='ignore')
                    lib.MagickRelinquishMemory(error_msg_ptr)
                    print(f"read_image: MagickReadImage failed with error: {error_msg}", flush=True)
                else:
                    print(f"read_image: MagickReadImage failed (no error message)", flush=True)
                return None
            
            print(f"read_image: Image read successfully, applying auto-orient...", flush=True)
            # Auto-orient the image (handles EXIF orientation)
            lib.MagickAutoOrientImage(wand)
            
            print(f"read_image: Getting image dimensions...", flush=True)
            # Get image dimensions
            width = lib.MagickGetImageWidth(wand)
            height = lib.MagickGetImageHeight(wand)
            print(f"read_image: Image dimensions: {width}x{height}", flush=True)
            
            if width == 0 or height == 0:
                print(f"read_image: Invalid dimensions", flush=True)
                return None
            
            print(f"read_image: Exporting pixels to numpy array...", flush=True)
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
                print(f"read_image: MagickExportImagePixels failed", flush=True)
                return None
            
            print(f"read_image: Successfully exported pixels", flush=True)
            return pixel_data
            
        finally:
            # Clean up
            print(f"read_image: Cleaning up wand...", flush=True)
            lib.DestroyMagickWand(wand)
            lib.MagickWandTerminus()
            print(f"read_image: Cleanup complete", flush=True)
            
    except Exception as e:
        # Print verbose exception info for diagnostics
        import traceback
        print(f"Error in read_image({path}): {e}", flush=True)
        traceback.print_exc()
        return None
