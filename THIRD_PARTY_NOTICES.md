# Third-Party Notices

This file lists the open-source Python packages installed in this project's
virtual environment, and the licence each one is distributed under. It
supports Section 13 of [`licence.md`](licence.md), which covers third-party
material bundled into the Software.

These packages are installed via `pip` from PyPI (see
`utility/requirements.txt`) and are not vendored copies within this
repository; each remains under its own licence, reproduced or linked below
as that licence requires.

Generated on 2026-09-21 using [`pip-licenses`](https://github.com/raimon49/pip-licenses):

```
pip-licenses --format=markdown --with-urls --order=license
```

Re-run that command and replace the table below whenever dependencies
change, so this file stays current.

| Name | Version | License | URL |
|------|---------|---------|-----|
| bcrypt | 5.0.0 | Apache Software License | https://github.com/pyca/bcrypt/ |
| requests | 2.32.5 | Apache Software License | https://requests.readthedocs.io |
| python-dateutil | 2.9.0.post0 | Apache Software License; BSD License | https://github.com/dateutil/dateutil |
| packaging | 26.2 | Apache-2.0 OR BSD-2-Clause | https://github.com/pypa/packaging |
| cryptography | 46.0.4 | Apache-2.0 OR BSD-3-Clause | https://github.com/pyca/cryptography |
| Flask-Mail | 0.10.0 | BSD License | https://github.com/pallets-eco/flask-mail/ |
| Flask-SQLAlchemy | 3.1.1 | BSD License | https://flask-sqlalchemy.palletsprojects.com |
| Flask-WTF | 1.2.2 | BSD License | https://flask-wtf.readthedocs.io/ |
| Jinja2 | 3.1.6 | BSD License | https://github.com/pallets/jinja/ |
| Pygments | 2.19.2 | BSD License | https://pygments.org |
| WTForms | 3.2.1 | BSD License | https://wtforms.readthedocs.io |
| contourpy | 1.3.2 | BSD License | https://github.com/contourpy/contourpy |
| cycler | 0.12.1 | BSD License | https://matplotlib.org/cycler/ |
| itsdangerous | 2.2.0 | BSD License | https://github.com/pallets/itsdangerous/ |
| kiwisolver | 1.5.0 | BSD License | https://github.com/nucleic/kiwi |
| numpy | 2.2.6 | BSD License | https://numpy.org |
| Flask | 3.1.2 | BSD-3-Clause | https://github.com/pallets/flask/ |
| Markdown | 3.10.3 | BSD-3-Clause | https://Python-Markdown.github.io/ |
| MarkupSafe | 3.0.3 | BSD-3-Clause | https://github.com/pallets/markupsafe/ |
| Werkzeug | 3.1.5 | BSD-3-Clause | https://github.com/pallets/werkzeug/ |
| click | 8.3.1 | BSD-3-Clause | https://github.com/pallets/click/ |
| idna | 3.11 | BSD-3-Clause | https://github.com/kjd/idna |
| lxml | 6.0.2 | BSD-3-Clause | https://lxml.de/ |
| pycparser | 3.0 | BSD-3-Clause | https://github.com/eliben/pycparser |
| python-dotenv | 1.2.1 | BSD-3-Clause | https://github.com/theskumar/python-dotenv |
| websockets | 16.0 | BSD-3-Clause | https://github.com/python-websockets/websockets |
| PyJWT | 2.11.0 | MIT | https://github.com/jpadilla/pyjwt |
| SQLAlchemy | 2.0.46 | MIT | https://www.sqlalchemy.org |
| cffi | 2.0.0 | MIT | https://cffi.readthedocs.io/en/latest/whatsnew.html |
| charset-normalizer | 3.4.4 | MIT | https://github.com/jawah/charset_normalizer/blob/master/CHANGELOG.md |
| fonttools | 4.62.1 | MIT | http://github.com/fonttools/fonttools |
| pyparsing | 3.3.2 | MIT | https://github.com/pyparsing/pyparsing/ |
| spiceypy | 8.1.0 | MIT | https://github.com/AndrewAnnex/SpiceyPy |
| urllib3 | 2.6.3 | MIT | https://github.com/urllib3/urllib3/blob/main/CHANGES.rst |
| greenlet | 3.3.1 | MIT AND Python-2.0 | https://greenlet.readthedocs.io |
| Flask-Login | 0.6.3 | MIT License | https://github.com/maxcountryman/flask-login |
| blinker | 1.9.0 | MIT License | https://github.com/pallets-eco/blinker/ |
| python-docx | 1.2.0 | MIT License | https://github.com/python-openxml/python-docx |
| six | 1.17.0 | MIT License | https://github.com/benjaminp/six |
| pillow | 12.1.0 | MIT-CMU | https://python-pillow.github.io |
| certifi | 2026.1.4 | Mozilla Public License 2.0 (MPL 2.0) | https://github.com/certifi/python-certifi |
| typing_extensions | 4.15.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| matplotlib | 3.10.9 | Python Software Foundation License | https://matplotlib.org |
| ujson | 5.11.0 | UNKNOWN | https://github.com/ultrajson/ultrajson |
| waitress | 3.0.2 | Zope Public License | https://github.com/Pylons/waitress |

## Notes

- No GPL- or AGPL-licensed package was found in this scan. If a future
  dependency shows GPL/AGPL here, check it against Section 3 of the Licence
  (non-commercial, no public hosting) before relying on it, as some GPL
  terms can conflict with those restrictions.
- `ujson`'s licence metadata reports as `UNKNOWN` in this scan; it is in fact
  distributed under a permissive BSD-style licence, but this has not been
  independently re-verified here — check its repository if that matters for
  your use.
- Some packages listed above (e.g. `matplotlib`, `python-docx`, `numpy`,
  `contourpy`, `cycler`, `kiwisolver`, `fonttools`, `pyparsing`, `six`) are
  not direct entries in `utility/requirements.txt`; they were present in the
  virtual environment this scan was run against, either as transitive
  dependencies or leftovers from other tooling. Re-run the scan against a
  clean environment built only from `utility/requirements.txt` if you need
  an exact minimal list.
