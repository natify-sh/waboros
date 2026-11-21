#!/bin/sh
# gc_kotlin.wasm: a Wasm GC program from a real toolchain, Kotlin/Wasm (target wasm-wasi), then optimised
# with binaryen's wasm-opt as the Kotlin Gradle plugin does (-O3 --closed-world). Needs java 17+, mvn,
# curl, unzip; the work goes into ./gc_build (kept out of the repository). The Kotlin compiler and the wasi
# stdlib klib come from Maven Central; wasm-opt (binaryen 133) comes from the Dart SDK zip, pinned by sha256.
# Only the features Wasmtime has are enabled: `-all` makes binaryen 133 emit exact reference types
# (custom descriptors), which no engine of the corpus has.
set -e
cd "$(dirname "$0")"
V=2.4.21
mkdir -p gc_build && cd gc_build
if [ ! -d lib ]; then
  cat > pom.xml <<POM
<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
<groupId>x</groupId><artifactId>x</artifactId><version>1</version><dependencies>
<dependency><groupId>org.jetbrains.kotlin</groupId><artifactId>kotlin-compiler-embeddable</artifactId><version>$V</version></dependency>
<dependency><groupId>org.jetbrains.kotlin</groupId><artifactId>kotlin-stdlib-wasm-wasi</artifactId><version>$V</version><type>klib</type></dependency>
</dependencies></project>
POM
  mvn -q -B dependency:copy-dependencies -DoutputDirectory=lib
fi
if [ ! -x dart-sdk/bin/utils/wasm-opt ]; then
  curl -sSfL -o dart.zip https://storage.googleapis.com/dart-archive/channels/stable/release/3.14.0/sdk/dartsdk-linux-x64-release.zip
  echo "90f7fd75fe21e16437778df882f280910d9bbd4809c90024f21de82435dded1d  dart.zip" | sha256sum -c -
  unzip -q dart.zip dart-sdk/bin/utils/wasm-opt && rm dart.zip # only wasm-opt of the 500 MB
fi
CP=$(ls lib/*.jar | tr '\n' ':')
K="java -cp $CP org.jetbrains.kotlin.cli.js.K2JSCompiler -Xwasm -Xwasm-target=wasm-wasi -libraries lib/kotlin-stdlib-wasm-wasi-$V.klib"
$K -Xir-produce-klib-file -ir-output-dir out -ir-output-name gc_kotlin ../gc_kotlin.kt
$K -Xinclude=out/gc_kotlin.klib -Xir-produce-js -ir-output-dir out2 -ir-output-name gc_kotlin
dart-sdk/bin/utils/wasm-opt --enable-gc --enable-reference-types --enable-exception-handling \
  --enable-bulk-memory --enable-nontrapping-float-to-int --enable-sign-ext --enable-multivalue \
  --enable-mutable-globals --enable-tail-call -O3 --closed-world out2/gc_kotlin.wasm -o ../gc_kotlin.wasm
ls -l ../gc_kotlin.wasm
