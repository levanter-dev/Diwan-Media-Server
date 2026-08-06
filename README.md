# Diwan

A self-hosted Windows media library and web portal for movies, series, and audio.
Browse your local folders, discover new titles via TMDB/OMDb, search and download
subtitles, and stream to any device on your local network. Built-in AI automatically
detects and filters NSFW and inappropriate scenes.

## Features

- **Local media library** — Scan Windows drives and folders; organize movies, series, and audio
- **Explore catalogue** — Search TMDB and OMDb for movies/series, browse trending and popular rows
- **Personal scoring** — Rate titles 1–10 to build a taste profile for recommendations
- **In-page video player** — Play media directly with subtitle overlay, seek controls, and progress saving
- **Subtitle search** — Find and download subtitles from OpenSubtitles, with language filter
- **AI content filtering** — Automatically detect and skip/warn/mark NSFW, nudity, and inappropriate scenes using on-device AI (no cloud)
- **Multi-device** — Access from any device on your local network (phone, tablet, TV)
- **Circle scoring** — Multiple household members can maintain independent scores

## Quick Start

### Prerequisites

- **Windows 10 or 11**
- **Python 3.12+** — [python.org](https://www.python.org/downloads/)
- **Node.js 20+** — [nodejs.org](https://nodejs.org/)
- **FFmpeg** (for the packaged build) — place ffmpeg.exe and ffprobe.exe in vendor/ffmpeg/bin/

### 1. Clone and configure

```bat
git clone https://github.com/levanter-dev/Diwan-Media-Server.git
cd Diwan-Media-Server
copy .env.example .env
```

Edit `.env` and add your API keys (see [API Keys](#api-keys) below).
At minimum you will want a TMDB token for the Explore page.

### 2. Install and run

```bat
scripts\01-setup.bat
scripts\02-run-development.bat
```

Open **http://localhost:8080** in your browser.

### 3. Access from other devices

On other devices, open `http://<your-pc-ip>:8080` (e.g. `http://192.168.1.50:8080`).

To use a friendly name instead of an IP, set `DOMAIN=diwan.local` in your `.env`
file. The server automatically advertises the name via mDNS (Bonjour/Zeroconf) so
any device on your network can reach it at `http://diwan.local:8080` — no hosts
file editing or router config required.

Press **Ctrl+C** to stop both servers.

## Architecture

```text
Browser / TV / Phone
    |
    | http://server-pc:8080
    v
Node portal (serves web UI)
    |
    | http://127.0.0.1:8081/api
    v
Python FastAPI server
    |-- Windows drives and folders
    |-- SQLite database
    |-- FFmpeg / FFprobe
    |-- Media scanning and jobs
```

Development uses two processes: Python FastAPI (port 8081) and Node.js (port 8080).
The packaged `.exe` combines both into a single process.

## API Keys

Diwan uses three external services. All are free.

### TMDB (The Movie Database)

Used for search, posters, metadata, and discovery rows (trending, popular, now playing).

1. Sign up at [themoviedb.org](https://www.themoviedb.org/signup)
2. Go to [Settings → API](https://www.themoviedb.org/settings/api)
3. Request an API key — choose **Developer**
4. Copy the **API Read Access Token (v4 auth)** — starts with `eyJ...`
5. Add to `.env`: `TMDB_TOKEN=eyJ...`

> This product uses the TMDB API but is not endorsed or certified by TMDB.

### OMDb (Open Movie Database)

Alternative search engine.

1. Go to [omdbapi.com/apikey.aspx](https://www.omdbapi.com/apikey.aspx)
2. Choose **FREE** tier (1,000 requests/day)
3. Enter your email; you will receive a key
4. Add to `.env`: `OMDB_API_KEY=your-key`

### OpenSubtitles

Subtitle search and download.

1. Sign up at [opensubtitles.com](https://www.opensubtitles.com/en/users/sign_up)
2. Go to [Consumers](https://www.opensubtitles.com/en/consumers)
3. Create a new Consumer (any name)
4. Copy the **API Key**
5. Add to `.env`:
   ```
   OPENSUBTITLES_USERNAME=your-username
   OPENSUBTITLES_PASSWORD=your-password
   OPENSUBTITLES_API_KEY=your-api-key
   ```

> Credentials are stored in SQLite. The browser only sees configured/not configured status.

## AI Content Filtering

Diwan uses on-device AI to detect and handle inappropriate scenes — no files
are uploaded to any cloud service. Two AI models run locally:

- **[NudeNet](https://github.com/notAI-tech/NudeNet)** — detects exposed body parts (anatomy)
- **[OpenCLIP (ViT-B-32)](https://github.com/mlfoundations/open_clip)** — understands scene context (activity, attire, kissing)

### Detected categories

| Category | Detector | Description |
|---|---|---|
| Sexual activity | OpenCLIP | Explicit sexual content in a scene |
| Female toplessness | NudeNet | Exposed female chest |
| Male toplessness | NudeNet | Exposed male chest |
| General nudity | NudeNet | Any exposed private parts or buttocks |
| Kissing | OpenCLIP | Romantic kissing scenes |
| Revealing attire / swimwear | OpenCLIP | Scantily clad or swimwear scenes |

### Per-category actions

Each category can be independently set to one of four modes:

| Action | Behavior |
|---|---|
| **Off** | Category is ignored |
| **Marker** | Shows colored markers on the playback timeline |
| **Warn** | Displays a warning overlay before the scene; viewer can choose to skip or continue |
| **Skip** | Automatically jumps past the detected scene with a brief notice |

### Sensitivity

Three sensitivity levels control the detection threshold: **Low** (fewer false
positives), **Balanced**, and **High** (catches more borderline scenes).

### GPU acceleration

Content analysis runs significantly faster with an NVIDIA GPU (CUDA).
On CPU-only systems, analysis still works but takes longer. The Settings page
shows whether GPU acceleration is available.

### How it works

1. Enable content filtering in Settings and choose your preferred actions
2. Newly scanned videos are analyzed automatically (or trigger manually)
3. During playback, detected scenes are handled according to your settings
4. You can override filters per media item from the media details page

## Configuration

All settings go in `.env`. See `.env.example` for the full list.

| Variable | Default | Description |
|---|---|---|
| `TMDB_TOKEN` | — | TMDB API Read Access Token |
| `OMDB_API_KEY` | — | OMDb API key |
| `OPENSUBTITLES_USERNAME` | — | OpenSubtitles username |
| `OPENSUBTITLES_PASSWORD` | — | OpenSubtitles password |
| `OPENSUBTITLES_API_KEY` | — | OpenSubtitles consumer API key |
| `PORT` | 8080 | Web portal port |
| `DOMAIN` | — | Custom local domain (e.g. diwan.local) |
| `MEDIA_ROOTS` | All drives | Comma-separated folder paths |
| `DATA_DIR` | AppData | Database and config location |

## Building the Windows Executable

To create a standalone `.exe`:

1. Place `ffmpeg.exe` and `ffprobe.exe` in `vendor/ffmpeg/bin/`
2. Run:

```bat
scripts\03-build-exe.bat
scripts\04-install-local.bat
scripts\05-run-installed.bat
```

The server auto-starts at logon. To remove:

```bat
scripts\90-uninstall-local.bat
```

## Project Structure

```text
app/                    FastAPI backend (database, scanner, scrapers, analysis)
web/                    Browser SPA + Node dev server
scripts/                Setup, run, build, install, uninstall
vendor/ffmpeg/          Place ffmpeg.exe + ffprobe.exe here
native_server.py        Entry point for the native Windows executable
native_server.spec      PyInstaller packaging definition
requirements.txt        Python dependencies
requirements-build.txt  Build dependencies
.env.example            Config template
```

## Security

- Media files stay on the server. Nothing is uploaded.
- API keys and passwords live in server-side SQLite, never sent to the browser.
- Do **not** expose port 8080 to the public internet.
- Authentication is planned for a future release.

## License

[MIT](LICENSE)