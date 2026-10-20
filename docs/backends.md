# Backends, pictures, and long texts

ex-regex sends its questions to a System One model: Jev through OpenRouter, Jev from TypeSafe, or a compatible server.
Jev 1.14 adds pictures, boxes, and long texts.

## Keys

For live calls, set a provider key in your shell.
For Jev through OpenRouter:

```bash
export OPENROUTER_API_KEY=sk-or-...
```

In PowerShell:

```powershell
$env:OPENROUTER_API_KEY = "sk-or-..."
```

Use `TYPESAFE_API_KEY` for TypeSafe directly.
The library reads keys from the environment.
The [command line](cli.md) also reads keys from a `.env` file at or above the working directory.

## Backends

| Backend | How | Status |
|---|---|---|
| Jev via OpenRouter | `OPENROUTER_API_KEY`, or `openrouter()` | run live for everything in the README |
| Jev from TypeSafe | `TYPESAFE_API_KEY`, or `typesafe()` (pinned `jev-1.14.0`) | same System One shape; not yet run live from here |
| An open model on your machine | `EXREGEX_BACKEND=http://127.0.0.1:8000` with `EXREGEX_MODEL=...`, or `local(url, model=...)` | any `/v1/systemone` server: Kev, Laya, razorback16/openjev; not yet run live from here |
| Cloudflare Clef | `SystemOne("https://api.cloudflare.com/client/v4/accounts/<id>/ai/run/@cf/cloudflare/clef", api_key=..., model="clef")` | Clef follows the System One API; the reply wrapper is handled; not yet run live |
| OpenAI Decisions API | `OPENAI_API_KEY` with `EXREGEX_BACKEND=openai`, or `openai()` | public beta since 2026-10-06; translated to and from System One per OpenAI's guide and tested against that shape offline; not yet run live |

The presets pin a model: `typesafe/jev-1.14` on OpenRouter and `jev-1.14.0` on TypeSafe, the default since 0.2.0a1.
Pass `model=` for 1.13.
Thresholds do not transfer between models, so re-run `evals/run.py --backend <name>` before you trust one.

## Pictures and boxes

Jev 1.14 reads pictures and answers a fourth question type, `Box`.
TypeSafe labels both "Dreamer-ready": suitable for prototypes, with a format and limits that can change.

```python
import exregex as ex

engine = ex.Engine(ex.openrouter())   # Jev 1.14
photo = ex.image("board.png")         # PNG or JPEG, 280-1,568 px a side, checked before sending
a = engine.ask({"photo": photo}, {
    "there": ex.Noul("`photo` shows the MATRIX logo"),
    "where": ex.Box("the MATRIX logo in `photo`"),
})
a["where"]                        # BoxAnswer(left=0.62, top=0.4, right=0.86, bottom=0.51, confidence=0.87)
a["where"].pixels(748, 993)       # (464, 397, 643, 506), ready for a crop
```

- **A Box always returns a box.** There is no "not found", so ask a `Noul` beside it.
- **Send one picture per request.** Packed together, pictures leak into each other's answers.
- **Tighten a loose box with a second request.** Crop to `a["where"].pad(0.08)`, enlarge, and ask again.
- **The same state works on OpenRouter and on TypeSafe.**
  - `ex.image()` builds the image part OpenRouter reads.
  - `typesafe()` translates it into TypeSafe's own shape: each picture becomes a reference string, the image goes in a top-level `assets` map, and the request carries `TypeSafe-Danger-Zone-Images: true` (also for a Box question with no picture).
  - The reference comes from the picture's bytes, so the same picture is sent once per request.
  - The translation follows TypeSafe's documented JSON format and is tested against it offline; it has not been run against TypeSafe live.
- **Semantic programs do not send pictures to TypeSafe.** They raise `UnsupportedCapability`; use `engine.ask` for pictures.

## Long texts

Long texts use the same calls.
`long_context=True` on any preset raises the 60,000-character limit to 600,000 and sends TypeSafe's long-context header.
OpenRouter switches modes by size without the header.
A whole book can be one `test`:

```python
book = open("aesop.txt").read()   # 313 fables, 200,000 characters
ex.test("a fox fails to reach something and then says it was not worth having", book, unit="text",
        engine=ex.Engine(ex.openrouter(long_context=True)))
```

In a live check on 2026-10-19, one planted sentence in 168,000 characters scored 0.97, the same text without it 0.01, and a near miss 0.04.
TypeSafe documents long context as slower and less accurate than staying under 32k tokens, so prefer packed short spans where the task allows.

## Request limits

- **Request size.** Jev takes up to 32k tokens of state plus the longest question, 64k in total.
  ex-regex packs requests to about 60,000 characters.
  It raises `LimitError` with a hint when one span is too large to judge, for example one huge paragraph.
- **Choice options.** A choice question takes at most 255 options, so `search` and `extract` window long inputs.
- **Cost.** Jev bills $0.042 per million input tokens through OpenRouter or TypeSafe, and output is free.
  ex-regex packs many spans into one request so the state is not repeated.
