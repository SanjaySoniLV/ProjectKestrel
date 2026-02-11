# Custom PyInstaller hook for cv2 to fix collection of binaries from python-3.x subdirectory
# Based on: https://github.com/pyinstaller/pyinstaller-hooks-contrib/issues/issues

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules, collect_data_files, get_module_file_attribute
import os
import sys

print("=" * 80, flush=True)
print("CUSTOM CV2 HOOK: Starting cv2 hook execution", flush=True)
print("=" * 80, flush=True)

# Collect submodules
hiddenimports = collect_submodules('cv2')
print(f"CUSTOM CV2 HOOK: Collected {len(hiddenimports)} hidden imports", flush=True)
print(f"CUSTOM CV2 HOOK: Hidden imports: {hiddenimports[:10]}...", flush=True)

# Collect data files (includes config.py and other necessary files)
datas = collect_data_files('cv2')
print(f"CUSTOM CV2 HOOK: Collected {len(datas)} data files", flush=True)
for data in datas[:10]:
    print(f"CUSTOM CV2 HOOK:   DATA: {data}", flush=True)
if len(datas) > 10:
    print(f"CUSTOM CV2 HOOK:   ... and {len(datas) - 10} more data files", flush=True)

# Collect binaries - standard location
binaries = collect_dynamic_libs('cv2')
print(f"CUSTOM CV2 HOOK: Standard collect_dynamic_libs found {len(binaries)} binaries", flush=True)
for binary in binaries[:5]:
    print(f"CUSTOM CV2 HOOK:   - {binary}", flush=True)
if len(binaries) > 5:
    print(f"CUSTOM CV2 HOOK:   ... and {len(binaries) - 5} more", flush=True)

# Try to collect from python-3.x subdirectory with specific patterns
print("CUSTOM CV2 HOOK: Attempting to collect from python-3.x subdirectory...", flush=True)
try:
    extra_binaries = collect_dynamic_libs('cv2', search_patterns=['cv2*.so', 'cv2*.pyd', 'cv2*.dylib'])
    print(f"CUSTOM CV2 HOOK: Pattern-based collection found {len(extra_binaries)} binaries", flush=True)
    for binary in extra_binaries[:5]:
        print(f"CUSTOM CV2 HOOK:   - {binary}", flush=True)
    if len(extra_binaries) > 5:
        print(f"CUSTOM CV2 HOOK:   ... and {len(extra_binaries) - 5} more", flush=True)
    binaries += extra_binaries
except Exception as e:
    print(f"CUSTOM CV2 HOOK: Error collecting extra binaries: {e}", flush=True)

# Try to manually find cv2 module location and look for python-3.x subdirectory
print("CUSTOM CV2 HOOK: Attempting manual search for cv2 binaries...", flush=True)
try:
    cv2_location = get_module_file_attribute('cv2')
    print(f"CUSTOM CV2 HOOK: cv2 module location: {cv2_location}", flush=True)
    
    if cv2_location:
        cv2_dir = os.path.dirname(cv2_location)
        print(f"CUSTOM CV2 HOOK: cv2 directory: {cv2_dir}", flush=True)
        
        # List contents of cv2 directory
        if os.path.isdir(cv2_dir):
            cv2_contents = os.listdir(cv2_dir)
            print(f"CUSTOM CV2 HOOK: cv2 directory contents ({len(cv2_contents)} items):", flush=True)
            for item in cv2_contents[:20]:
                item_path = os.path.join(cv2_dir, item)
                item_type = "DIR" if os.path.isdir(item_path) else "FILE"
                print(f"CUSTOM CV2 HOOK:   [{item_type}] {item}", flush=True)
            if len(cv2_contents) > 20:
                print(f"CUSTOM CV2 HOOK:   ... and {len(cv2_contents) - 20} more items", flush=True)
            
            # Look for python-3.x subdirectories
            python_dirs = [d for d in cv2_contents if d.startswith('python-') and os.path.isdir(os.path.join(cv2_dir, d))]
            print(f"CUSTOM CV2 HOOK: Found {len(python_dirs)} python-* directories: {python_dirs}", flush=True)
            
            for python_dir in python_dirs:
                python_dir_path = os.path.join(cv2_dir, python_dir)
                print(f"CUSTOM CV2 HOOK: Examining {python_dir_path}", flush=True)
                
                if os.path.isdir(python_dir_path):
                    python_dir_contents = os.listdir(python_dir_path)
                    print(f"CUSTOM CV2 HOOK:   Contents ({len(python_dir_contents)} items):", flush=True)
                    for item in python_dir_contents[:10]:
                        print(f"CUSTOM CV2 HOOK:     - {item}", flush=True)
                    if len(python_dir_contents) > 10:
                        print(f"CUSTOM CV2 HOOK:     ... and {len(python_dir_contents) - 10} more items", flush=True)
                    
                    # Look for .so, .pyd, .dylib files
                    for item in python_dir_contents:
                        item_path = os.path.join(python_dir_path, item)
                        if item.endswith(('.so', '.pyd', '.dylib')) and os.path.isfile(item_path):
                            # Add to binaries - destination should be cv2/python-X.Y/
                            dest_dir = os.path.join('cv2', python_dir)
                            binary_entry = (item_path, dest_dir)
                            binaries.append(binary_entry)
                            print(f"CUSTOM CV2 HOOK:   ADDED BINARY: {item_path} -> {dest_dir}", flush=True)
except Exception as e:
    print(f"CUSTOM CV2 HOOK: Error in manual search: {e}", flush=True)
    import traceback
    traceback.print_exc()

print(f"CUSTOM CV2 HOOK: Final binary count: {len(binaries)}", flush=True)
print(f"CUSTOM CV2 HOOK: Final data file count: {len(datas)}", flush=True)
print("=" * 80, flush=True)
print("CUSTOM CV2 HOOK: Finished cv2 hook execution", flush=True)
print("=" * 80, flush=True)
