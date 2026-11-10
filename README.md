# Sunshine Store

A friendly, Microsoft-Store-style app shop for Arch Linux that searches **pacman**, the **AUR** and
**Flatpak** at once. Built for [Sunshine OS](https://github.com/tak0dan), usable on any Arch system.

![Home](screenshots/store-home.png)

## Features
- One search across the AUR, pacman repositories and Flathub; the same app from several sources is merged into one card.
- Colour-coded frames: **blue** = AUR, **white** = pacman, **gray** = Flatpak, **green** = installed.
- Source hierarchy AUR → Pacman → Flatpak (the default source for an app is the first one that has it).
- Split **Install** button: the arrow offers *Copy install command* (to paste into a terminal) and *Install from another source*.
- *Related packages* tabs below the install button: release, `-bin`, `-git` and other variants.
- Installs through polkit prompts; the AUR path uses [`yay`](https://github.com/Jguer/yay), Flatpak uses `flatpak`.

![Details](screenshots/store-detail.png)

## Install
Arch / AUR: `yay -S sunshine-store`
From source: `sudo make install` (needs `python-pyqt6`; optional: `yay`, `flatpak`).

## Status
Early (0.1). Search, install, uninstall, Home shelves and variants work; the Updates page and a pamac backend are planned.

## License
GPL-3.0-or-later
