import os
import sys
import subprocess
import numpy as np


def _find_magick_binary():
    """Find the ImageMagick 'magick' binary."""
    # Check MAGICK_HOME first
    magick_home = os.environ.get("MAGICK_HOME")
    if magick_home:
        magick_path = os.path.join(magick_home, "bin", "magick")
        if os.path.isfile(magick_path) and os.access(magick_path, os.X_OK):
            print(f"Found magick binary at: {magick_path}", flush=True)
            return magick_path
    
    # Try to find in PATH
    import shutil
    magick_path = shutil.which("magick")
    if magick_path:
        print(f"Found magick binary in PATH: {magick_path}", flush=True)
        return magick_path
    
    # Common installation paths
    common_paths = [
        "/usr/local/bin/magick",
        "/opt/homebrew/bin/magick",
        "/usr/bin/magick",
    ]
    
    for path in common_paths:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            print(f"Found magick binary at: {path}", flush=True)
            return path
    
    raise RuntimeError("Could not find ImageMagick 'magick' binary")


def read_image(path: str):
    """
    Read an image using ImageMagick CLI via subprocess.
    Returns a numpy array or None on failure.
    """
    try:
        print(f"read_image: Finding magick binary...", flush=True)
        magick_bin = _find_magick_binary()
        print(f"read_image: Using magick binary: {magick_bin}", flush=True)
        
        # First, get the image dimensions after auto-orient
        print(f"read_image: Getting image dimensions for {path}", flush=True)
        result = subprocess.run(
            [magick_bin, path, "-auto-orient", "-format", "%w %h", "info:"],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode != 0:
            print(f"read_image: Failed to get dimensions. stderr: {result.stderr}", flush=True)
            return None
        
        dims = result.stdout.strip().split()
        if len(dims) != 2:
            print(f"read_image: Invalid dimension output: {result.stdout}", flush=True)
            return None
        
        try:
            width = int(dims[0])
            height = int(dims[1])
        except ValueError:
            print(f"read_image: Could not parse dimensions: {dims}", flush=True)
            return None
        
        print(f"read_image: Image dimensions: {width}x{height}", flush=True)
        
        if width == 0 or height == 0:
            print(f"read_image: Invalid dimensions", flush=True)
            return None
        
        # Now get the raw RGB pixel data
        print(f"read_image: Reading RGB pixel data...", flush=True)
        result = subprocess.run(
            [magick_bin, path, "-auto-orient", "-depth", "8", "RGB:-"],
            capture_output=True,
            timeout=60
        )
        
        if result.returncode != 0:
            print(f"read_image: Failed to read pixel data. stderr: {result.stderr.decode('utf-8', errors='ignore')}", flush=True)
            return None
        
        # Convert raw bytes to numpy array
        expected_size = width * height * 3
        if len(result.stdout) != expected_size:
            print(f"read_image: Unexpected data size. Expected {expected_size}, got {len(result.stdout)}", flush=True)
            return None
        
        pixel_data = np.frombuffer(result.stdout, dtype=np.uint8)
        pixel_data = pixel_data.reshape((height, width, 3))
        
        print(f"read_image: Successfully read image, shape={pixel_data.shape}", flush=True)
        return pixel_data
        
    except subprocess.TimeoutExpired:
        print(f"read_image: Timeout reading {path}", flush=True)
        return None
    except Exception as e:
        # Print verbose exception info for diagnostics
        import traceback
        print(f"Error in read_image({path}): {e}", flush=True)
        traceback.print_exc()
        return None
