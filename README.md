# Meeseeks Box

> "I'm Mr. Meeseeks! Look at me!"

A Meeseeks box is one AI agent ([Qwen Code](https://github.com/QwenLM/qwen-code)) that you
drive from a web page, on your own computer. You type a task, Mr. Meeseeks does it (he can
read and write files and run commands inside his box folder), and you watch it happen live,
from your desktop or your phone.

The thinking happens on **your own model**: any OpenAI-compatible server, such as
[vLLM](https://github.com/vllm-project/vllm), llama.cpp's `llama-server`,
[Ollama](https://ollama.com) or LM Studio, on this computer or another one. The box
itself only runs the page and the agent, so it needs no GPU of its own.

It is **one box**: one agent and one page, run from this folder. Starting it a second time
just shows you the link to the one that's already running.

## What you need

- Linux, or Windows with WSL2 (Ubuntu). macOS should work too.
- Python 3.9+ (with `venv`; on Ubuntu: `sudo apt install python3-venv`)
- Node.js **22 or newer** (`node -v` to check; get it from <https://nodejs.org> or `nvm`)
- `git` and `curl`
- A model behind an OpenAI-compatible API (`/v1/chat/completions` with tool calling).
  Pick a model that is good at tool use and has a long context (32k tokens or more);
  Qwen3 / Qwen-Coder models work well.

## Install

```bash
git clone https://github.com/martinseeger2002/meeseeks-box.git
cd meeseeks-box
./setup.sh            # asks for your model server, key and model; installs into this folder
```

`setup.sh` installs everything inside this folder: a Python environment (`venv/`), Qwen
Code (`vendor/`), and the agent's own home (`home/`). It creates `box.env` (your model server and
settings) and `.token` (the key to your box's page). Nothing is installed system-wide, and
your own `~/.qwen` is never touched.

## Use

```bash
./start.sh    # starts the box and prints its link: http://localhost:8491/?k=...
./stop.sh     # stops the page, the agent, and everything the agent was running
```

Open the link. The first time, tick Rick's terms ("Keep your requests simple. They're not
gods."). Then press the box to summon a Meeseeks and type your task.

- **Box button**: a fresh Meeseeks (new session). Old sessions are listed under
  *Previous sessions* and can be resumed.
- **Mode**: how much the agent may do on its own. The default ("ask per tool") has a
  classifier approve safe commands and refuse dangerous ones. "plan mode" only looks
  and plans. "auto (no asks)" does anything, so use it with care.
- **Share**: makes a read-only live view of the session that you can hand to someone else.
- Logs are in `run/box.log`. The agent's files and memory are in `home/`. It reads
  `home/QWEN.md` at the start of every session, so put standing instructions there.

## From your phone

The box listens only on `localhost`. To use it from your phone on the same Wi-Fi, the
simplest safe option is an SSH tunnel or [Tailscale](https://tailscale.com). If you put it
on the internet (for example with a Cloudflare tunnel), the `?k=` key is the only thing
protecting it. Never post the link anywhere public.

## Settings (`box.env`)

| setting | meaning |
|---|---|
| `OPENAI_BASE_URL` | the model server, e.g. `http://localhost:8000/v1` (vLLM), `http://localhost:11434/v1` (Ollama) |
| `OPENAI_API_KEY` | the server's key, or `none` for a local server that doesn't check. **Keep a real key private** |
| `OPENAI_MODEL` | the model name as the server lists it at `/v1/models` |
| `PORT` | the page's port on your computer (default 8491) |
| `TITLE` | the page's title |

Run `./stop.sh && ./start.sh` after changing these.

## Good to know

- With vLLM, start the server with tool calling on (for Qwen models:
  `--enable-auto-tool-choice --tool-call-parser hermes`). With llama-server, use `--jinja`.
- If the model server is busy, the agent waits or retries; that's normal.
- Mr. Meeseeks can run commands as **your user**. Keep the box folder for his work, and
  use "plan mode" first if you're unsure what he'll do. For full
  separation, run the box as a separate Linux user (`sudo adduser meeseeks`, then clone
  and run it as that user).
- To get a fresh access key for the page: `./stop.sh && rm .token && ./setup.sh && ./start.sh`.
- To update: `git pull`, then `./stop.sh && ./start.sh`.
- To uninstall: `./stop.sh` and delete the folder.

## What's inside

| path | what |
|---|---|
| `relay/serve.py`, `relay/static/` | the web page and the server that connects it to the agent |
| `bin/qwen-local` | starts Qwen Code against the model in `box.env` |
| `setup.sh`, `start.sh`, `stop.sh` | install, start, stop |
| `template/` | the agent's starting settings and `QWEN.md` |

Meeseeks Box is MIT-licensed (see `LICENSE`). Qwen Code is © the Qwen team, under the
Apache-2.0 license, and is downloaded by `setup.sh`, not bundled here. Mr. Meeseeks is a
character from *Rick and Morty*; this is an unofficial fan project.
