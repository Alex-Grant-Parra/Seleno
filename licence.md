# SELENO Personal Use Licence (SPUL) v1.0

Copyright (c) 2026 Alex Grant-Parra

Last Updated: 21st of September 2026

This is a **source-available** licence, not an open-source licence as defined
by the Open Source Initiative: it restricts commercial use and public
hosting, which open-source licences are not permitted to do. Treat it
accordingly.

---

## 1. Definitions

- **"Software"** refers to all source code, binaries, documentation, and associated materials provided under this licence, **excluding any Third Party Component identified in Section 13**, which remains licensed solely under its own terms.
- **"Third Party Component"** refers to any material identified in Section 13 as originating from, or licensed by, a party other than the Author.
- **"Author"** refers to Alex Grant-Parra.
- **"Official Service"** refers to any server, API, or infrastructure operated by the Author.
- **"Modified Version"** refers to any version of the Software that has been altered from its original form.
- **"Public Service"** refers to any server, platform, or system made accessible to users beyond a single private individual, or beyond the personnel of a single organisation (for example, a school, club, or astronomical society) operating the Software for that organisation's own internal, non-commercial use.
- **"Contact Channel"** refers to the Contact Us ticket system operated by the Author at <https://seleno.org/contact>, which is the Author's designated means of contact for the purposes of this licence.

---

## 2. Permitted Use

You are granted a non-exclusive, non-transferable, revocable licence to:

- Use the Software for personal, private, and non-commercial purposes, including on behalf of a single organisation (such as a school, club, or astronomical society) for that organisation's own internal, non-commercial use.
- Operate the Software on private systems, including private telescope setups or private telescope networks.
- Study, modify, and adapt the Software for your own use, or for the internal use of that single organisation.

This licence does not grant any right to redistribute the Software to any other individual or organisation. See Section 3.3.

---

## 3. Restrictions

You may NOT:

### 3.1 Public Hosting
- Host, deploy, or operate the Software (or any derivative of it) as a Public Service without explicit written permission from the Author.

### 3.2 Commercial Use
- Use the Software, or any part of it, for commercial purposes without prior written permission from the Author.
- Sell, sublicense, or monetise the Software or any derivative works.

### 3.3 Redistribution
- Redistribute, share, sublicense, or otherwise make available the Software, or any Modified Version of it, to any other individual or organisation, in whole or in part, in any form.
- This restriction applies regardless of whether the redistribution is commercial or non-commercial, and regardless of whether this licence and attribution are included with it.

### 3.4 Official Service Access
- Use Modified Versions of the Software to access or interact with the Official Service unless explicitly permitted by the Author.

### 3.5 Circumvention
- Attempt to bypass, disable, or interfere with any technical measures enforcing these restrictions.

---

## 4. Modifications

You are permitted to modify the Software for your own personal use, or for the internal use of the single organisation described in Section 2. However:

- Modified Versions must not be used to access the Official Service unless authorised.
- Modified Versions must not be redistributed to any other individual or organisation (Section 3.3).

---

## 5. Attribution

Any copy of the Software you hold or operate, including Modified Versions, must retain:

- The original copyright notice
- A reference to this licence
- Clear acknowledgement of the original Author

---

## 6. Derivative Works

Any derivative works (Modified Versions) based on the Software must:

- Remain governed by this same licence (SPUL v1.0)
- Not be redistributed to any other individual or organisation (Section 3.3)
- Not grant additional permissions beyond those defined here

---

## 7. Commercial Licensing

For commercial use, licensing, or partnerships, contact the Author via the Contact Channel: <https://seleno.org/contact>.

The Author reserves the right to grant separate commercial licences under different terms.

---

## 8. Termination

If you breach any term of this licence, the Author will, where reasonably possible, notify you (via the Contact Channel, or any other contact details you have provided) and give you 14 days from that notice to cure the breach.

If the breach is not cured within that period, or where the Author reasonably considers the breach to be deliberate, repeated, or incapable of being cured (for example, unauthorised commercial exploitation or public hosting of the Software), this licence terminates automatically and immediately, without the notice period above.

Upon termination, you must cease all use of the Software and destroy any copies in your possession.

---

## 9. Disclaimer

The Software is provided "as is", without warranty of any kind, express or implied, including but not limited to warranties of merchantability, fitness for a particular purpose, and non-infringement.

To the extent permitted by law, the Author is not liable for any damages arising from the use of the Software. Nothing in this licence excludes or limits liability for death or personal injury caused by negligence, or for fraud or fraudulent misrepresentation, where such exclusion or limitation is not permitted under the laws of England and Wales.

**Safety notice:** The Software may be used to control physical telescope hardware and associated equipment. It is provided without any warranty that it is safe, fit for purpose, or free of defects. You are responsible for ensuring adequate physical and electrical safety measures, supervision, and fail-safes are in place before operating any hardware controlled by the Software. Do not rely on the Software as a sole safety mechanism.

---

## 10. Governing Law

This licence is governed by and construed in accordance with the laws of England and Wales. The courts of England and Wales have exclusive jurisdiction to settle any dispute arising out of or in connection with this licence.

---

## 11. Acceptance

By using, modifying, or operating the Software, you agree to the terms of this licence.

## 12. Related Policies

If you use the Author's hosted service or any official deployment of the Software, you are also subject to the Service's Terms of Service and Privacy Policy.

- Terms of Service: <https://seleno.org/about/tos>
- Privacy Policy: <https://seleno.org/about/privacy>

This licence governs the Software itself. The Terms of Service and Privacy Policy govern use of the hosted Service.

---

## 13. Third-Party Components

**Third Party Components are not licensed under this licence.** They remain under their own terms, and nothing in this licence restricts, removes, or overrides the rights those terms grant. In particular, the restrictions in Sections 3, 4, 6 and 8 — including the bans on commercial use and public hosting, the requirement that derivatives be released under this licence, and the requirement to destroy copies on termination — apply only to the Author's own original material, and not to the Third Party Components listed below.

These notices are retained as those licences require, and apply only to the specific components named.

### Constellation line data — `ConstellationsTable`, `ConstellationLinesTable`

Derived from the constellation figures published by the d3-celestial project
(<https://github.com/ofrohn/d3-celestial>) and imported into the database by
`scripts/import_constellations.py`. d3-celestial's own sources note that this
line data originates from the [IAU Constellation
page](https://www.iau.org/public/themes/constellations/), with name positions
and some line modifications by Olaf Frohn.

> Copyright (c) 2015, Olaf Frohn
> All rights reserved.
>
> Redistribution and use in source and binary forms, with or without
> modification, are permitted provided that the following conditions are met:
>
> 1. Redistributions of source code must retain the above copyright notice, this
>    list of conditions and the following disclaimer.
> 2. Redistributions in binary form must reproduce the above copyright notice,
>    this list of conditions and the following disclaimer in the documentation
>    and/or other materials provided with the distribution.
> 3. Neither the name of the copyright holder nor the names of its contributors
>    may be used to endorse or promote products derived from this software
>    without specific prior written permission.
>
> THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
> AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
> IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
> DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
> FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
> DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
> SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
> CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
> OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
> OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

### Star names and magnitudes — `HDSTARTable` (`commonNames`, `bayer`, `variableId`, `V-Mag`, `magSource`)

Imported into the database by `scripts/import_star_names.py` from two
catalogues. Rows whose `V-Mag` came from these sources are identified by
`magSource`.

**IAU Catalog of Star Names (IAU-CSN)** — IAU Division C Working Group on Star
Names (<https://www.iau.org/public/themes/naming_stars/>). IAU-produced
products of this kind are believed to be released under Creative Commons
Attribution 4.0 International (CC BY 4.0)
(<https://creativecommons.org/licenses/by/4.0/>), which is the version cited
elsewhere on the IAU's site; the naming_stars page itself returned a 404 when
last checked, so this has not been directly confirmed against it — re-check
the live page if it becomes reachable again, and update this notice if the
stated licence differs. This data has been modified from its original form:
it was imported by `scripts/import_star_names.py` and merged with V
magnitudes from the Yale Bright Star Catalogue (below) into `HDSTARTable`.
Supplies the official proper names and Bayer designations.

**Yale Bright Star Catalogue, 5th Revised Edition** — Hoffleit, D. & Warren Jr,
W. H. (1991), Yale University Observatory; distributed by the CDS as catalogue
V/50 (<https://cdsarc.cds.unistra.fr/ftp/V/50/>). Supplies V magnitudes for the
naked-eye stars the Henry Draper catalogue records none for. No formal licence
for this catalogue is known to the Author; it is acknowledged here as a
source of data, not asserted to carry specific reuse terms.

### Base star catalogue — `HDSTARTable` (base rows: designations, coordinates, base magnitudes)

The base catalogue of approximately 272,000 Henry Draper–designated stars
predates the import scripts described above, and its precise immediate
source has not been conclusively identified. In-The-Sky.org
(<https://in-the-sky.org/data/catalogue.php?cat=HD>), maintained by Dominic
Ford, hosts an HD catalogue in a similar form and is recorded here as a
possible source, but this has not been confirmed, and the data may in fact
originate further upstream. The underlying Henry Draper Catalogue itself
(Harvard College Observatory, 1918–1924) is a public-domain astronomical
catalogue. If the immediate source is later confirmed, this notice should be
updated to credit it precisely, including any reuse terms that source states
(In-The-Sky.org, for instance, claims copyright in its own compiled
presentation of catalogue data and restricts commercial reproduction without
permission — a term the Software's own non-commercial-only restriction in
Section 3.2 would already satisfy if that turns out to be the source, but
this is not asserted as fact here).

### Software dependencies

The Software is built on open-source Python packages (Flask, SQLAlchemy, and
others). These are installed via `pip` from PyPI at setup time and are not
vendored copies within this repository. A snapshot of their names, versions,
and licences, generated by an automated licence scan, is maintained in
`THIRD_PARTY_NOTICES.md` at the repository root. As of the date that file
was last generated, no GPL- or AGPL-licensed dependency was found among them.
