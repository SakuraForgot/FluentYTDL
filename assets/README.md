# Application icon

`logo.svg` is the approved pure-vector source (icon study v13). Edit this file for
future changes, then regenerate the shipped resources from the project root:

```powershell
.venv/Scripts/python.exe scripts/generate_app_icons.py
```

- `FluentYTDL_v2.ico`: 16/20/24/32/48/64/72/96/128/256px frames. Shared by the
  application, tray, build GUI, EXE, updater and installer icon configuration.
- `logo.png`: 256px image for help pages and thumbnail placeholders.
- `logo_16.png`, `logo_32.png`, `logo_64.png`, `logo_128.png`: runtime fallbacks.
- `logo_tight.png`: legacy cropped fallback, from the same vector source.

The existing packaging configuration ships the generated ICO/PNG resources.
The SVG source and `icon-preview/` studies are development assets.
Earlier production resources are preserved in `icon-preview/original-assets/`.
