"""Always-on Telegram poller for Blaze's Pump Alert Bot.

Why this exists: the bot used to wake up only every 5 minutes (cron), so
replies to taps and typed messages took up to 5 minutes to arrive. This
loop long-polls Telegram's getUpdates instead (30s hold) and runs the full
checker the moment a message arrives, so replies land within seconds.

Design notes:
- It never answers anything itself and never acknowledges updates. It only
  watches: when getUpdates returns new updates it runs check_alerts.py once,
  and the checker does the real work (handles the message, price checks,
  sends the reply) and advances last_update_id.
- Vault mode only: the token comes from the Secure Vault connection
  custom.telegram-bot via the checker's raw-surrogate handling. No token
  in this file, in argv, or in any env var.
- The 5-minute cron (pump-alert-bot-telegram) stays as a safety net: if
  this poller ever dies, alerts and replies still work within 5 minutes.
- Logs to hidden_files/poll-log.txt. Exit only on a fatal error; the
  systemd unit restarts it (Restart=always).
"""

import json
import os
import signal
import subprocess
import sys
import time
import traceback
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "hidden_files")
LOG_FILE = os.path.join(LOG_DIR, "poll-log.txt")
CHECKER = os.path.join(HERE, "check_alerts.py")

sys.path.insert(0, HERE)
sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")

import check_alerts  # noqa: E402


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S %Z')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def poll_once():
    """One long-poll. Returns True when new updates arrived."""
    last = check_alerts._load_last_update_id(LOG_DIR)
    url = check_alerts.telegram_url(
        check_alerts.TELEGRAM_GETUPDATES_TEMPLATE, None)
    res = check_alerts.telegram_post(
        url, {"limit": 50, "timeout": 30, "offset": last + 1}, timeout=40)
    if not res.get("ok"):
        raise RuntimeError(
            f"getUpdates not ok: {res.get('description') or res}")
    return bool(res.get("result"))


def run_checker():
    started = time.time()
    proc = subprocess.run(
        [sys.executable, CHECKER],
        cwd=HERE, capture_output=True, text=True, timeout=180)
    elapsed = time.time() - started
    out = (proc.stdout or "").strip().replace("\n", " | ")
    if proc.returncode != 0:
        log(f"checker exit={proc.returncode} ({elapsed:.1f}s) "
            f"out={out} err={(proc.stderr or '').strip()[:300]}")
    else:
        log(f"checker ran ({elapsed:.1f}s) exit=0"
            + (f" out={out}" if out else ""))


def main():
    def _on_signal(signum, frame):
        # Log the signal so a silent death becomes visible in poll-log.txt,
        # then exit cleanly; the watchdog restarts the poller if needed.
        log(f"received signal {signum} — exiting")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    log("poller started (long-poll 30s, triggers checker on new updates)")
    idle_rounds = 0
    while True:
        try:
            if poll_once():
                idle_rounds = 0
                log("new update(s) — running checker")
                run_checker()
            else:
                idle_rounds += 1
                if idle_rounds % 20 == 0:
                    log(f"still watching (idle {idle_rounds} polls)")
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode()[:200]
            except Exception:
                body = ""
            log(f"poll HTTP {e.code} {body} — retry in 5s")
            time.sleep(5)
        except Exception as e:
            # Full traceback goes to poll-log.txt (append-only), so the real
            # cause survives even though poll-stdout.log is truncated on
            # every watchdog restart.
            tb = traceback.format_exc().strip().splitlines()
            log(f"poll error: {e} — retry in 5s | {tb[-1] if tb else ''}")
            time.sleep(5)
        except BaseException as e:
            # SystemExit/KeyboardInterrupt used to kill the poller silently
            # with no log line. Log them here, then let them exit: a plain
            # `raise` would be swallowed by this handler, so re-raise
            # explicitly — otherwise SIGTERM can never stop the poller.
            log(f"poll fatal-ish: {type(e).__name__}: {e}")
            if isinstance(e, (SystemExit, KeyboardInterrupt)):
                raise
            time.sleep(5)


if __name__ == "__main__":
    main()
