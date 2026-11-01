"""Bundle the canonical repository skills in wheels without duplicate source copies."""
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithSkills(build_py):
    def run(self):
        super().run()
        source = Path(__file__).parent / "skills"
        target = Path(self.build_lib) / "luma" / "assets" / "skills"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)


setup(cmdclass={"build_py": BuildWithSkills})
