import os
import sys
import subprocess
import numpy as np


def _find_magick_binary():
    """Find the ImageMagick 'magick' binary."""
    print(f"_find_magick_binary: Starting search...", flush=True)
    
    # In frozen builds, check sys._MEIPASS first
    if getattr(sys, 'frozen', False):
        meipass = sys._MEIPASS
        print(f"_find_magick_binary: Frozen build detected, MEIPASS={meipass}", flush=True)
        
        # Check various possible locations within the frozen bundle
        frozen_candidates = [
            os.path.join(meipass, "ImageMagick", "ImageMagick-7.0.10", "bin", "magick"),
            os.path.join(meipass, "ImageMagick-7.0.10", "bin", "magick"),
            os.path.join(meipass, "bin", "magick"),
        ]
        
        for candidate in frozen_candidates:
            print(f"_find_magick_binary: Checking frozen candidate: {candidate}", flush=True)
            if os.path.exists(candidate):
                print(f"_find_magick_binary: File exists, checking if executable", flush=True)
                # Print detailed file info
                try:
                    import stat
                    st = os.stat(candidate)
                    mode = st.st_mode
                    print(f"_find_magick_binary: File mode: {oct(stat.S_IMODE(mode))}", flush=True)
                    print(f"_find_magick_binary: Is executable: {os.access(candidate, os.X_OK)}", flush=True)
                except Exception as stat_exc:
                    print(f"_find_magick_binary: Could not stat file: {stat_exc}", flush=True)
                
                if os.access(candidate, os.X_OK):
                    print(f"Found magick binary at: {candidate}", flush=True)
                    return candidate
                else:
                    print(f"_find_magick_binary: File exists but not executable", flush=True)
            else:
                print(f"_find_magick_binary: File does not exist", flush=True)
    
    # Check MAGICK_HOME (set by runtime_hook.py in frozen builds)
    magick_home = os.environ.get("MAGICK_HOME")
    if magick_home:
        print(f"_find_magick_binary: MAGICK_HOME={magick_home}", flush=True)
        
        # Debug: Print directory tree of MAGICK_HOME
        if os.path.isdir(magick_home):
            print(f"_find_magick_binary: Directory tree of MAGICK_HOME:", flush=True)
            try:
                for root, dirs, files in os.walk(magick_home):
                    level = root.replace(magick_home, '').count(os.sep)
                    indent = ' ' * 2 * level
                    print(f"{indent}{os.path.basename(root)}/", flush=True)
                    subindent = ' ' * 2 * (level + 1)
                    for file in sorted(files)[:20]:  # Limit files per directory
                        print(f"{subindent}{file}", flush=True)
                    if len(files) > 20:
                        print(f"{subindent}... and {len(files) - 20} more files", flush=True)
                    # Limit depth to avoid too much output
                    if level >= 3:
                        dirs[:] = []
            except Exception as tree_exc:
                print(f"_find_magick_binary: Error printing tree: {tree_exc}", flush=True)
        
        # Try common locations within MAGICK_HOME
        candidates = [
            os.path.join(magick_home, "bin", "magick"),
            os.path.join(magick_home, "ImageMagick-7.0.10", "bin", "magick"),
            os.path.join(magick_home, "ImageMagick", "ImageMagick-7.0.10", "bin", "magick"),
        ]
        for magick_path in candidates:
            print(f"_find_magick_binary: Checking MAGICK_HOME candidate: {magick_path}", flush=True)
            if os.path.exists(magick_path):
                print(f"_find_magick_binary: File exists, checking if executable", flush=True)
                if os.access(magick_path, os.X_OK):
                    print(f"Found magick binary at: {magick_path}", flush=True)
                    return magick_path
                else:
                    print(f"_find_magick_binary: File exists but not executable", flush=True)
            else:
                print(f"_find_magick_binary: File does not exist", flush=True)
    
    # Try to find in PATH (works for both frozen and non-frozen)
    import shutil
    print(f"_find_magick_binary: Searching in PATH", flush=True)
    magick_path = shutil.which("magick")
    if magick_path:
        print(f"Found magick binary in PATH: {magick_path}", flush=True)
        return magick_path
    
    # Common installation paths for non-frozen builds
    common_paths = [
        "/usr/local/bin/magick",
        "/opt/homebrew/bin/magick",
        "/usr/bin/magick",
    ]
    
    print(f"_find_magick_binary: Checking common paths", flush=True)
    for path in common_paths:
        print(f"_find_magick_binary: Checking common path: {path}", flush=True)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            print(f"Found magick binary at: {path}", flush=True)
            return path
    
    print(f"_find_magick_binary: Failed to find magick binary anywhere", flush=True)
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
