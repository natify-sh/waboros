"""Makes the harness wheel a platform wheel, without a compiler: cibuildwheel refuses a wheel named
`...-none-any.whl`. The idea of wabt-package and wasm-tools-package: the distribution claims to have
extension modules, so the wheel gets bdist_wheel's own platform tag, cp3XY-cp3XY[t]-<platform>.
Not py3-none-<platform>: one job builds several Pythons, and cibuildwheel refuses a second wheel of
the same name in a run.
Unproven on Android, iOS and Pyodide; the fallback there is an empty C extension (ext-modules)."""

from setuptools import Distribution, setup
from setuptools.command.bdist_wheel import bdist_wheel


class PlatformDistribution(Distribution):
    def has_ext_modules(self):
        return True


class PlatformWheel(bdist_wheel):
    def finalize_options(self):
        super().finalize_options()
        self.root_is_pure = False


setup(distclass=PlatformDistribution, cmdclass={"bdist_wheel": PlatformWheel})
