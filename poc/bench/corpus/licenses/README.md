# Licences of the corpus sources

Sources are fetched by `build.sh` (not stored here); the built `.wasm` files contain compiled code of:

| library | version | licence |
|---|---|---|
| Monocypher | 4.0.2 | BSD-2-Clause or CC0-1.0 (`monocypher-LICENCE.md`) |
| BLAKE3 (portable C) | 1.5.4 | CC0-1.0 or Apache-2.0 (`blake3-LICENSE_CC0`, `blake3-LICENSE_A2`) |
| miniz | 3.0.2 | MIT (`miniz-LICENSE`) |
| xxHash | 0.8.2 | BSD-2-Clause (`xxhash-LICENSE`; the library) |
| QOI (`qoi.h`) | master, content pinned by sha256 | MIT (`qoi-LICENSE`) |
| crypto-algorithms (SHA-256) | master, content pinned by sha256 | public domain (`crypto-algorithms-README.md`) |

`../blake3_vectors.json` is a subset of `test_vectors/test_vectors.json` of BLAKE3 1.5.4 (the first 32 bytes of
`hash`, for 11 input lengths).

`../xxh_vectors.json` holds values of xxHash 0.8.2 built natively once (inputs `(i*31+7)&255`); `../qoi_test.qoi` is a
64x48 RGBA image written by the reference QOI encoder, its expected decoder checksum is in `tools/run_corpus.py`.
