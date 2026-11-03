"""Makes the harness wheel a platform wheel, without a compiler: cibuildwheel refuses a wheel named
`...-none-any.whl`. The idea of wabt-package and wasm-tools-package: the distribution claims to have
extension modules, and the wheel is tagged py3-none-<platform>.
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

    def get_tag(self):
        _, _, plat = super().get_tag()
        return "py3", "none", plat


setup(distclass=PlatformDistribution, cmdclass={"bdist_wheel": PlatformWheel})
