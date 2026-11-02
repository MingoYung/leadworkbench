# Third-party notices

The root MIT license covers original LeadWorkbench source code and assets.
Third-party components and copied materials retain their own licenses.

| Component / material | Use | Notices |
| --- | --- | --- |
| Python | Runtime, included in optional frozen Windows builds | [PSF notice](licenses/Python-PSF.txt) |
| PyInstaller | Optional Windows build tool and bootloader | [PyInstaller notice and bootloader exception](licenses/PyInstaller.txt) |
| PySide6-Essentials / shiboken6 / Qt | Native desktop widgets and local single-instance IPC | [Qt for Python notices, exact versions, upstream source locations, and original metadata](licenses/native-desktop/NOTICE.txt) |
| certifi / Mozilla CA bundle | Additional trusted CA roots for public HTTPS source reads | [certifi license](certs/LICENSE-certifi.txt), [bundle provenance](certs/provenance.json) |
| dsh-web-search-pro | Referenced/copied rules for the experimental Kuaishou search adapter | [MIT attribution and upstream commit](licenses/dsh-web-search-pro-MIT.txt) |
| OpenCLI | Separately installed optional platform-read integration; not vendored here | [Upstream project](https://github.com/jackwener/OpenCLI) |

The native desktop uses dynamically loaded Qt shared libraries. QtWebEngine is
not part of the GUI. This repository does not modify Qt or Qt for Python.
`build.ps1 -DesktopLauncher` retains the original component notices with both
onedir distributions. Keep the complete distribution directories when shipping
the program; Qt libraries remain in their ordinary replaceable shared-library
locations. Their source locations and license choices are recorded in the Qt
notice linked above. A root MIT license does not relicense these libraries.

The CA bundle is preserved unchanged. Certificate and hostname verification are
enabled; the application does not install certificates into the operating
system trust store. License and provenance files travel with the bundle.
