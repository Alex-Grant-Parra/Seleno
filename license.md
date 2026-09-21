# SELENO Personal Use License (APUL) v1.0

Copyright (c) 2026 Alex Grant-Parra

Last Updated: 8th of August 2026

---

## 1. Definitions

- **"Software"** refers to all source code, binaries, documentation, and associated materials provided under this licence.  
- **"Author"** refers to Alex Grant-Parra.  
- **"Official Service"** refers to any server, API, or infrastructure operated by the Author.  
- **"Modified Version"** refers to any version of the Software that has been altered from its original form.  
- **"Public Service"** refers to any server, platform, or system made accessible to users beyond a single private individual or organisation.  

---

## 2. Permitted Use

You are granted a non-exclusive, non-transferable, revocable licence to:

- Use the Software for personal, private, and non-commercial purposes.  
- Operate the Software on private systems, including private telescope setups or private telescope networks.  
- Study, modify, and adapt the Software for your own use.  

---

## 3. Restrictions

You may NOT:

### 3.1 Public Hosting
- Host, deploy, or operate the Software (or any derivative of it) as a Public Service without explicit written permission from the Author.  

### 3.2 Commercial Use
- Use the Software, or any part of it, for commercial purposes without prior written permission from the Author.  
- Sell, sublicense, or monetise the Software or any derivative works.  

### 3.3 Redistribution
- Redistribute the Software or Modified Versions without including this licence and proper attribution to the Author.  

### 3.4 Official Service Access
- Use Modified Versions of the Software to access or interact with the Official Service unless explicitly permitted by the Author.  

### 3.5 Circumvention
- Attempt to bypass, disable, or interfere with any technical measures enforcing these restrictions.  

---

## 4. Modifications

You are permitted to modify the Software for personal use. However:

- Modified Versions must not be used to access the Official Service unless authorised.  
- Any distribution of Modified Versions must comply with all terms of this licence.  

---

## 5. Attribution

All copies or substantial portions of the Software must include:

- The original copyright notice  
- A reference to this licence  
- Clear acknowledgement of the original Author  

---

## 6. Derivative Works

Any derivative works based on the Software must:

- Be distributed under this same licence (APUL v1.0)  
- Not grant additional permissions beyond those defined here  

---

## 7. Commercial Licensing

For commercial use, licensing, or partnerships, you must contact the Author directly.

The Author reserves the right to grant separate commercial licences under different terms.  

---

## 8. Termination

This licence is automatically terminated if you violate any of its terms.

Upon termination, you must cease all use of the Software and destroy any copies in your possession.  

---

## 9. Disclaimer

The Software is provided "as is", without warranty of any kind, express or implied.

The Author is not liable for any damages arising from the use of the Software.  

---

## 10. Governing Law

This licence shall be governed by and interpreted in accordance with the laws of the United Kingdom.  

---

## 11. Acceptance

By using, modifying, or distributing the Software, you agree to the terms of this licence.

## 12. Related Policies

If you use the Author's hosted service or any official deployment of the Software, you are also subject to the Service's Terms of Service and Privacy Policy.

- Terms of Service: /about/tos
- Privacy Policy: /about/privacy

This licence governs the Software itself. The Terms of Service and Privacy Policy govern use of the hosted Service.

---

## 13. Third-Party Components

Portions of the Software incorporate third-party material under their own terms.
These notices are retained as those licences require, and they apply only to the
components named.

### Constellation line data — `ConstellationsTable`, `ConstellationLinesTable`

Derived from the constellation figures published by the d3-celestial project
(<https://github.com/ofrohn/d3-celestial>) and imported into the database by
`scripts/import_constellations.py`.

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
Names (<https://www.iau.org/public/themes/naming_stars/>). IAU-produced products
are released under Creative Commons Attribution, i.e. free to use in perpetuity
and world-wide as long as the source is mentioned. Supplies the official proper
names and Bayer designations.

**Yale Bright Star Catalogue, 5th Revised Edition** — Hoffleit, D. & Warren Jr,
W. H. (1991), Yale University Observatory; distributed by the CDS as catalogue
V/50 (<https://cdsarc.cds.unistra.fr/ftp/V/50/>). Supplies V magnitudes for the
naked-eye stars the Henry Draper catalogue records none for.

