#!/usr/bin/env python3
"""Meeseeks box: a web page in front of one headless Qwen Code session.

One persistent `qwen -p --input-format stream-json --output-format stream-json`
subprocess is the session; this server speaks it and relays it to a browser over
SSE. The model it talks to is whatever OPENAI_BASE_URL / OPENAI_API_KEY point at
(see box.env).

Access control: the whole site requires ?k=<token> once (then a cookie). The token
lives in .token next to box.env and is created by setup.sh.
"""
import argparse
import collections
import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import quote

from flask import Flask, Response, jsonify, redirect, request, send_file

HOME = Path.home()
BOX_DIR = Path(__file__).resolve().parent.parent
# Roots the web page may view images from. The session itself can read
# anything its permissions allow; this list is only what a URL can fetch.
# Images the agent reads or writes can be anywhere the owner works, so the viewable area
# is the home directory rather than a hand-listed few — the page is already behind the
# same token as the session, which can read all of this anyway. The denylist is for
# things that are credentials rather than content: no reason for a web page to fetch
# them, and a stray path in a tool result should not turn into an exfiltration route.
FILE_ROOTS = [HOME]
FILE_DENY = [HOME / ".ssh", HOME / ".gnupg", HOME / ".cloudflared", HOME / ".aws",
             HOME / ".config/gcloud", HOME / ".claude/.credentials.json",
             BOX_DIR / ".token", BOX_DIR / "box.env"]
# Only actual media: a tool result naming secrets.env must not make it fetchable.
VIEWABLE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif"}
# Audio the agent made is played in the feed too: a guest box's user has no way to reach
# the machine, so a file that is only on its disk does not exist for them (2026-09-24).
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".ogg", ".opus", ".flac", ".aac"}

# The web UI speaks Claude Code's mode names (the page and the /mode endpoint predate
# the move to our own harness); the fork spells them differently.
_APPROVAL_MODES = {
    # "default" maps to the fork's `auto`, NOT its `default`: `default` asks a human,
    # and headless there is no human to ask, so it just blocks. `auto` decides with an
    # LLM classifier — running on the LOCAL model, so the judgment costs nothing and
    # leaves nothing. Verified 2026-09-21: it allowed `echo hello` and denied
    # `rm -rf <dir>` (the directory survived).
    "default": "auto",
    "acceptEdits": "auto-edit",        # edits through unconditionally, shell classified
    "plan": "plan",                    # analyse only
    "bypassPermissions": "yolo",       # everything, no questions
}

# The chosen mode outlives the process. It used to be a bare global seeded from
# --mode, so every service restart silently dropped back to the asking mode while
# the phone still showed whatever it last picked. --mode is now only the fallback
# for a first run.
MODE_FILE = Path(__file__).resolve().parent / "mode.json"


def _prefs():
    try:
        d = json.loads(MODE_FILE.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_prefs(**kw):
    d = _prefs()
    d.update(kw)
    try:
        MODE_FILE.write_text(json.dumps(d) + "\n")
    except OSError:
        pass                                  # a read-only disk must not break /mode


def _load_mode(fallback):
    m = _prefs().get("mode")
    return m if m in _APPROVAL_MODES else fallback


def _save_mode(m):
    _save_prefs(mode=m)


# ── autopilot ───────────────────────────────────────────────────────────────────────
# One checkbox: run with no approvals AND keep the agent going after it stops, until it
# says it is done. The agent has no idea a turn "ended" — from its side it answered and
# fell silent — so finishing is something it has to DECLARE, and the sentinel is how.
# It runs unbounded on purpose. Watch it on the page: every continue is announced, and
# a message, Stop or the checkbox ends it the moment you want it ended.
# Messages waiting for a polite moment. A prompt sent while the agent is mid-turn
# lands in the middle of its reasoning; the same words delivered as the thing that
# starts its NEXT turn are read as the instruction they are. So anything that is not
# time-critical queues here and rides out with the continue prompt.
PENDING = []
INJECT_LOCK = threading.Lock()


# Politeness has a limit. A turn that runs for hours means a queued message is never
# delivered at all, which is not patience, it is loss — observed at 45 minutes while the
# agent worked on the very thing the message was about. Past this, deliver anyway.
INJECT_MAX_WAIT = float(os.environ.get("RQ_INJECT_MAX_WAIT", "1800"))


def _queue_injection(text):
    with INJECT_LOCK:
        PENDING.append((text, time.time()))
        return len(PENDING)


def _overdue():
    """Queued messages that have waited longer than a turn has any right to take."""
    now = time.time()
    with INJECT_LOCK:
        if not PENDING or now - min(t for _, t in PENDING) < INJECT_MAX_WAIT:
            return []
        items, PENDING[:] = [t for t, _ in PENDING], []
    return items


def _drain_injections():
    with INJECT_LOCK:
        items, PENDING[:] = [t for t, _ in PENDING], []
    return items


def _pending_count():
    with INJECT_LOCK:
        return len(PENDING)


AUTOPILOT = False
# No limit by default: "keep going" means keep going. The stop conditions are the
# sentinel, a message from you, Stop, or unticking the box — all of them yours. Set
# RQ_AUTOPILOT_MAX to a positive number to put a ceiling back on.
AUTOPILOT_MAX = int(os.environ.get("RQ_AUTOPILOT_MAX", "0") or 0)
AUTOPILOT_DELAY = float(os.environ.get("RQ_AUTOPILOT_DELAY", "3"))
DONE_SENTINEL = "ALL GOALS ACHIEVED"
CONTINUE_PROMPT = (
    "Continue the work. Do not ask me anything — decide and proceed on your own. If "
    "something is blocked, work around it or move to the next item.\n\n"
    "Start by calling plan_status: the plan file is the memory this conversation does "
    "not keep, and it is the authority on what is left. If the plan has a section "
    "titled \"Review note\" that you have not acted on yet, read it and deal with it "
    "BEFORE new work — those are findings from a reviewer looking at your commits from "
    "outside, and they are usually about something you cannot see from in here. "
    "Otherwise do the FIRST remaining step. "
    "Then plan_record what you actually built (before dropping the step, so an "
    "interrupted run still leaves a record) and plan_set_order with that step removed. "
    "If the work proved the plan wrong, change the plan with plan_set_order or "
    "plan_edit rather than following it off a cliff.\n\n"
    "Commit with remote_commit, never with a bare git commit: it stages your work, runs "
    "the tests covering what you changed, and commits ONLY if they pass — so a commit "
    "means the tests were green at that moment. If it refuses, fix what failed rather "
    "than committing around it.\n\n"
    "Only when the plan has no steps left, and you have verified the work, reply with "
    f"exactly {DONE_SENTINEL} on its own line. Do not say it early: if anything is "
    "left, keep working instead."
)

def _group_pids(pgid):
    """Every live pid in a process group, read straight from /proc.

    `comm` can contain spaces and parentheses, so the fields are taken after the
    LAST ')': state, ppid, pgrp.
    """
    out = []
    if pgid is None:
        return out
    want = str(pgid)
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return out
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            tail = (entry / "stat").read_text().rpartition(")")[2].split()
        except OSError:
            continue                       # it exited while we looked
        if len(tail) >= 3 and tail[2] == want:
            out.append(int(entry.name))
    return out


# An agent this relay spawned, recognised by argv. An interactive `qwen-local` has
# no -p, so this never matches a terminal the user is sitting at.
_AGENT_ARGV = ("--input-format", "stream-json")


def _stray_agents(keep_pgid=None):
    """Agent processes left over from an earlier relay (or an earlier crash)."""
    out = []
    keep = set(_group_pids(keep_pgid))
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return out
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) in keep:
            continue
        try:
            argv = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        if str(BOX_DIR / "vendor") in argv and \
                all(a in argv for a in _AGENT_ARGV) and not _has_live_relay(entry):
            out.append(int(entry.name))
    return out


def _has_live_relay(proc_dir):
    """True when a running relay is among this agent's ancestors — it is not a leftover.

    A relay that dies leaves its agent reparented onto systemd; one that is alive sits
    above it (directly, or above the agent's own worker processes). Without this check,
    starting ANY second relay as the same user (a test copy on a spare port, 2026-09-24)
    SIGKILLed the owner's live session mid-turn.
    """
    pid = int(proc_dir.name)
    for _ in range(12):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            pid = int(stat[stat.rindex(")") + 2:].split()[1])
            if pid <= 1:
                return False
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
        except (OSError, ValueError, IndexError):
            return False
        if "serve.py" in argv:
            return True
    return False


# ── the share stream ────────────────────────────────────────────────────────────────
# A read-only window on the SAME session for anyone the owner hands the link to. Read-only
# is enforced on the SERVER, not by hiding buttons: these routes are GET-only and there
# is no path from them to /send, /approve, /mode, /new, /sessions/* or /file.
#
# It carries its OWN secret, separate from the operator token, for two reasons. Sharing
# a view must never hand over the ability to drive the session — so the two secrets can
# never be the same string. And an endpoint with no secret at all would put every tool
# call and every file the agent prints in front of anyone who guessed the path, while
# the agent runs unattended with no approvals in the owner's home directory. Rotating the
# view secret revokes every link already handed out.
# The agent driving the owner's actual keyboard, pointer and screen. OFF unless he turns
# it on, and off again by default at every boot: a capability that moves the mouse on a
# desk somebody is sitting at should never be something he finds already running.
DESKTOP = False


SHARE = False
VIEW_FILE = Path(__file__).resolve().parent / ".view-token"

# Who is watching, counted by live feed connection. Worth showing for its own sake —
# and worth showing because the one thing you want to know about a shared stream is
# whether anyone is actually on it.
WATCHERS = set()
WATCH_LOCK = threading.Lock()


def _watchers():
    with WATCH_LOCK:
        return len(WATCHERS)


def _view_token():
    try:
        tok = VIEW_FILE.read_text().strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    VIEW_FILE.write_text(tok + "\n")
    try:
        VIEW_FILE.chmod(0o600)
    except OSError:
        pass
    return tok


def _may_watch():
    if not SHARE:
        return False
    tok = _view_token()
    return request.args.get("v") == tok or request.cookies.get("rq_view") == tok


def _redact(obj):
    """Scrub both secrets out of anything on its way to a viewer."""
    secretish = [t for t in (TOKEN, _view_token()) if t]
    def scrub(x):
        if isinstance(x, str):
            for t in secretish:
                x = x.replace(t, "[redacted]")
            return x
        if isinstance(x, list):
            return [scrub(v) for v in x]
        if isinstance(x, dict):
            return {k: scrub(v) for k, v in x.items()}
        return x
    return scrub(obj)


def _public_safe(evt):
    """One event, rewritten for a viewer.

    Image BYTES never go out: the operator's page fetches those through /file, which is
    an authenticated read of the owner's home directory, and a viewer has no business
    reaching it. Approvals and questions go out as a note with no request_id, so nothing
    a viewer could send would ever match a tool waiting for an answer.
    """
    t = evt.get("type")
    if t in ("approval", "ask"):
        return {"type": "waiting", "ts": evt.get("ts"),
                "text": _redact(str(evt.get("question") or evt.get("tool") or "the operator"))}
    out = dict(evt)
    out.pop("uid", None)
    if t in ("assistant", "tool_results"):
        blocks = []
        for b in out.get("blocks", []):
            b = dict(b)
            if b.get("kind") in ("image", "image_url", "audio_url"):
                b = {"kind": "text", "text": "[media — not shown on the shared stream]"}
            blocks.append(b)
        out["blocks"] = blocks
    return _redact(out)


# static_folder=None kills Flask's built-in /static/<path> route. It was serving
# static/index.html — the whole operator UI — and static/watch.html to anyone, straight
# past the token check on "/" and past the share switch on "/watch". Nothing needs it:
# the icons, the service worker and the manifest each have their own route below.
app = Flask(__name__, static_folder=None)
TOKEN = ""

# ── Meeseeks boxes ──────────────────────────────────────────────────────────────────
# TITLE: what the page calls itself. TERMS: the box does no work until someone ticks
# Rick's terms (enforced here, not only on the page).
TITLE = "Qwen Remote"
TERMS = False
TERMS_TEXT = "Keep your requests simple. They're not gods."
TERMS_FILE = Path(__file__).resolve().parent / "terms-agreed.json"
# False on guest boxes: they have no desktop tools and no screen of their own, so the
# "drive desktop" switch would be a dead control that warns about moving your pointer.
# (A virtual desktop, or another machine's desktop, may be given to them later.)
DESKTOP_OK = True
# Served under a path on a shared hostname (example.com/box) instead of a host
# of its own. cloudflared cannot strip a path prefix, so the app strips it on the way in
# (_Prefixed) and puts it back on every URL it hands out (P, _prefixed_text).
PREFIX = ""


def P(url):
    """An app URL as the browser must request it."""
    return PREFIX + url if PREFIX and url.startswith("/") else url


# Every root-relative app URL in the page, the service worker and the key form. Rewritten
# only when a prefix is set, so an unprefixed box serves its files byte for byte.
_APP_PATHS = re.compile(r"""(['"`])/(?=(?:new|exit|approve|watch|upload|ui/|terms|sw\.js|"""
                        r"""summon\.m4a|status|share|sessions|send|mode|interrupt|file\?|"""
                        r"""events|desktop|autopilot|manifest\.webmanifest|icon-|\?k=|\1))""")


def _prefixed_text(text):
    return _APP_PATHS.sub(lambda m: m.group(1) + PREFIX + "/", text) if PREFIX else text


def _prefixed_file(path, mimetype):
    if not PREFIX:
        return send_file(path, mimetype=mimetype, max_age=0)
    return Response(_prefixed_text(Path(path).read_text()), mimetype=mimetype)


class _Prefixed:
    """WSGI: serve the app under PREFIX; anything outside it is not ours."""
    def __init__(self, wsgi):
        self.wsgi = wsgi

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path == PREFIX or path.startswith(PREFIX + "/"):
            environ["SCRIPT_NAME"] = PREFIX
            environ["PATH_INFO"] = path[len(PREFIX):] or "/"
            return self.wsgi(environ, start_response)
        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"not here"]
PERMISSION_MODE = "default"


class Session:
    """The one agent subprocess and everything the browser needs from it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.session_id = None
        self.alive = False
        self.dead_reason = None
        self.pending_approvals = {}          # request_id -> threading.Event
        self.pending_inputs = {}             # request_id -> tool input (echoed on allow)
        # Whether the model is working, tracked HERE rather than in the page: a phone
        # locks its screen mid-answer and reconnects with no memory of what it sent, so
        # the state has to survive a page reload and arrive in /status.
        self.busy = False
        self.activity = ""
        self.detail = ""
        self.busy_since = 0.0
        # Messages written to the agent that it has not finished yet. The harness QUEUES
        # what arrives mid-turn and answers each one with its own result, so "a result
        # came" is not "the agent is idle": treating it so sent a Continue per result and
        # kept a standing backlog, every message hours behind (Claude, 2026-09-25).
        self.outstanding = 0
        # autopilot bookkeeping: how many continues since the last thing YOU typed,
        # the agent's last words (the sentinel is looked for there), and a latch that
        # cancels a continue already in flight.
        self.autopilot_rounds = 0
        self.last_assistant_text = ""
        self.autopilot_stop = False
        self.last_event = time.time()
        self.subs = []                      # per-client queues
        self.replay = collections.deque(maxlen=200)
        self.reader = None

    # ------------------------------------------------ lifecycle
    def start(self):
        with self.lock:
            if self.alive:
                return
            # Qwen Code (bin/qwen-local), NOT Claude Code: this stack is
            # meant to be ours end to end — local model, local harness, local search,
            # no Anthropic anywhere. The fork speaks the same stream-json protocol, so
            # the events this file parses (system/init, assistant, result) are
            # unchanged; it also emits "stream_event" frames, which _dispatch ignores.
            #
            # Approval: the fork takes --approval-mode rather than Claude Code's
            # --permission-mode / --permission-prompt-tool pair. It has no host-answered
            # prompt protocol, so the browser's approve button has nothing to answer
            # right now — auto-edit is what keeps the session useful from a phone, and
            # its own "auto" mode (an LLM classifier, running on the LOCAL model) is the
            # thing to wire up next.
            args = [os.environ.get("MEESEEKS_AGENT", str(BOX_DIR / "bin/qwen-local")), "-p",
                    "--input-format", "stream-json",
                    "--output-format", "stream-json",
                    "--approval-mode", _APPROVAL_MODES.get(PERMISSION_MODE, "auto-edit")]
            if self.session_id:
                args += ["--resume", self.session_id]
            env = dict(os.environ)
            env.setdefault("HOME", str(HOME))
            # The agent's stderr goes to a log, not /dev/null: on 2026-09-24 it exited
            # mid-turn with no error anywhere, and the reason was unrecoverable.
            errlog = open(Path(__file__).resolve().parent / "agent-stderr.log", "a")
            errlog.write(f"\n=== agent start {time.strftime('%Y-%m-%d %H:%M:%S')} "
                         f"session={self.session_id}\n")
            errlog.flush()
            try:
                self.proc = subprocess.Popen(
                    args, cwd=str(HOME), env=env, start_new_session=True,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=errlog, text=True, bufsize=1)
            except FileNotFoundError as exc:
                self._publish({"type": "fatal", "text": f"cannot start qwen-local: {exc}"})
                return
            self.alive = True
            self.dead_reason = None
            self.busy = False          # a fresh process is not mid-answer
            self.outstanding = 0
            self.activity = ""
            self.busy_since = 0.0
            self._publish({"type": "status", "alive": True,
                           "session_id": self.session_id,
                           "permission_mode": PERMISSION_MODE})
            self.reader = threading.Thread(target=self._read_loop, daemon=True)
            self.reader.start()

    def stop(self):
        with self.lock:
            self.alive = False
            proc, self.proc = self.proc, None
        # Clear the spinner HERE. Killing the process that was mid-answer leaves
        # busy=True with nobody left to turn it off, and the stale-reader guard
        # deliberately says nothing — so the page span forever on "thinking…" while
        # the model sat idle. Observed for 89 minutes straight.
        self._set_busy(False)
        if proc:
            # The group id must be read BEFORE the wait. Waiting reaps the child, and
            # os.getpgid() on a reaped pid raises ProcessLookupError — which the old
            # code caught and ignored, so the group kill silently never happened.
            # That mattered because node relaunches itself with a bigger heap: the
            # REAL agent is a GRANDCHILD in this group, not proc.pid. Killing only
            # proc.pid left one agent alive per session switch; 29 were found running
            # at once, holding 5.5 GB, with the relay reporting no restarts.
            # start_new_session=True in start() is what makes the group ours to signal.
            try:
                pgid = os.getpgid(proc.pid)
            except OSError:
                pgid = None
            for sig in (signal.SIGTERM, signal.SIGKILL):
                if pgid is not None:
                    try:
                        os.killpg(pgid, sig)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
                try:
                    proc.send_signal(sig)
                except (ProcessLookupError, OSError, ValueError):
                    pass
                try:
                    proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    pass
                if not _group_pids(pgid):
                    break
            left = _group_pids(pgid)
            if left:
                # Never silent: a survivor is the thing that ate the machine.
                self._publish({"type": "log",
                               "text": f"warning: agent pids {left} survived shutdown"})

    def open_session(self, session_id, force=False):
        """Switch to a stored session. The fork resumes it via --resume on start.

        `force` respawns even when that session is already the live one, which is what
        a mode change needs: --approval-mode is a process argument, so the only way to
        change it is a new process on the same conversation.
        """
        # Opening the session that is ALREADY live is a no-op. Every device shares one
        # agent here, so a second phone opening the same conversation used to stop and
        # respawn it — killing the answer the first phone was waiting on and clearing
        # its transcript out from under it. The opening page has already rendered the
        # stored history by the time it calls in, so there is nothing left to do.
        if not force and session_id and session_id == self.session_id and self.alive:
            return False
        self.stop()
        self.session_id = session_id
        self.pending_approvals.clear()
        self.pending_inputs.clear()
        with self.lock:
            self.replay.clear()
        # NOT a "clear": every open page treats that as "wipe the transcript", which
        # threw away the history the page had just replayed and left "chat cleared"
        # sitting where the conversation should be. Resuming is its own event, and a
        # page that receives it reloads the stored transcript instead.
        self._publish({"type": "resumed", "id": session_id})
        self.start()
        return True

    def new_chat(self):
        self.stop()
        self.session_id = None
        self.autopilot_rounds = 0
        self.last_assistant_text = ""
        self.pending_approvals.clear()
        self.pending_inputs.clear()
        with self.lock:
            self.replay.clear()
        self._publish({"type": "clear"})   # tells open pages to wipe their transcript
        self.start()

    def send(self, text, display=None, images=None, uid=""):
        if not self.alive or not self.proc:
            self.start()
        # Echo the turn to EVERY connected device. The pages used to add the user
        # bubble locally, so a second phone on the same session watched answers arrive
        # with no question above them. Published before the write so it lands ahead of
        # the reply, and it goes through the replay ring, so a device that reconnects
        # mid-turn still gets it.
        self._publish({"type": "you",
                       "text": display if display is not None else text,
                       "images": [u for u in (images or []) if isinstance(u, str)][:8],
                       "uid": str(uid or "")[:64]})
        self._set_busy(True, "thinking")
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        try:
            with self.lock:
                self.proc.stdin.write(json.dumps(msg) + "\n")
                self.proc.stdin.flush()
                self.outstanding += 1
        except Exception as exc:
            self._publish({"type": "fatal", "text": f"send failed: {exc}"})

    def _maybe_continue(self, evt):
        """A turn just ended. Under autopilot, send it onward unless it says it is done.

        Three ways this stops, and all three say so on the page: the agent declares
        the sentinel, the round cap is reached, or you take over (a message, Stop, or
        unticking the box). Silence is never one of them.
        """
        # Queued messages go first, and they go even when autopilot is off: waiting
        # for a turn to end is about not interrupting, not about the loop being armed.
        waiting = _drain_injections()
        if waiting:
            body = "\n\n".join(waiting)
            text = (body + "\n\n" + CONTINUE_PROMPT) if AUTOPILOT else body
            self.autopilot_rounds = 0          # a new instruction, not another lap
            self.autopilot_stop = False
            threading.Thread(
                target=lambda: (time.sleep(0.5),
                                self.send(text, display="↻ queued message, delivered "
                                                        "now the turn is done")),
                daemon=True).start()
            return

        if not AUTOPILOT or self.autopilot_stop:
            return
        said = f"{self.last_assistant_text} {evt.get('result', '')}"
        if DONE_SENTINEL.lower() in said.lower():
            self._publish({"type": "log",
                           "text": "autopilot: the agent reports all goals achieved — stopping."})
            return
        if AUTOPILOT_MAX and self.autopilot_rounds >= AUTOPILOT_MAX:
            self._publish({"type": "log",
                           "text": (f"autopilot: paused after {AUTOPILOT_MAX} continues with no "
                                    f"'{DONE_SENTINEL}'. Send a message to carry on.")})
            return
        self.autopilot_rounds += 1
        n = self.autopilot_rounds

        def go():
            # Off the reader thread: send() takes the same lock the reader's dispatch
            # path can be holding, and a pause makes a runaway visible before it is a
            # hundred turns deep.
            time.sleep(AUTOPILOT_DELAY)
            if not AUTOPILOT or self.autopilot_stop:
                return
            count = f"{n}/{AUTOPILOT_MAX}" if AUTOPILOT_MAX else str(n)
            self.send(CONTINUE_PROMPT, display=f"↻ autopilot: continue ({count})")

        threading.Thread(target=go, daemon=True).start()

    def answer_approval(self, request_id, behavior):
        ev = self.pending_approvals.pop(request_id, None)
        payload = self.pending_inputs.pop(request_id, None)
        if self.proc and ev:
            if behavior == "allow":
                inner = {"behavior": "allow", "updatedInput": payload or {},
                         "updatedPermissions": []}
            else:
                inner = {"behavior": "deny", "message": "Denied from the remote page",
                         "interrupt": False}
            resp = {"type": "control_response", "response": {
                "subtype": "success", "request_id": request_id, "response": inner}}
            try:
                with self.lock:
                    self.proc.stdin.write(json.dumps(resp) + "\n")
                    self.proc.stdin.flush()
            except Exception:
                pass
            ev.set()

    # ------------------------------------------------ io
    def _read_loop(self):
        assert self.proc
        proc = self.proc          # the process THIS thread owns
        for line in self.proc.stdout:
            line = line.strip()
            if not line or not line.startswith("{"):
                # qwen-local's banner lines ([qwen-local] ...) land here.
                if line.startswith("[qwen-local]") or line.startswith("[the agent]"):
                    self._publish({"type": "log", "text": line})
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._dispatch(evt)
        # Switching sessions kills the old process, and its reader lands here — after
        # the replacement is already running. Announcing that would tell the page the
        # session is dead while a healthy one is serving it, which is what "session
        # ended (exit None)" was: the corpse of the previous process, not this one.
        if self.proc is not proc:
            return
        code = proc.returncode
        self.alive = False
        self._set_busy(False)
        self.dead_reason = ("session ended" if code in (None, 0)
                            else f"session ended (exit {code})")
        self._publish({"type": "status", "alive": False, "reason": self.dead_reason})

    def _dispatch(self, evt):
        t = evt.get("type")
        if t == "system" and evt.get("subtype") == "init":
            self.session_id = evt.get("session_id") or self.session_id
            self._publish({"type": "status", "alive": True,
                           "session_id": self.session_id,
                           "model": evt.get("model"),
                           "permission_mode": evt.get("permissionMode", PERMISSION_MODE)})
        elif t == "assistant":
            out = []
            for block in evt.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    out.append({"kind": "text", "text": block["text"]})
                    self.last_assistant_text = block["text"]
                elif block.get("type") == "thinking":
                    out.append({"kind": "thinking", "text": block.get("thinking", "")})
                elif block.get("type") == "tool_use":
                    out.append({"kind": "tool", "name": block.get("name"),
                                "input": _trim(block.get("input", {}))})
            if out:
                self._publish({"type": "assistant", "blocks": out})
            # Name what it is doing, not just that it is doing something: on a local
            # model a tool call can run for a while, and "searching the web" vs
            # "thinking" is the difference between waiting and wondering.
            tool = next((b for b in out if b["kind"] == "tool" and b.get("name")), None)
            if tool:
                self._set_busy(True, _real_tool(tool), _tool_detail(tool))
            elif any(b["kind"] == "text" for b in out):
                self._set_busy(True, "writing")
        elif t == "user":
            # tool_results travel back as user messages; images ride here.
            out = []
            content = evt.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if block.get("type") != "tool_result":
                        continue
                    inner = block.get("content", [])
                    if isinstance(inner, str):
                        out.append({"kind": "tool_text", "text": _trim(inner)})
                        out.extend(_image_blocks_from_text(inner))
                    elif isinstance(inner, list):
                        for b in inner:
                            if b.get("type") == "text":
                                out.append({"kind": "tool_text", "text": _trim(b["text"])})
                                out.extend(_image_blocks_from_text(b["text"]))
                            elif b.get("type") == "image":
                                src = b.get("source", {})
                                if src.get("type") == "base64":
                                    out.append({"kind": "image",
                                                "data": src["data"],
                                                "media_type": src.get("media_type", "image/png")})
            if out:
                self._publish({"type": "tool_results", "blocks": out})
                self._set_busy(True, "thinking")
        elif t == "result":
            # No lock: this reader thread is the only one that lowers the count, and a
            # write stuck on a full pipe must never be able to freeze the reader.
            self.outstanding = max(0, self.outstanding - 1)
            idle = self.outstanding == 0
            self._set_busy(not idle, "" if idle else "queued")
            self._publish({"type": "result", "is_error": evt.get("is_error", False),
                           "cost": evt.get("total_cost_usd"),
                           "text": _trim(str(evt.get("result", ""))[:4000])})
            if idle:                    # only a truly idle agent gets the next Continue
                self._maybe_continue(evt)
        elif t == "control_request":
            req = evt.get("request", {})
            if req.get("subtype") == "can_use_tool":
                rid = evt.get("request_id", str(uuid.uuid4()))
                waiter = threading.Event()
                self.pending_approvals[rid] = waiter
                # The CLI has spelled this field both ways across versions; echoing
                # the wrong one back as updatedInput would approve an empty tool call.
                tool_input = req.get("input") or req.get("tool_input") or {}
                self.pending_inputs[rid] = tool_input
                self._publish({"type": "approval", "request_id": rid,
                               "tool": req.get("tool_name"),
                               "input": _trim(tool_input)})
                threading.Thread(target=self._timeout_approval, args=(rid, waiter),
                                 daemon=True).start()

    def _timeout_approval(self, rid, ev):
        if not ev.wait(300):
            self.answer_approval(rid, "deny")
            self._publish({"type": "approval_timeout", "request_id": rid})

    # ------------------------------------------------ pubsub
    def _set_busy(self, on, activity="", detail=""):
        self.busy = on
        self.activity = activity
        self.detail = detail
        if on and not self.busy_since:
            self.busy_since = time.time()
        if not on:
            self.busy_since = 0.0
        self._publish({"type": "busy", "on": on, "activity": activity,
                       "detail": detail, "since": self.busy_since})

    def _publish(self, evt):
        evt["ts"] = time.time()
        self.last_event = evt["ts"]      # the watchdog's heartbeat
        self.replay.append(evt)
        for q in list(self.subs):
            q.put(evt)

    def subscribe(self, last_ts):
        q = _new_queue()
        for evt in self.replay:
            if evt["ts"] > last_ts:
                q.put(evt)
        self.subs.append(q)
        return q

    def unsubscribe(self, q):
        if q in self.subs:
            self.subs.remove(q)


def _new_queue():
    import queue
    return queue.Queue()


# The CLI tells the model about an image out of band: the stream-json tool result is
# only the line below, with no bytes in it, so the page had nothing to draw and the
# model's "the images are shown above" was wrong from the page's point of view.
# Turning the path into a /file URL puts the picture in the chat where it belongs.
# Lines in a tool's output that put a file in the feed: the harness's own "Read image file:",
# the image/voice helpers' "Image saved:" / "Audio saved:", and "SHOW:" for anything else.
_READ_IMAGE_RE = re.compile(r"^(?:Read image file|Image saved|Audio saved|SHOW):\s*(\S.*?)\s*$",
                            re.MULTILINE)


def _image_blocks_from_text(text):
    """Pictures and sound named in a tool's output, as feed blocks (images and audio)."""
    out, seen = [], set()
    for match in _READ_IMAGE_RE.finditer(text or ""):
        raw = match.group(1).strip().strip("'\"")
        path = Path(os.path.expanduser(raw))
        if not path.is_absolute():
            path = HOME / raw            # the CLI prints paths relative to its cwd
        if str(path) in seen or not path.is_file():
            continue
        seen.add(str(path))
        suffix = path.suffix.lower()
        kind = ("image_url" if suffix in VIEWABLE_SUFFIXES else
                "audio_url" if suffix in AUDIO_SUFFIXES else None)
        if kind:
            out.append({"kind": kind, "url": P("/file?path=" + quote(str(path))),
                        "name": path.name})
    return out


def _tool_detail(block):
    """A few words about WHAT the tool is doing — the command, the query, the path."""
    raw = block.get("input")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return ""
    if not isinstance(raw, dict):
        return ""
    args = raw.get("arguments") if isinstance(raw.get("arguments"), dict) else raw
    for key in ("command", "query", "file_path", "path", "url", "pattern"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            val = " ".join(val.split())
            return val[:70] + ("…" if len(val) > 70 else "")
    return ""


def _real_tool(block):
    """The tool a person would name, not the envelope it arrived in.

    The fork loads tools on demand, so what comes over the wire is `tool_search`
    ({"query": "select:mcp__local-search__web_search"}) and then `tool_call`
    ({"name": "mcp__local-search__web_search", ...}). Reporting those verbatim makes
    the phone say "tool call…" while it is actually searching the web.
    """
    name = block.get("name") or ""
    raw = block.get("input")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    if name == "tool_call":
        return str(raw.get("name") or name)
    if name == "tool_search":
        q = str(raw.get("query") or "")
        return q.split("select:", 1)[1].split(",")[0].strip() if "select:" in q else name
    return name


def _trim(obj, n=1500):
    s = obj if isinstance(obj, str) else json.dumps(obj)
    return s if len(s) <= n else s[:n] + f"... [{len(s)} chars]"


sess = Session()


# ---------------------------------------------------------------- routes
def authorized():
    if not TOKEN:
        return True
    if request.cookies.get("rq_token") == TOKEN:
        return True
    return request.args.get("k") == TOKEN


ENTRY_PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow"><title>Qwen Remote</title>
<style>body{margin:0;min-height:100dvh;display:flex;align-items:center;
justify-content:center;background:#0f1115;color:#e6e8ec;font:16px/1.5 -apple-system,
BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}form{width:min(420px,88vw);
background:#171a21;border:1px solid #262b36;border-radius:14px;padding:22px}
h1{font-size:17px;margin:0 0 6px}p{color:#8b93a7;font-size:13px;margin:0 0 16px}
input,button{width:100%;font-size:16px;padding:12px;border-radius:9px;
border:1px solid #262b36;background:#1e222b;color:#e6e8ec;margin-bottom:10px}
button{background:#7aa2f7;color:#fff;border:none;font-weight:600}</style>
<form onsubmit="event.preventDefault();
 var k=document.getElementById('k').value.trim();
 if(k)location.replace('/?k='+encodeURIComponent(k))">
 <h1>Qwen Remote</h1>
 <p>This session needs its key. It is the <code>?k=</code> part of the link, or scan
 the QR code again from the machine.</p>
 <input id="k" type="password" autocomplete="off" autocapitalize="none"
        autocorrect="off" spellcheck="false" placeholder="key" autofocus>
 <button type="submit">Open</button>
</form>"""


@app.route("/")
def index():
    if not authorized():
        # One field, no token in it. Submitting navigates to /?k=… which sets the
        # cookie for this browser (or this installed app's own cookie jar) and then
        # replaces the URL, so the key does not sit in the address bar or in history.
        return (_prefixed_text(ENTRY_PAGE), 403, {"Content-Type": "text/html; charset=utf-8"})
    resp = _prefixed_file(Path(__file__).parent / "static" / "index.html", "text/html")
    if request.args.get("k") == TOKEN:
        resp.set_cookie("rq_token", TOKEN, httponly=True, samesite="Lax", secure=bool(PREFIX), path=P("/"))
    return resp


def _announce_watchers():
    """Push the count so it changes the moment somebody arrives or leaves."""
    try:
        sess._publish({"type": "watchers", "n": _watchers()})
    except Exception:
        pass


@app.route("/watch")
def watch():
    """The viewer's page. Nothing on it, and no route it can reach, can act."""
    if not SHARE:
        return ("This stream is off.", 404)
    if not _may_watch():
        return ("This stream needs the share link.", 403)
    # The SAME page the operator gets. A second renderer could only ever drift from
    # this one — different markdown, no history, blocks laid out differently — so the
    # viewer runs the real thing in read-only mode, which it detects from its own path.
    # Hiding controls is cosmetic: what makes it read-only is that every mutating route
    # refuses a viewer, and the only feeds it can reach are /watch/*.
    resp = _prefixed_file(Path(__file__).parent / "static" / "index.html", "text/html")
    if request.args.get("v") == _view_token():
        # So a refresh keeps working without the secret sitting in the address bar.
        resp.set_cookie("rq_view", _view_token(), httponly=True, samesite="Lax", secure=bool(PREFIX), path=P("/"))
    return resp


@app.route("/watch/status")
def watch_status():
    if not _may_watch():
        return jsonify(live=False), 403
    # Deliberately less than /status: no session id, no mode, no permissions, no token.
    return jsonify(live=True, alive=sess.alive, busy=sess.busy,
                   activity=sess.activity, since=sess.busy_since,
                   watchers=_watchers())


@app.route("/watch/events")
def watch_events():
    if not _may_watch():
        return ("This stream needs the share link.", 403)
    last = float(request.args.get("last", 0) or 0)
    q = sess.subscribe(last)
    me = uuid.uuid4().hex
    with WATCH_LOCK:
        WATCHERS.add(me)
    _announce_watchers()

    def stream():
        try:
            while True:
                try:
                    evt = q.get(timeout=25)
                except Exception:
                    yield ": keep-alive\n\n"
                    continue
                if not SHARE:              # unticked mid-stream: the feed just ends
                    return
                safe = _public_safe(evt)
                if safe is not None:
                    yield "data: " + json.dumps(safe) + "\n\n"
        finally:
            # A phone that sleeps or a tab that closes ends the generator, which is
            # what makes the count fall on its own without a heartbeat to maintain.
            sess.unsubscribe(q)
            with WATCH_LOCK:
                WATCHERS.discard(me)
            _announce_watchers()

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/watch/transcript")
def watch_transcript():
    """The live session's history, so a viewer sees the whole thing and not just the
    tail of the replay ring.

    Only the session currently being driven: a viewer has no business reading the
    others, and no way to name one — the id is ignored and the live one is used.
    """
    if not _may_watch():
        return ("This stream needs the share link.", 403)
    sid = sess.session_id
    if not sid:
        return jsonify(events=[])
    return jsonify(events=[e for e in (_public_safe(x) for x in _transcript(sid))
                           if e is not None])


@app.route("/desktop", methods=["POST"])
def desktop_toggle():
    """The checkbox behind the agent's keyboard, mouse and screenshots."""
    global DESKTOP
    if not authorized():
        return "nope", 403
    if not DESKTOP_OK:
        return jsonify(ok=False, desktop=False, error="this box has no desktop"), 403
    DESKTOP = bool((request.json or {}).get("on"))
    _save_prefs(desktop=DESKTOP)
    sess._publish({"type": "log", "text": (
        "DESKTOP DRIVER ON — the agent can move your pointer, type on your keyboard "
        "and photograph your screen." if DESKTOP else "desktop driver off")})
    return jsonify(ok=True, desktop=DESKTOP)


@app.route("/desktop", methods=["GET"])
def desktop_state():
    """Asked by the desktop tools before they touch anything. Loopback only."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify(on=False), 403
    return jsonify(on=DESKTOP)



PORT = 8491


def _deliver(text, display):
    """Hand the agent a message the way /inject does: now if idle, else queued."""
    if not sess.busy:
        sess.autopilot_rounds = 0
        sess.autopilot_stop = False
        sess.send(text if not AUTOPILOT else text + "\n\n" + CONTINUE_PROMPT, display=display)
        return "now"
    _queue_injection(text)
    return "when the turn ends"


@app.route("/share", methods=["POST"])
def share_toggle():
    """The checkbox. Operator only, and it returns the link to hand out."""
    global SHARE
    if not authorized():
        return "nope", 403
    body = request.json or {}
    SHARE = bool(body.get("on"))
    if body.get("rotate"):
        try:
            VIEW_FILE.unlink()             # every link handed out so far stops working
        except OSError:
            pass
    _save_prefs(share=SHARE)
    sess._publish({"type": "log", "text": (
        "SHARED STREAM ON — anyone with the watch link can read this session, including "
        "every tool call and its output." if SHARE else "shared stream off")})
    return jsonify(ok=True, share=SHARE,
                   url=(P(f"/watch?v={_view_token()}") if SHARE else ""))


@app.route("/healthz")
def healthz():
    return jsonify(ok=True)


# ── PWA ─────────────────────────────────────────────────────────────────────────────
# The manifest, the icons and the service worker are served WITHOUT the token on
# purpose: iOS fetches them while installing and on launch, sometimes without the
# session cookie, and a 403 there makes the install fail or the icon come out blank.
#
# Which is exactly why the manifest must not CONTAIN the token. It used to put it in
# start_url to solve the installed-app cookie jar, and the result was the session
# token published at a public URL — full operator control of an agent running with no
# approvals, for anyone who fetched it. The cookie jar is now solved at the other end:
# an unauthenticated "/" serves a one-field form instead of a bare 403.

@app.route("/manifest.webmanifest")
def manifest():
    # No secret here, ever. An installed app opening "/" with an empty cookie jar
    # lands on the key form, which sets the cookie for that jar once and for good.
    return jsonify({
        "name": TITLE,
        "short_name": "Qwen",
        "description": "A Meeseeks box: one Qwen agent session",
        "start_url": P("/"),
        "scope": P("/"),
        "display": "standalone",
        "orientation": "any",
        "background_color": "#14161b",
        "theme_color": "#14161b",
        "icons": [
            {"src": P("/icon-192.png"), "sizes": "192x192", "type": "image/png"},
            {"src": P("/icon-256.png"), "sizes": "256x256", "type": "image/png"},
            {"src": P("/icon-512.png"), "sizes": "512x512", "type": "image/png"},
            {"src": P("/icon-maskable-512.png"), "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
    })


@app.route("/summon.m4a")
def summon_audio():
    """The pre-recorded "I'm Mr. Meeseeks! Look at me!" played when a box is pressed."""
    if not authorized():
        return "nope", 403
    return send_file(Path(__file__).parent / "static" / "summon.m4a", mimetype="audio/mp4")


@app.route("/icon-<name>.png")
def icon(name):
    safe = {"180", "192", "256", "512", "maskable-512"}
    if name not in safe:
        return "nope", 404
    return send_file(Path(__file__).parent / "static" / f"icon-{name}.png",
                     mimetype="image/png", max_age=86400)


@app.route("/sw.js")
def service_worker():
    # Must be served from the root for its scope to cover the whole app.
    return _prefixed_file(Path(__file__).parent / "static" / "sw.js", "application/javascript")


@app.route("/status")
def status():
    if not authorized():
        return "nope", 403
    return jsonify(alive=sess.alive, session_id=sess.session_id,
                   mode=PERMISSION_MODE, reason=sess.dead_reason,
                   pending_messages=_pending_count(),
                   desktop=DESKTOP,
                   share=SHARE, share_url=(P(f"/watch?v={_view_token()}") if SHARE else ""),
                   watchers=_watchers(),
                   autopilot=AUTOPILOT, autopilot_max=AUTOPILOT_MAX,
                   autopilot_round=sess.autopilot_rounds,
                   busy=sess.busy, activity=sess.activity, since=sess.busy_since,
                   detail=getattr(sess, "detail", ""),
                   box={"title": TITLE, "terms": TERMS, "terms_text": TERMS_TEXT,
                        "agreed": _terms_agreed(),
                        "desktop": DESKTOP_OK})


# ── sessions ────────────────────────────────────────────────────────────────────────
# The fork writes one JSONL transcript per session under
# ~/.qwen/projects/<slug>/chats/<session-id>.jsonl, where <slug> is the working
# directory with separators replaced. Those files ARE the session list; this server
# only adds the names, because the fork has nowhere to put a human title.
CHATS_DIR = HOME / ".qwen/projects" / ("-" + str(HOME).strip("/").replace("/", "-")) / "chats"
NAMES_FILE = Path(__file__).parent / "session-names.json"


def _names():
    try:
        return json.loads(NAMES_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_names(d):
    NAMES_FILE.write_text(json.dumps(d, indent=1))


def _first_user_line(path, limit=80):
    """The opening prompt, as a fallback title — what the session was about."""
    try:
        with path.open() as fh:
            for line in fh:
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if evt.get("type") != "user":
                    continue
                msg = evt.get("message", {})
                # The fork writes Gemini-shaped messages ({"parts": [{"text": ...}]});
                # "content" is the Anthropic shape the old the agent sessions used.
                # Read both, so transcripts from either era still get a title.
                content = msg.get("content")
                if content is None:
                    content = msg.get("parts")
                if isinstance(content, list):
                    content = " ".join(b.get("text", "") for b in content
                                       if isinstance(b, dict))
                text = " ".join(str(content or "").split())
                if text:
                    return text[:limit] + ("…" if len(text) > limit else "")
    except OSError:
        pass
    return ""


AUTO_NAMES = Path(__file__).parent / "auto-names.json"
_naming = set()          # sessions a worker is already titling
_naming_lock = threading.Lock()


def _auto_names():
    try:
        return json.loads(AUTO_NAMES.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _first_exchange(path, limit=1200):
    """The opening prompt and the reply to it — enough to title the session."""
    user = reply = ""
    try:
        with path.open() as fh:
            for line in fh:
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = evt.get("message") or {}
                parts = msg.get("parts") if isinstance(msg.get("parts"), list) else None
                content = msg.get("content") if parts is None else None
                if isinstance(content, list):
                    text = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
                elif isinstance(content, str):
                    text = content
                else:
                    text = " ".join(b.get("text", "") for b in (parts or [])
                                    if isinstance(b, dict) and not b.get("thought"))
                text = " ".join(str(text or "").split())
                if not text:
                    continue
                if evt.get("type") == "user" and not user:
                    user = text[:limit]
                elif evt.get("type") == "assistant" and user and not reply:
                    reply = text[:limit]
                if user and reply:
                    break
    except OSError:
        pass
    return user, reply


def _title_session(sid, path):
    """Ask the local model for a short title. Costs nothing and leaves nothing."""
    user, reply = _first_exchange(path)
    if not user:
        return
    prompt = ("Title this conversation in 2 to 5 words, like a chat app would. "
              "Reply with the title only: no quotes, no punctuation at the end, "
              "no preamble.\n\nUser: " + user + "\n\nAssistant: " + reply)
    try:
        req = urllib.request.Request(
            os.environ.get("OPENAI_BASE_URL", "").rstrip("/") + "/chat/completions",
            data=json.dumps({"model": os.environ.get("OPENAI_MODEL", ""),
                             "messages": [{"role": "user", "content": prompt}],
                             "max_tokens": 24, "temperature": 0,
                             "chat_template_kwargs": {"enable_thinking": False}}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + os.environ.get("OPENAI_API_KEY", "")},
            method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            title = json.load(resp)["choices"][0]["message"]["content"]
    except Exception:
        return
    title = " ".join(str(title or "").split()).strip(" .\"'")[:60]
    if not title:
        return
    names = _auto_names()
    names[sid] = title
    try:
        AUTO_NAMES.write_text(json.dumps(names, indent=1))
    except OSError:
        pass


def _title_worker(pending):
    for sid, path in pending:
        try:
            _title_session(sid, path)
        finally:
            with _naming_lock:
                _naming.discard(sid)


def _session_list():
    names = _names()
    autos = _auto_names()
    out = []
    pending = []
    if CHATS_DIR.is_dir():
        for f in CHATS_DIR.glob("*.jsonl"):
            sid = f.stem
            try:
                st = f.stat()
            except OSError:
                continue
            # A title you set wins; otherwise the model's short one; otherwise the
            # opening prompt, which is only a placeholder until the title lands.
            if sid not in names and sid not in autos:
                with _naming_lock:
                    if sid not in _naming:
                        _naming.add(sid)
                        pending.append((sid, f))
            out.append({
                "id": sid,
                "name": names.get(sid) or autos.get(sid) or _first_user_line(f, 48)
                        or "Untitled session",
                "named": sid in names,
                "updated": st.st_mtime,
                "bytes": st.st_size,
                "current": sid == sess.session_id,
            })
    out.sort(key=lambda r: r["updated"], reverse=True)
    if pending:
        # Titling happens off the request: the list has to come back instantly, and a
        # model call per untitled session would make opening the app feel broken.
        threading.Thread(target=_title_worker, args=(pending,), daemon=True).start()
    return out


@app.route("/sessions")
def sessions():
    if not authorized():
        return "nope", 403
    return jsonify(sessions=_session_list(), current=sess.session_id)


# Replayed images are inlined as base64, so a screenshot-heavy session could hand a
# phone tens of megabytes over the tunnel. Newest images win: those are the ones near
# where you resume reading. Older ones are replaced by a one-line note.
_IMAGE_BUDGET = 6_000_000


def _transcript(sid, limit=400):
    """Replay a stored session as the blocks the page already knows how to render.

    Resuming restores the MODEL's memory (the fork reloads the transcript), but the
    page starts empty, which reads as "the history is gone". This turns the stored
    JSONL back into the same event shapes _dispatch publishes live, so reopening a
    session looks like scrolling up in it.

    The fork writes Gemini-shaped messages: {"role", "parts":[{"text"|"thought"|
    "functionCall"|"functionResponse"}]}.
    """
    path = CHATS_DIR / f"{sid}.jsonl"
    events = []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return events
    for line in lines[-limit:]:
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = evt.get("type")
        parts = (evt.get("message") or {}).get("parts")
        if not isinstance(parts, list):
            continue
        if kind == "user":
            text = " ".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
            if text:
                events.append({"type": "you", "text": text})
            # An image you attached to a message is part of the conversation too.
            shots = [{"kind": "image", "data": b["inlineData"]["data"],
                      "media_type": b["inlineData"].get("mimeType", "image/png"),
                      "name": b["inlineData"].get("displayName", "")}
                     for b in parts
                     if isinstance(b, dict) and (b.get("inlineData") or {}).get("data")
                     and str(b["inlineData"].get("mimeType", "")).startswith("image/")]
            if shots:
                events.append({"type": "tool_results", "blocks": shots})
        elif kind == "assistant":
            blocks = []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if part.get("functionCall"):
                    call = part["functionCall"]
                    blocks.append({"kind": "tool", "name": call.get("name"),
                                   "input": _trim(call.get("args", {}))})
                elif part.get("text"):
                    # `thought` marks the model's reasoning; the live path shows those
                    # as thinking blocks, so keep the distinction on replay too.
                    blocks.append({"kind": "thinking" if part.get("thought") else "text",
                                   "text": part["text"]})
            if blocks:
                events.append({"type": "assistant", "blocks": blocks})
        elif kind == "tool_result":
            blocks = []
            for part in parts:
                resp = (part or {}).get("functionResponse") or {}
                out = resp.get("response", {})
                text = out.get("output") if isinstance(out, dict) else out
                if text:
                    blocks.append({"kind": "tool_text", "text": _trim(str(text))})
                    blocks.extend(_image_blocks_from_text(str(text)))
                # Images a tool returned. They live at
                # functionResponse.parts[].inlineData ({mimeType, data, displayName})
                # — a level deeper than the text, which is why replay showed the
                # "Image overview: 192x192" caption but not the picture.
                for inner in resp.get("parts") or []:
                    blob = (inner or {}).get("inlineData") or {}
                    if blob.get("data") and str(blob.get("mimeType", "")).startswith("image/"):
                        blocks.append({"kind": "image", "data": blob["data"],
                                       "media_type": blob["mimeType"],
                                       "name": blob.get("displayName", "")})
            if blocks:
                events.append({"type": "tool_results", "blocks": blocks})
    # Trim images from the OLDEST end until the payload is sane.
    budget = _IMAGE_BUDGET
    dropped = 0
    for event in reversed(events):
        kept = []
        for block in event.get("blocks", []):
            if block.get("kind") == "image":
                size = len(block.get("data", ""))
                if size > budget:
                    dropped += 1
                    kept.append({"kind": "tool_text",
                                 "text": f"[image not replayed: {block.get('name') or 'image'}]"})
                    continue
                budget -= size
            kept.append(block)
        if "blocks" in event:
            event["blocks"] = kept
    if dropped:
        events.insert(0, {"type": "you" if False else "tool_results",
                          "blocks": [{"kind": "tool_text",
                                      "text": f"[{dropped} older image(s) omitted to keep this "
                                              f"transcript small — they are still in the session]"}]})
    return events


@app.route("/sessions/transcript")
def sessions_transcript():
    if not authorized():
        return "nope", 403
    sid = request.args.get("id", "")
    if not any(r["id"] == sid for r in _session_list()):
        return jsonify(error="unknown session"), 404
    return jsonify(events=_transcript(sid))


@app.route("/sessions/open", methods=["POST"])
def sessions_open():
    if not authorized():
        return "nope", 403
    sid = (request.json or {}).get("id", "")
    # Only ids we actually have transcripts for: this string becomes a --resume
    # argument to a subprocess.
    if not any(r["id"] == sid for r in _session_list()):
        return jsonify(error="unknown session"), 404
    restarted = sess.open_session(sid)
    return jsonify(ok=True, id=sid, restarted=bool(restarted))


@app.route("/sessions/name", methods=["POST"])
def sessions_name():
    if not authorized():
        return "nope", 403
    body = request.json or {}
    sid, name = body.get("id", ""), " ".join(str(body.get("name", "")).split())[:80]
    if not any(r["id"] == sid for r in _session_list()):
        return jsonify(error="unknown session"), 404
    names = _names()
    if name:
        names[sid] = name
    else:
        names.pop(sid, None)   # cleared name falls back to the model's title
    _save_names(names)
    return jsonify(ok=True, id=sid, name=name)


TRASH_DIR = Path(__file__).parent / "deleted-sessions"


@app.route("/sessions/delete", methods=["POST"])
def sessions_delete():
    if not authorized():
        return "nope", 403
    sid = (request.json or {}).get("id", "")
    if not any(r["id"] == sid for r in _session_list()):
        return jsonify(error="unknown session"), 404

    # Resolve inside CHATS_DIR and verify the parent: `sid` reaches this from a phone
    # and ends up in a filesystem path, so it does not get to contain "..".
    target = (CHATS_DIR / f"{sid}.jsonl").resolve()
    if target.parent != CHATS_DIR.resolve() or not target.is_file():
        return jsonify(error="refusing that path"), 400

    # Moved, not unlinked. This is a swipe on a phone — the gesture is one thumb away
    # from the one that opens a session, and a mis-swipe should not be permanent.
    # Recover with: mv relay/deleted-sessions/<id>.jsonl <chats dir>/
    TRASH_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        target.rename(TRASH_DIR / f"{sid}.{stamp}.jsonl")
    except OSError as exc:
        return jsonify(error=f"could not delete: {exc}"), 500

    names = _names()
    if names.pop(sid, None) is not None:
        _save_names(names)

    # Deleting the session you are sitting in leaves the subprocess resuming a
    # transcript that no longer exists; start a fresh one instead.
    if sess.session_id == sid:
        sess.new_chat()
    return jsonify(ok=True, id=sid, trashed=True)


def _terms_agreed():
    if not TERMS:
        return True
    try:
        return bool(json.loads(TERMS_FILE.read_text()).get("agreed"))
    except (OSError, ValueError):
        return False


@app.route("/terms", methods=["POST"])
def terms():
    if not authorized():
        return "nope", 403
    agreed = bool((request.json or {}).get("agreed"))
    TERMS_FILE.write_text(json.dumps({"agreed": agreed, "text": TERMS_TEXT,
                                      "at": time.time()}))
    return jsonify(ok=True, agreed=agreed)


@app.route("/send", methods=["POST"])
def send():
    if not authorized():
        return "nope", 403
    if not _terms_agreed():
        return jsonify(error="agree to the terms first", terms=TERMS_TEXT), 428
    body = request.json or {}
    text = body.get("text", "").strip()
    if not text:
        return jsonify(error="empty"), 400
    # `display` is what the person typed; `text` is what the model gets (it may carry
    # appended attachment paths). Every device shows the former.
    # Your own message is the budget reset: autopilot counts continues since the last
    # thing a person said, not since the session began.
    sess.autopilot_rounds = 0
    sess.autopilot_stop = False
    sess.send(text, body.get("display"), body.get("images"), body.get("uid", ""))
    return jsonify(ok=True)


# ── the ask channel ─────────────────────────────────────────────────────────────────
# One mechanism behind two features: a tool asking permission to run a command, and
# the agent asking a multiple-choice question. The fork has no protocol for either
# (it never emits a control_request, verified), so the tools that need an answer call
# in here over localhost, this publishes the question to the phone, and the reply
# comes back through /ui/answer.
_asks = {}                     # id -> {"event": Event, "answer": str|None, "meta": {...}}
_asks_lock = threading.Lock()
ASK_TIMEOUT = float(os.environ.get("RQ_ASK_TIMEOUT", "900"))


@app.route("/ui/ask", methods=["POST"])
def ui_ask():
    """Called by our MCP tools. Blocks until the phone answers, or times out."""
    if not authorized():
        return "nope", 403
    body = request.json or {}
    kind = body.get("kind", "question")           # "approval" | "question"
    text = str(body.get("text", ""))[:2000]
    options = [str(o)[:120] for o in (body.get("options") or [])][:6]
    detail = str(body.get("detail", ""))[:4000]
    aid = str(uuid.uuid4())
    ev = threading.Event()
    with _asks_lock:
        _asks[aid] = {"event": ev, "answer": None}
    sess._publish({"type": "ask", "id": aid, "kind": kind, "text": text,
                   "options": options, "detail": detail})
    # The spinner would otherwise keep saying "thinking" while nothing happens.
    sess._set_busy(True, "waiting for you")
    answered = ev.wait(ASK_TIMEOUT)
    with _asks_lock:
        entry = _asks.pop(aid, {})
    if not answered:
        sess._publish({"type": "log", "text": "[no answer — assuming no]"})
        return jsonify(answer=None, timed_out=True)
    sess._set_busy(True, "thinking")
    return jsonify(answer=entry.get("answer"), timed_out=False)


@app.route("/ui/answer", methods=["POST"])
def ui_answer():
    """Called by the page when you tap a button."""
    if not authorized():
        return "nope", 403
    body = request.json or {}
    aid, answer = body.get("id", ""), body.get("answer")
    with _asks_lock:
        entry = _asks.get(aid)
        if not entry:
            return jsonify(error="that question is no longer open"), 404
        entry["answer"] = answer
        entry["event"].set()
    sess._publish({"type": "answered", "id": aid, "answer": answer})
    return jsonify(ok=True)


UPLOADS = HOME / "Pictures/agent-uploads"
MAX_UPLOAD = 25 * 1024 * 1024


@app.route("/upload", methods=["POST"])
def upload():
    """Take a picture from the phone and put it where the agent can read it.

    The agent has no way to receive bytes from the page — it reads files off this
    machine — so an attachment becomes a file, and the prompt carries its path.
    """
    if not authorized():
        return "nope", 403
    f = request.files.get("file")
    if f is None:
        return jsonify(error="no file"), 400
    raw = f.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        return jsonify(error=f"image is larger than {MAX_UPLOAD // 1024 // 1024} MB"), 413
    suffix = Path(f.filename or "photo.jpg").suffix.lower()
    if suffix not in VIEWABLE_SUFFIXES:
        return jsonify(error=f"{suffix or 'that'} is not an image type"), 400
    # iOS hands every photo over as "image.jpg", so the timestamp is what keeps them
    # apart — and keeps one upload from overwriting the last.
    UPLOADS.mkdir(parents=True, exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6] + suffix
    dest = UPLOADS / name
    dest.write_bytes(raw)
    return jsonify(ok=True, path=str(dest), url=P("/file?path=" + quote(str(dest))),
                   name=name, bytes=len(raw))


PRIORITY_EVERY = float(os.environ.get("RQ_PRIORITY_EVERY", "600"))
_priority_last = [0.0]
RESUME_NOTE = (
    "This message interrupted your previous turn at a safe point, on purpose: it could "
    "not wait for the turn to end. Do what it asks first. Then go back to what you were "
    "doing -- nothing was thrown away: your uncommitted work is still in the tree (git "
    "status), and plan_status says where you were.")


@app.route("/priority", methods=["POST"])
def priority():
    """Interrupt the current turn at a safe point and hand the agent one urgent message.

    Claude, 2026-09-25, for the owner: a ready merge sat for hours behind one long turn,
    because /inject waits for the turn to end. This asks the harness itself to stop
    the turn (the stream-json `interrupt` control request: it cancels the turn with a
    recoverable TurnInterruptedError -- the process, the conversation and the files on
    disk all stay, unlike /interrupt, which respawns the agent and turns autopilot
    off). The message goes next, carrying anything already queued, with a note telling
    the agent to resume afterwards. Autopilot is left as it was, and the busy count
    keeps a stray Continue from following the interrupted turn. At most one every
    RQ_PRIORITY_EVERY seconds, so it cannot become a way to keep an agent from working.
    """
    if not authorized():
        return "nope", 403
    body = request.json or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify(error="empty"), 400
    now = time.time()
    if now - _priority_last[0] < PRIORITY_EVERY:
        wait = int(PRIORITY_EVERY - (now - _priority_last[0]))
        return jsonify(error=f"one priority message every {int(PRIORITY_EVERY)}s; "
                             f"the next in {wait}s"), 429
    _priority_last[0] = now
    interrupted = False
    if sess.busy and sess.alive and sess.proc:
        req = {"type": "control_request", "request_id": str(uuid.uuid4()),
               "request": {"subtype": "interrupt"}}
        try:
            with sess.lock:
                sess.proc.stdin.write(json.dumps(req) + "\n")
                sess.proc.stdin.flush()
            interrupted = True
        except Exception as exc:
            return jsonify(error=f"could not interrupt: {exc}"), 500
    waiting = _drain_injections()
    parts = [text] + waiting + [RESUME_NOTE if interrupted else ""]
    msg = "\n\n".join(p for p in parts if p)
    if AUTOPILOT:
        msg += "\n\n" + CONTINUE_PROMPT
    sess.autopilot_rounds = 0
    sess.autopilot_stop = False
    sess._publish({"type": "log", "text": "priority message: "
                   + ("interrupted the turn at a safe point" if interrupted else "agent was idle")
                   + (f", with {len(waiting)} queued message(s)" if waiting else "")})
    sess.send(msg, display=(body.get("display") or "\u26a1 priority message"))
    return jsonify(ok=True, interrupted=interrupted, carried=len(waiting))


@app.route("/interrupt", methods=["POST"])
def interrupt():
    """Abandon the current turn but keep the conversation.

    A wedged turn is not hypothetical: a run_shell_command once disappeared without
    ever returning a result, leaving the agent waiting on a tool that would never
    answer while the model sat idle — five minutes of spinner with no way out from a
    phone. This kills the process and starts it again on the SAME session, so the
    history survives and the next message continues the conversation.
    """
    if not authorized():
        return "nope", 403
    global AUTOPILOT
    if AUTOPILOT:
        # Stop has to mean stop. Leaving autopilot armed would send a "continue" a
        # second after the tap and look like the button did nothing.
        AUTOPILOT = False
        _save_prefs(autopilot=False)
        sess._publish({"type": "log", "text": "autopilot off (you pressed stop)"})
    sess.autopilot_stop = True
    sid = sess.session_id
    if sid:
        # force: this is the one caller that means "respawn the SAME session", which
        # is exactly what the already-open guard would otherwise skip.
        sess.open_session(sid, force=True)
    else:
        sess.new_chat()
    sess._publish({"type": "log", "text": "[turn stopped]"})
    return jsonify(ok=True, resumed=sid)


@app.route("/new", methods=["POST"])
def new():
    if not authorized():
        return "nope", 403
    sess.new_chat()
    return jsonify(ok=True)


@app.route("/mode", methods=["POST"])
def mode():
    global PERMISSION_MODE, AUTOPILOT
    if not authorized():
        return "nope", 403
    m = (request.json or {}).get("mode")
    if m in _APPROVAL_MODES and m != PERMISSION_MODE:
        PERMISSION_MODE = m
        _save_mode(m)
        if m != "bypassPermissions" and AUTOPILOT:
            # Autopilot without yolo would stall on the first approval with nobody
            # watching, so the two cannot disagree.
            AUTOPILOT = False
            _save_prefs(autopilot=False)
            sess._publish({"type": "log", "text": "autopilot off (it needs 'auto (no asks)')"})
        # The mode is a process argument, so it needs a fresh agent — but NOT a fresh
        # chat. This used to call new_chat(), which threw the conversation away, so
        # switching to "no asks" cost you the thread you were switching it for and
        # nobody ever switched. Resuming the same session id keeps the history.
        if sess.session_id:
            sess.open_session(sess.session_id, force=True)
        else:
            sess.new_chat()
    return jsonify(ok=True, mode=PERMISSION_MODE)


@app.route("/say", methods=["POST"])
def say():
    """Put a line in the feed without prompting the agent.

    For things the owner should see as they happen but the agent must not be interrupted
    by — a review request it just filed, a job that started. It reaches every open
    device, including the shared stream, because it goes through the ordinary event
    ring.
    """
    if not authorized():
        return "nope", 403
    text = ((request.json or {}).get("text") or "").strip()
    if not text:
        return jsonify(error="empty"), 400
    sess._publish({"type": "log", "text": text[:2000]})
    return jsonify(ok=True)


@app.route("/inject", methods=["POST"])
def inject():
    """Give the agent a message WITHOUT interrupting it.

    If it is mid-turn the text waits and is delivered as the opening of its next turn,
    carried out on the same continue that keeps it going — so it arrives as an
    instruction rather than as an interruption in the middle of a thought. If it is
    idle there is nothing to wait for and it goes at once.
    """
    if not authorized():
        return "nope", 403
    if not _terms_agreed():
        return jsonify(error="agree to the terms first", terms=TERMS_TEXT), 428
    body = request.json or {}
    if body.get("clear"):
        # Delivering the same thing twice is its own kind of noise: /nudge sends
        # directly and leaves any queued copy behind, so the sender can drop it.
        dropped = len(_drain_injections())
        return jsonify(ok=True, dropped=dropped, pending=_pending_count())
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify(error="empty"), 400

    if not sess.busy:
        sess.autopilot_rounds = 0
        sess.autopilot_stop = False
        sess.send(text if not AUTOPILOT else text + "\n\n" + CONTINUE_PROMPT,
                  display=(body.get("display") or "↻ message"))
        return jsonify(ok=True, delivered="now", pending=_pending_count())

    n = _queue_injection(text)
    sess._publish({"type": "log",
                   "text": f"a message is queued and will be delivered when this turn "
                           f"ends ({n} waiting)"})
    return jsonify(ok=True, delivered="when the turn ends", pending=n)


@app.route("/nudge", methods=["POST"])
def nudge():
    """Wake the session with a message it did not ask for.

    This is how a long job gets the agent moving again: rather than holding an MCP call
    open for ten minutes — during which the session can do nothing else and the model
    sits idle — the work is started detached, a watcher keeps an eye on it, and the
    result arrives here when it lands.

    Operator-authenticated, because it puts words in the session's mouth.
    """
    if not authorized():
        return "nope", 403
    body = request.json or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify(error="empty"), 400
    # The same budget reset a person's message gets: a finished job is real news, and
    # the work that follows it should not be cut short by an autopilot count.
    sess.autopilot_rounds = 0
    sess.autopilot_stop = False
    if sess.busy:
        # Mid-turn it would join the harness's queue behind everything else and arrive
        # as stale news; queued here it goes with the next delivery, when the agent is idle.
        n = _queue_injection(text)
        return jsonify(ok=True, delivered="when the turn ends", pending=n)
    sess.send(text, display=(body.get("display") or "↻ " + text[:80]))
    return jsonify(ok=True)


@app.route("/autopilot", methods=["POST"])
def autopilot():
    """The checkbox: no approvals, and keep going until the agent says it is done."""
    global AUTOPILOT, PERMISSION_MODE
    if not authorized():
        return "nope", 403
    on = bool((request.json or {}).get("on"))
    AUTOPILOT = on
    sess.autopilot_rounds = 0
    sess.autopilot_stop = not on          # cancels a continue already waiting to fire
    _save_prefs(autopilot=on)

    if on and PERMISSION_MODE != "bypassPermissions":
        # An unattended run that stops for approval is the one thing this must not do.
        PERMISSION_MODE = "bypassPermissions"
        _save_mode(PERMISSION_MODE)
        if sess.session_id:
            sess.open_session(sess.session_id, force=True)
        else:
            sess.new_chat()

    sess._publish({"type": "log", "text": (
        f"autopilot ON — no approvals, and it will be told to continue after every turn "
        + (f"(up to {AUTOPILOT_MAX}) " if AUTOPILOT_MAX else "for as long as it takes, ")
        + f"until it replies {DONE_SENTINEL}. Stop, a message, or unticking ends it."
        if on else "autopilot off")})
    return jsonify(ok=True, autopilot=AUTOPILOT, mode=PERMISSION_MODE)


@app.route("/approve", methods=["POST"])
def approve():
    if not authorized():
        return "nope", 403
    body = request.json or {}
    sess.answer_approval(body.get("request_id", ""),
                         "allow" if body.get("allow") else "deny")
    return jsonify(ok=True)


@app.route("/events")
def events():
    if not authorized():
        return "nope", 403
    last = float(request.args.get("last", 0))
    q = sess.subscribe(last)

    def stream():
        try:
            while True:
                try:
                    evt = q.get(timeout=25)
                except Exception:
                    yield ": keep-alive\n\n"
                    continue
                yield "data: " + json.dumps(evt) + "\n\n"
        finally:
            sess.unsubscribe(q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/file")
def file_route():
    if not authorized():
        return "nope", 403
    raw = request.args.get("path", "")
    try:
        p = Path(raw).resolve()
    except Exception:
        return "bad path", 400
    if not p.is_file():
        return "not found", 404
    # symlink-safe allow-list: the resolved path must sit under a root
    if not any(p == r or r in p.parents for r in (r.resolve() for r in FILE_ROOTS)):
        return "not viewable here", 403
    if any(p == d or d in p.parents for d in (d for d in FILE_DENY)):
        return "not viewable here", 403
    if p.suffix.lower() not in VIEWABLE_SUFFIXES | AUDIO_SUFFIXES:
        return "not an image or sound", 403
    return send_file(p)


# A turn is "stalled" when the session is busy but nothing at all has come out of the
# agent for this long. Generous on purpose: a real answer emits thinking, tool calls
# and text continuously, so silence this long means the turn is not coming back. Seen
# for real when a run_shell_command vanished without returning a result.
STALL_SECONDS = float(os.environ.get("RQ_STALL_SECONDS", "300"))


def _inject_valve():
    """Deliver a queued message that has waited too long, even mid-turn.

    The queue exists so a message arrives between thoughts rather than inside one. When
    a single turn outlasts the wait, "between thoughts" never comes and the polite path
    silently becomes a dead letter. Interrupting is then the lesser harm, and it says so
    on the page rather than pretending it waited.
    """
    while True:
        time.sleep(30)
        try:
            items = _overdue()
            if not items:
                continue
            body = "\n\n".join(items)
            text = (body + "\n\n" + CONTINUE_PROMPT) if AUTOPILOT else body
            sess.autopilot_rounds = 0
            sess.autopilot_stop = False
            sess._publish({"type": "log",
                           "text": f"a queued message waited {INJECT_MAX_WAIT / 60:.0f} "
                                   f"minutes for this turn to end; delivering it anyway"})
            sess.send(text, display="↻ queued message (the turn ran long)")
        except Exception:
            continue


def _watchdog():
    """End turns that will never end themselves.

    Without this a wedged turn spins until someone notices and presses Stop — which
    is no help at all if the phone is in a pocket, or if the person is waiting on the
    answer to know something is wrong.
    """
    while True:
        time.sleep(15)
        try:
            if not (sess.busy and sess.alive):
                continue
            idle = time.time() - sess.last_event
            if idle < STALL_SECONDS:
                continue
            sid = sess.session_id
            sess._publish({"type": "log",
                           "text": f"[no output for {int(idle)}s — ending this turn; "
                                   f"the conversation is kept]"})
            if sid:
                sess.open_session(sid)
            else:
                sess.new_chat()
        except Exception:
            # A crashed watchdog is worse than a slow turn: never let it die.
            continue


def main():
    global TOKEN, PERMISSION_MODE, AUTOPILOT, SHARE, DESKTOP, TITLE, TERMS, PREFIX, DESKTOP_OK
    global PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8491)
    ap.add_argument("--token", default="")
    # Preferred over --token: a process's arguments are readable by every user on the
    # machine, so a token passed that way is visible to any guest box's agent.
    ap.add_argument("--token-file", default="")
    ap.add_argument("--title", default=TITLE)
    ap.add_argument("--terms", action="store_true")
    ap.add_argument("--prefix", default="")   # e.g. /box
    ap.add_argument("--no-desktop", action="store_true")
    ap.add_argument("--mode", default="acceptEdits")
    a = ap.parse_args()
    TOKEN = Path(a.token_file).read_text().strip() if a.token_file else a.token
    TITLE, TERMS = a.title, a.terms
    DESKTOP_OK = not a.no_desktop
    PREFIX = "/" + a.prefix.strip("/") if a.prefix.strip("/") else ""
    if PREFIX:
        app.wsgi_app = _Prefixed(app.wsgi_app)
    PERMISSION_MODE = _load_mode(a.mode)
    AUTOPILOT = bool(_prefs().get("autopilot")) and PERMISSION_MODE == "bypassPermissions"
    SHARE = bool(_prefs().get("share"))
    # Deliberately NOT restored from prefs: see the note by the flag.
    DESKTOP = False
    PORT = a.port
    if SHARE:
        print("[serve] SHARED STREAM IS ON — /watch is readable with the share link",
              flush=True)

    # Sweep before starting. Restarting this service orphans whatever agent was live
    # onto systemd, where nothing will ever clean it up — the same leak stop() had,
    # from the other end. Nothing of ours is running yet at this point, so every
    # match is a leftover, and the conversations themselves are on disk and resumable.
    strays = _stray_agents()
    if strays:
        for pid in strays:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except OSError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        print(f"[serve] cleaned up {len(strays)} leftover agent process(es)", flush=True)

    threading.Thread(target=sess.start, daemon=True).start()
    threading.Thread(target=_watchdog, daemon=True).start()
    threading.Thread(target=_inject_valve, daemon=True).start()
    app.run(host="127.0.0.1", port=a.port, threaded=True)


if __name__ == "__main__":
    main()
