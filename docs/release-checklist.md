# Release checklist

Checks that the automated tests cannot do. Run them on the built Windows app before a release.

## Before building

- `pytest` and `pytest -m slow` pass.
- `docker compose up --build` starts, and one file goes through at `http://localhost:8000`.

## Build

- `bin\ffmpeg.exe` is in place, then `python build.py`.
- `dist\GetSubtitles.exe` is the only file to ship. Zip it as `GetSubtitlesApp.zip`.

## Windows app (clean machine or a new Windows user)

- Double-click `GetSubtitles.exe`: the page opens in the browser within a minute.
- Double-click it a second time: the same page opens, no second app starts.
- The page looks right with no internet (fonts, icon).
- First run downloads the Fast model, shows the download percentage, and finishes a short file.
- First run with no internet says that the model could not be downloaded.
- Second start with no internet still works with the downloaded model.
- A file name with non-English letters (`Bölüm 5 Reel.mp4`) gives `Bölüm 5 Reel.srt`.
- A video with two audio tracks shows the track picker, and the second track gives the right language.
- A file over 2 GB uploads and finishes without the memory use growing past the file size.
- Close the browser tab: `GetSubtitles.exe` leaves Task Manager after about a minute.
- Close the tab while a long file is running: the app stays until the job ends.
- **Close the app** ends the process at once.
- Another program on port 8000 (for example `python -m http.server 8000`): the app still starts.
- `getsubtitles.log` in `%LOCALAPPDATA%\GetSubtitles` (the page footer has an Open the folder button) holds the errors when something fails.

## GPU

- Machine with an NVIDIA GPU and the CUDA libraries: the chip at the top and the result line say `GPU`.
- Machine with an NVIDIA GPU but without cuDNN or cuBLAS: the job still finishes and the result line says `CPU`.
- Machine without a GPU: Fast and Balanced finish.

## Subtitles and editor

- Standard style: no line count is red (at most two lines of 42 characters).
- Vertical style: single lines of at most 38 characters.
- Japanese or Chinese audio in vertical style: no spaces between the characters.
- A video that starts with a minute of music: the language is still detected correctly.
- An `.mp4` plays in the editor with the line on the picture. An `.mkv` that the browser cannot play shows the notice and can still be edited.
- Edit a line, download at once: the download has the edit.
- The `.srt` loads in Premiere Pro, DaVinci Resolve and VLC.

## Hugging Face Space

- Build from `Dockerfile.hf` (copied to the Space as `Dockerfile`, with the `app` folder and `requirements.txt`).
- The demo notice is shown, only the Fast model is offered, and a file over 500 MB is refused with a clear message.
- An upload works on the Space itself (the check against requests from other sites compares Origin and Host behind the Hugging Face proxy).
- Two browsers upload a file with the same name at the same time: each gets its own subtitles.
