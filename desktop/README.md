# Desktop launchers

Freedesktop `.desktop` entries for pinning the two GUI apps (LSO Screener,
ToS Returns) to the taskbar/app menu on Linux.

## Install

```bash
cp desktop/*.desktop ~/.local/share/applications/
update-desktop-database ~/.local/share/applications/
```

Then find "LSO Screener" / "ToS Returns" in the app menu and pin to the taskbar.

## Notes

- **Absolute paths**: `Exec`, `Path`, and `Icon` are hard-coded to
  `/home/bob/Tresorit/ngData/srcCode/lso-tools`. Edit them if the repo lives
  elsewhere or you're on another machine.
- **`Path=` is required.** Both apps read/write their data files by *relative*
  path (`schwab_token.json`, `gains_history_*.json`, the `*_cache.json` files,
  etc.). Without `Path=` setting the working directory, a taskbar launch starts
  in `$HOME`, so the screener can't find its Schwab token ("authentication
  required") and ToS Returns silently reads/writes a second copy of its data in
  `$HOME` — appearing to lose data across runs. Keep `Path=` pointed at the
  repo root.
- **`StartupWMClass`** matches `app.setDesktopFileName(...)` in `run_screener.py`
  / `run_portfolio.py` so the running window binds to the pinned icon instead of
  a generic Python icon.
