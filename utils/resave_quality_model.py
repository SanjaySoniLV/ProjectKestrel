#!/usr/bin/env python3
"""
Resave the quality.keras model for improved macOS/XLA compatibility.

This script:
  1. Loads the existing quality model (compile=False to strip training artifacts).
  2. Runs a forward pass on a synthetic input to materialise all weights.
  3. Re-saves the model in the same .keras format.
  4. Renames the original model to quality_old.keras before writing the new one.

Usage
-----
Run from the repository root after installing requirements-macos.txt::

    python scripts/resave_quality_model.py

Optional arguments
------------------
  --model-dir  Path to the models directory (default: analyzer/models)
  --model-name Name for the model file to load and re-save (default: quality.keras)
  --old-name   Name for the backup of the original file (default: quality_old.keras)
"""

import argparse
import os
import shutil
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model-dir", default=os.path.join("analyzer", "models"),
                        help="Directory containing quality.keras (default: analyzer/models)")
    parser.add_argument("--model-name", default="quality.keras",
                        help="Model filename to load and re-save (default: quality.keras)")
    parser.add_argument("--old-name", default="quality_old.keras",
                        help="Backup filename for the original model (default: quality_old.keras)")
    args = parser.parse_args()

    model_dir = args.model_dir
    input_path = os.path.join(model_dir, args.model_name)
    old_path = os.path.join(model_dir, args.old_name)
    output_path = os.path.join(model_dir, args.model_name)  # overwrite in-place after rename

    if not os.path.exists(input_path):
        print(f"ERROR: Model not found at {input_path}", file=sys.stderr)
        sys.exit(1)

    # --- Import TensorFlow (import late so errors are readable) ---
    try:
        import tensorflow as tf
        import numpy as np
    except ImportError as exc:
        print(f"ERROR: Required package not available – {exc}", file=sys.stderr)
        print("Install dependencies:  pip install -r requirements-macos.txt", file=sys.stderr)
        sys.exit(1)

    print(f"TensorFlow version : {tf.__version__}")
    print(f"Keras version      : {tf.keras.__version__}")
    print()

    # --- Load model ---
    print(f"Loading model from  : {input_path}")
    model = tf.keras.models.load_model(input_path, compile=False)

    input_shape = model.input_shape  # e.g. (None, 1024, 1024, 1)
    print(f"Model input shape   : {input_shape}")
    print(f"Model output shape  : {model.output_shape}")
    print()

    # --- Synthetic forward pass to materialise weights ---
    # Replace the batch dimension (None) with 1; keep spatial/channel dims.
    concrete_shape = tuple(d if d is not None else 1 for d in input_shape)
    dummy_input = np.zeros(concrete_shape, dtype=np.float32)
    print(f"Running inference on synthetic input shape: {dummy_input.shape} ...")
    output = model(dummy_input, training=False)
    print(f"Inference output shape : {output.shape}")
    print(f"Inference output value : {output.numpy().reshape(-1)}")
    print()

    # --- Rename original → backup ---
    if os.path.exists(old_path):
        print(f"Removing existing backup at : {old_path}")
        if os.path.isdir(old_path):
            shutil.rmtree(old_path)
        else:
            os.remove(old_path)

    print(f"Renaming  {input_path}  →  {old_path}")
    os.rename(input_path, old_path)

    # --- Save re-serialised model ---
    print(f"Saving re-serialised model to : {output_path}")
    model.save(output_path)
    print()
    print("Done.")
    print(f"  New model  : {output_path}")
    print(f"  Backup     : {old_path}")


if __name__ == "__main__":
    main()
