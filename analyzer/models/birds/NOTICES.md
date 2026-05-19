# Bundled bird-catalog data sources

The catalog file ``birds_global.csv`` is built from the following
authoritative sources. Attribution required by license / convention is
listed below.

## IOC World Bird List (v15.1)

Frank Gill, David Donsker & Pamela Rasmussen (Eds). 2025. *IOC World Bird List* (v15.1). doi:10.14344/IOC.ML.15.1. https://www.worldbirdnames.org/

Licensed under [Creative Commons Attribution 3.0 Unported](https://creativecommons.org/licenses/by/3.0/).
Common names, scientific binomials, taxonomic order/family/genus, and breeding-range biogeographic codes are derived from this source.

## IBP-AOS Alpha Codes

Pyle, P. and DeSante, D.F. *Four-letter (English Name) and Six-letter (Scientific Name) Alpha Codes for North American Birds.* The Institute for Bird Populations. https://www.birdpop.org/

Per the 66th AOS Supplement (2025). 4-letter codes are reproduced as
factual abbreviations; full attribution is preserved here in lieu of a
publicly documented license.

## ProjectKestrel additions

* Hand-curated AOS-to-IOC name overrides for model species whose
  preferred English name differs between authorities (see
  ``tools/build_bird_catalog.py``, ``AOS_TO_IOC_OVERRIDES``).
* Family display names (``family_common``) preserve the existing ``analyzer/models/scispecies_dispname.csv`` mapping where present and fall back to IOC's *Family (English)* otherwise.
