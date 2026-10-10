# ex-regex playground

One HTML file that runs ex-regex in the browser with Pyodide.
The live copy is at [nothans.github.io/ex-regex](https://nothans.github.io/ex-regex/), deployed by the Playground workflow on every change to this folder.
To run it locally, build `ex-regex-playground.html` (below) and open it by double-clicking, or serve it from anywhere.

## Features

- A station for each part of the package: test, findall/search, scan, sub, split, extract, find_units, classify, rate, filter/rank, diff, the classic regex problems, `exregex.semantic`, the engine and lockfile, and a Python console.
- Every built-in example replays a real Jev answer from a recorded lockfile, so it works with no key and sends no request.
- Add an OpenRouter key (the pill at the top right) to ask new questions live, with a spending cap.
  Without a key, an unrecorded question offers a keyword stand-in.
- Each result shows the same call in Python, ready to copy.
- The engine station shows the lockfile, its stats, and the five typed errors.
- Light and dark themes, and a phone layout.

## Files

- `template.html` - the app: UI, the Pyodide worker, and the Python glue.
- `playground.py` - the station calls, shared by the browser and the recorder, so both ask identical questions.
- `presets.json` - the built-in examples.
- `snippets.json` - the console snippets, recorded with the presets.
- `decisions.jsonl`, `decisions.semantic-v1.jsonl` - the recorded answers.
- `record.py` - asks every preset live and writes the lockfile.
- `build.py` - writes `ex-regex-playground.html` from the template and the files above.
- `e2e.mjs` - checks the built page in a real browser.

## Rebuild

After changing a preset, record and build (needs `OPENROUTER_API_KEY` and ex-regex 0.1.0a1, the release the page installs):

```
python playground/record.py
python playground/build.py
```

Recorded decisions are reused, so only new questions cost anything.

## Test

```
npm install --no-save --no-package-lock playwright
npx playwright install chromium
node playground/e2e.mjs [--file] [--dark] [--live] [--shots DIR]
```

Without `--live` it fails if any request reaches openrouter.ai.
`--file` opens the page from disk, the way a double-click does.

## Notes

- Pyodide runs in a classic worker.
  A page opened from disk cannot start a module worker in Chrome, so the worker fetches `pyodide.js` itself and Pyodide loads the rest with `import()`.
- The first load fetches Pyodide and the wheel from cdn.jsdelivr.net and pypi.org; after that the browser cache makes it start in about 3 seconds.
- The key stays in the browser and goes only to openrouter.ai.
