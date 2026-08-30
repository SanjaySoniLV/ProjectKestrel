"""Unit tests for SpeciesNet taxonomy routing helpers."""

import unittest

from kestrel_analyzer.ml._speciesnet_ensemble import Classification

from kestrel_analyzer.ml.speciesnet_taxonomy import (
    bird_vs_wildlife_classifier_scores,
    format_species_display_name,
    is_ambiguous_generic_taxonomy,
    is_bird_taxon,
    is_ignored_prediction,
    route_prediction,
    route_with_classifier_tiebreak,
    wildlife_display_name,
)


class TestSpeciesNetTaxonomy(unittest.TestCase):
    def test_format_species_display_name(self):
        self.assertEqual(format_species_display_name("pronghorn"), "Pronghorn")
        self.assertEqual(format_species_display_name("CANE TOAD"), "Cane Toad")
        self.assertEqual(format_species_display_name("red-tailed hawk"), "Red-Tailed Hawk")

    def test_examples_ignore(self):
        self.assertTrue(is_ignored_prediction("f1856211-cfb7-4a5b-9158-c0f72fd09ee6;;;;;;blank"))
        self.assertTrue(is_ignored_prediction("e2895ed5-780b-48f6-8a11-9e27cb594511;;;;;;vehicle"))
        self.assertTrue(is_ignored_prediction(
            "990ae9dd-7a59-4344-afcb-1b7b21368000;mammalia;primates;hominidae;homo;sapiens;human"
        ))
        # Genus-only human (no species / common name) — must still be ignored
        self.assertTrue(is_ignored_prediction(
            "d126f0ec-82dd-46f0-b76a-5985ff6bbc9b;mammalia;primates;hominidae;homo;;"
        ))

    def test_examples_wildlife_display(self):
        raw = (
            "04eda76f-c0e7-4e9e-85c3-5b1542db2915;"
            "amphibia;anura;bufonidae;rhinella;marina;cane toad"
        )
        self.assertEqual(wildlife_display_name(raw), "Cane Toad")

    def test_bird_aves(self):
        raw = "b1352069-a39c-4a84-a949-60044271c5c1;aves;;;;;bird"
        self.assertTrue(is_bird_taxon(raw))
        route, label = route_prediction(raw, wildlife_enabled=True)
        self.assertEqual(route, "bird")
        self.assertEqual(label, "bird")

    def test_wildlife_route_when_enabled(self):
        raw = (
            "04eda76f-c0e7-4e9e-85c3-5b1542db2915;"
            "amphibia;anura;bufonidae;rhinella;marina;cane toad"
        )
        route, label = route_prediction(raw, wildlife_enabled=True)
        self.assertEqual(route, "wildlife")
        self.assertEqual(label, "Cane Toad")

    def test_wildlife_disabled_skips_non_bird(self):
        raw = (
            "04eda76f-c0e7-4e9e-85c3-5b1542db2915;"
            "amphibia;anura;bufonidae;rhinella;marina;cane toad"
        )
        route, label = route_prediction(raw, wildlife_enabled=False)
        self.assertEqual(route, "ignore")
        self.assertIsNone(label)

    def test_ambiguous_generic_animal_string(self):
        self.assertTrue(is_ambiguous_generic_taxonomy(Classification.ANIMAL.value))
        self.assertTrue(is_ambiguous_generic_taxonomy(Classification.UNKNOWN.value))

    def test_bird_vs_wildlife_scores(self):
        bird = "b1352069-a39c-4a84-a949-60044271c5c1;aves;;;;;bird"
        mammal = (
            "04eda76f-c0e7-4e9e-85c3-5b1542db2915;"
            "amphibia;anura;bufonidae;rhinella;marina;cane toad"
        )
        cls = {
            "classes": [Classification.ANIMAL.value, bird, mammal],
            "scores": [0.5, 0.45, 0.1],
        }
        bb, bo = bird_vs_wildlife_classifier_scores(cls)
        self.assertAlmostEqual(bb, 0.45)
        self.assertAlmostEqual(bo, 0.5)

    def test_tiebreak_prefers_bird_when_generic_animal(self):
        bird = "b1352069-a39c-4a84-a949-60044271c5c1;aves;;;;;bird"
        cls = {
            "classes": [Classification.ANIMAL.value, bird],
            "scores": [0.55, 0.6],
        }
        route, label, score = route_with_classifier_tiebreak(
            Classification.ANIMAL.value,
            0.55,
            cls,
            wildlife_enabled=True,
        )
        self.assertEqual(route, "bird")
        self.assertEqual(label, "bird")
        self.assertAlmostEqual(score, 0.6)

    def test_tiebreak_wildlife_disabled_generic_animal(self):
        bird = "b1352069-a39c-4a84-a949-60044271c5c1;aves;;;;;bird"
        cls = {
            "classes": [Classification.ANIMAL.value, bird],
            "scores": [0.55, 0.6],
        }
        route, label, score = route_with_classifier_tiebreak(
            Classification.ANIMAL.value,
            0.55,
            cls,
            wildlife_enabled=False,
        )
        self.assertEqual(route, "bird")
        self.assertEqual(label, "bird")

    def test_tiebreak_respects_species_level_non_bird(self):
        bird = "b1352069-a39c-4a84-a949-60044271c5c1;aves;;;;;bird"
        toad = (
            "04eda76f-c0e7-4e9e-85c3-5b1542db2915;"
            "amphibia;anura;bufonidae;rhinella;marina;cane toad"
        )
        cls = {
            "classes": [toad, bird],
            "scores": [0.8, 0.2],
        }
        route, label, score = route_with_classifier_tiebreak(
            toad,
            0.8,
            cls,
            wildlife_enabled=True,
        )
        self.assertEqual(route, "wildlife")
        self.assertEqual(label, "Cane Toad")
        self.assertAlmostEqual(score, 0.8)


if __name__ == "__main__":
    unittest.main()
