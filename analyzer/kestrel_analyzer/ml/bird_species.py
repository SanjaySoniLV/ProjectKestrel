from pathlib import Path
import cv2
import numpy as np
import pandas as pd

from ..config import MODELS_DIR
from . import gpu_providers


class BirdSpeciesClassifier:
    def __init__(self, model_path: str, labels_path: str, use_gpu: bool, models_dir: str | None = None):
        try:
            import onnxruntime as ort
        except ImportError as e:
            raise RuntimeError(
                f"Failed to import onnxruntime: {e}\n"
                "Try reinstalling: pip uninstall onnxruntime; pip install onnxruntime"
            ) from e
        with open(labels_path, "r", encoding="utf-8-sig") as f:
            self.labels = np.array([l.strip() for l in f.readlines()])
        providers = gpu_providers() if use_gpu else ["CPUExecutionProvider"]
        try:
            self.session = ort.InferenceSession(model_path, providers=providers)
        except Exception as e:
            print(f"Warning: Failed to load ONNX model with specified providers: {e}")
            self.session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        active = self.session.get_providers()
        print(f"[BirdSpeciesClassifier] Active provider: {active[0] if active else 'unknown'}  all providers: {active}")

        try:
            base_dir = Path(models_dir) if models_dir else MODELS_DIR
            df_sf = pd.read_csv(base_dir / "labels_scispecies.csv")
            df_disp = pd.read_csv(base_dir / "scispecies_dispname.csv")
        except Exception as e:
            print(f"Failed to load family mapping CSVs: {e}")
            self.family_matrix = np.zeros((0, len(self.labels)), dtype=np.float32)
            self.family_display_names = []
            return

        species_to_family = dict(zip(df_sf["Species"], df_sf["Scientific Family"]))
        family_to_display = dict(zip(df_disp["Scientific Family"], df_disp["Display Name"]))

        display_families = []
        unknown_family_name = "Unknown Family"
        for sp in self.labels:
            fam = species_to_family.get(sp)
            if fam is None:
                display_families.append(unknown_family_name)
            else:
                display_families.append(family_to_display.get(fam, fam))
        display_families = np.array(display_families)

        _, unique_indices = np.unique(display_families, return_index=True)
        ordered_unique_fams = display_families[np.sort(unique_indices)]
        self.family_display_names = ordered_unique_fams.tolist()

        fam_index_map = {fam: i for i, fam in enumerate(self.family_display_names)}
        fam_indices = np.array([fam_index_map[f] for f in display_families])
        num_fams = len(self.family_display_names)
        num_species = len(self.labels)
        family_matrix = np.zeros((num_fams, num_species), dtype=np.float32)
        family_matrix[fam_indices, np.arange(num_species)] = 1.0
        self.family_matrix = family_matrix
        self._species_family_display = display_families

    @staticmethod
    def _preprocess(image):
        image = cv2.resize(image, dsize=(300, 300)).astype(np.float32)
        image = np.transpose(image, (2, 0, 1))
        return np.expand_dims(image, 0)

    def classify(self, image, top_k=5):
        input_tensor = self._preprocess(image)
        input_name = self.session.get_inputs()[0].name
        outputs = self.session.run(None, {input_name: input_tensor})
        logits = outputs[0][0]

        top_species_indices = np.argsort(logits)[-top_k:][::-1]
        top_species_labels = self.labels[top_species_indices]
        top_species_scores = logits[top_species_indices].astype(float)

        if self.family_matrix.shape[0] > 0:
            family_probs = self.family_matrix @ logits
            top_family_indices = np.argsort(family_probs)[-top_k:][::-1]
            top_family_labels = [self.family_display_names[i] for i in top_family_indices]
            top_family_scores = family_probs[top_family_indices].astype(float).tolist()
        else:
            top_family_labels, top_family_scores = [], []

        return {
            "top_species_labels": top_species_labels,
            "top_species_scores": top_species_scores,
            "top_family_labels": top_family_labels,
            "top_family_scores": top_family_scores,
        }
