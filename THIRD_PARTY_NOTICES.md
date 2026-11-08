# Third-Party Notices & License Information

## 1. libimobiledevice and Companion Libraries (LGPL v2.1+)

**iOS Media Mover** uses unmodified dynamic libraries (`.dll`) and command-line utilities from the [libimobiledevice](https://libimobiledevice.org/) project:

- **libimobiledevice**: <https://github.com/libimobiledevice/libimobiledevice>
- **libplist**: <https://github.com/libimobiledevice/libplist>
- **libusbmuxd**: <https://github.com/libimobiledevice/libusbmuxd>
- **libimobiledevice-glue**: <https://github.com/libimobiledevice/libimobiledevice-glue>
- **libtatsu**: <https://github.com/libimobiledevice/libtatsu>

Copyright (c) Nikias Bassen, Martin Szulecki, Christophe Fergeau, Jonathan Beck, and contributors.

These libraries and utilities are licensed under the **GNU Lesser General Public License v2.1 (or later)** (`LGPL-2.1-or-later`). The full text of the license is provided in the [`LICENSE`](./LICENSE) file distributed with this software.

In accordance with Section 6 of the LGPL v2.1:
- Pre-built release archives dynamically link against `libimobiledevice-1.0.dll` and its companion `.dll` files located in the `bin/` folder, allowing end users to inspect, modify, or replace those shared libraries at any time.
- Complete corresponding source code for `libimobiledevice` and its companion libraries is available from the official upstream repositories listed above, and can be automatically fetched and rebuilt from source using [`scripts/build_release.ps1`](./scripts/build_release.ps1).

---

## 2. OpenSSL (Apache License 2.0)

Pre-built Windows release packages include `libssl-3-x64.dll` and `libcrypto-3-x64.dll` from the [OpenSSL Project](https://www.openssl.org/).

- **OpenSSL**: <https://github.com/openssl/openssl>
- **License**: [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)

Copyright (c) 1998-2025 The OpenSSL Project Authors. All rights reserved.

---

## 3. Trademark Disclaimer

Apple, iPhone, iPad, iPod, iPod Touch, iOS, iPadOS, and macOS are trademarks of Apple Inc.
This project is independent software and has not been authorized, sponsored, or otherwise approved by Apple Inc.
