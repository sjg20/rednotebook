# -----------------------------------------------------------------------
# Copyright (c) 2009-2023  Jendrik Seipp
#
# RedNotebook is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# RedNotebook is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with RedNotebook; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
# -----------------------------------------------------------------------

"""
This is the installation script for RedNotebook.

To install RedNotebook, run "pip install ." (note the dot).
"""

from pathlib import Path
import shutil
import sys

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py
from setuptools.command.install import install as _install

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from rednotebook import info

from dev import build_translations


TMP_LOCALE_DIR = REPO / "build" / "locale"


class build_py(_build_py):
    def run(self):
        build_translations.build_translation_files(REPO / "po", TMP_LOCALE_DIR)
        _build_py.run(self)


"""
We use the deprecated install class since it provides the easiest way to install
data files outside of a Python package. This feature is needed for the
translation files, which must reside in <sys.prefix>/share/locale for the Glade
file to pick them up.

An alternative would be to build the translation files with a separate command,
but that would require changing all package scripts for all distributions.
"""


class install(_install):
    def run(self):
        _install.run(self)
        for lang_dir in TMP_LOCALE_DIR.iterdir():
            lang = lang_dir.name
            lang_file = TMP_LOCALE_DIR / lang / "LC_MESSAGES" / "rednotebook.mo"
            dest_dir = (
                Path(self.install_data) / "share" / "locale" / lang / "LC_MESSAGES"
            )
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(lang_file, dest_dir / "rednotebook.mo")


parameters = {
    "name": "rednotebook",
    "version": info.version,
    "description": "Graphical daily journal with calendar, templates and keyword searching",
    "long_description": info.comments,
    "author": info.author,
    "author_email": info.author_mail,
    "maintainer": info.author,
    "maintainer_email": info.author_mail,
    "url": info.url,
    "license": "GPL",
    "keywords": "journal, diary",
    "cmdclass": {"build_py": build_py, "install": install},
    "scripts": ["rednotebook/rednotebook"],
    "packages": [
        "rednotebook",
        "rednotebook.external",
        "rednotebook.gui",
        "rednotebook.util",
    ],
    "package_data": {
        "rednotebook": [
            "images/*.png",
            "images/rednotebook-icon/*.png",
            "images/rednotebook-icon/rednotebook.svg",
            "files/*.cfg",
            "files/*.glade",
            "files/*.lang",
            "files/*.xml",
        ]
    },
    "data_files": [
        ("share/applications", ["data/rednotebook.desktop"]),
        (
            "share/icons/hicolor/scalable/apps",
            ["rednotebook/images/rednotebook-icon/rednotebook.svg"],
        ),
        ("share/metainfo", ["data/rednotebook.appdata.xml"]),
    ],
    "extras_require": {
        "dev_style": [
            "black==22.3.0",
            "flake8==4.0.1",
            "flake8-2020==1.6.0",
            "flake8-bugbear==21.11.28",
            "flake8-comprehensions==3.7.0",
            "flake8-executable==2.1.1",
            "isort==5.10.1",
            "pyupgrade==2.32.0",
            "vulture==1.6",
        ],
    },
}

if __name__ == "__main__":
    # Additionally use MANIFEST.in for image files
    setup(**parameters)
