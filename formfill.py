#!/usr/bin/env python3
"""formfill: fill web forms in Chrome on the phone from an encrypted profile.

The profile's values never get printed. `scan` shows field labels and options,
`keys` shows profile key names, and `fill` reports only key names and statuses.
"""
import argparse
import datetime as dt
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket
import yaml

HOME = Path(__file__).resolve().parent
ENC = HOME / "profile.yaml.age"
TEMPLATE = HOME / "profile.template.yaml"
MAPS = HOME / "maps"
SHM = Path("/dev/shm/formfill")
UNLOCKED = SHM / "profile.yaml"
PHONE_DIR = "/sdcard/Download/.formfill"
PORT = 9222
ADB_TARGET = "10.205.58.95:5555"


# ---------- utilities ----------

def die(msg):
    print(f"formfill: {msg}", file=sys.stderr)
    sys.exit(1)


def adb(*args, check=True):
    return subprocess.run(["adb", *args], capture_output=True, text=True, check=check)


def ensure_adb():
    out = adb("devices").stdout
    if not re.search(r"\tdevice$", out, re.M):
        gw = subprocess.run(["ip", "route"], capture_output=True, text=True).stdout
        m = re.search(r"default via (\S+)", gw)
        target = f"{m.group(1)}:5555" if m else ADB_TARGET
        adb("connect", target, check=False)
        if not re.search(r"\tdevice$", adb("devices").stdout, re.M):
            die("phone not connected over adb (try: adbc)")
    adb("forward", f"tcp:{PORT}", "localabstract:chrome_devtools_remote")


def load_profile():
    override = os.environ.get("FORMFILL_PROFILE")  # plain-YAML test/demo profile, e.g. test/fake-profile.yaml
    if override:
        p = Path(os.path.expanduser(override))
        if not p.is_file():
            die(f"FORMFILL_PROFILE not found: {p}")
        return yaml.safe_load(p.read_text()) or {}
    if UNLOCKED.exists():
        return yaml.safe_load(UNLOCKED.read_text()) or {}
    die("profile is locked. Run: formfill unlock")


def flatten(d, prefix=""):
    """Yield (dotted.path, value) for every leaf."""
    if isinstance(d, dict):
        for k, v in d.items():
            yield from flatten(v, f"{prefix}{k}.")
    elif isinstance(d, list) and d and all(isinstance(x, (dict, list)) for x in d):
        for i, v in enumerate(d):
            yield from flatten(v, f"{prefix}{i}.")
    else:
        yield prefix[:-1], d


def lookup(profile, path):
    cur = profile
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
        if cur is None:
            return None
    return cur


def parse_date(v):
    if isinstance(v, dt.date):
        return v
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(str(v), fmt).date()
        except ValueError:
            pass
    return None


def fmt_date(d, fmt):
    return (fmt.replace("YYYY", f"{d.year:04d}").replace("YY", f"{d.year % 100:02d}")
               .replace("MM", f"{d.month:02d}").replace("DD", f"{d.day:02d}"))


def resolve(profile, spec):
    """Mapping spec -> value. Forms:
      path.to.key            profile value
      path|MM/DD/YYYY        date reformatted
      {a.b} {c.d}            template of several keys
      =literal               fixed answer (not from profile)
      file:key               upload profile.files.<key>
      today|MM/DD/YYYY       today's date
    """
    spec = str(spec)
    if spec.startswith("="):
        return spec[1:]
    if spec.startswith("file:"):
        return {"file": lookup(profile, "files." + spec[5:])}
    path, _, fmt = spec.partition("|")
    if "{" in path:
        missing = []

        def sub(m):
            v = lookup(profile, m.group(1))
            if v in (None, ""):
                missing.append(m.group(1))
                return ""
            return str(v)
        out = re.sub(r"\{([^}]+)\}", sub, path).strip()
        return None if missing and not out else out
    val = dt.date.today() if path == "today" else lookup(profile, path)
    if val is None:
        return None
    if isinstance(val, list):  # drop blank template rows; flatten list-of-dicts (e.g. medications)
        items = []
        for x in val:
            if isinstance(x, dict):
                x = " ".join(str(v) for v in x.values() if v not in (None, ""))
            if x not in (None, ""):
                items.append(str(x))
        val = items if all(not isinstance(x, dict) for x in val) else "; ".join(items)
        return val or None
    if fmt:
        d = parse_date(val)
        val = fmt_date(d, fmt) if d else val
    elif isinstance(val, dt.date):
        val = val.isoformat()
    return val


# ---------- Chrome DevTools ----------

class Tab:
    def __init__(self, info):
        self.info = info
        self.ws = websocket.create_connection(
            info["webSocketDebuggerUrl"].replace("localhost", "127.0.0.1"),
            suppress_origin=True, timeout=5)
        self.n = 0

    def call(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message"))
                return msg.get("result", {})

    def js(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description", "JS error"))
        return r["result"].get("value")


def pick_tab(match=None):
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5))
    pages = [t for t in tabs if t.get("type") == "page" and t.get("url", "").startswith(("http", "file"))]
    if match:
        pages = [t for t in pages if match.lower() in (t["url"] + t.get("title", "")).lower()]
        if not pages:
            die(f"no tab matches '{match}'")
        return Tab(pages[0])
    for t in pages[:12]:  # find the tab that is on screen
        try:
            tab = Tab(t)
            tab.ws.settimeout(2)
            if tab.js("document.visibilityState") == "visible":
                tab.ws.settimeout(10)
                return tab
            tab.ws.close()
        except Exception:
            continue
    if pages:  # Chrome is in the background: /json/list puts the most recent tab first
        print(f"# Chrome isn't on screen; using its most recent tab: {pages[0]['url'][:80]}", file=sys.stderr)
        return Tab(pages[0])
    die("no Chrome tab with a web page open. Open the form in Chrome, or use --tab <text>")


JS_SCAN = r"""
(() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 140);
  const visible = el => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return (r.width > 0 || r.height > 0 || el.type === 'radio' || el.type === 'checkbox' || el.type === 'file')
      && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const byIds = ids => clean((ids || '').split(/\s+/).map(i => document.getElementById(i)?.innerText).join(' '));
  const ownLabel = el => {
    if (el.labels && el.labels.length) return clean([...el.labels].map(l => l.innerText).join(' '));
    return byIds(el.getAttribute('aria-labelledby')) || clean(el.getAttribute('aria-label'))
      || clean(el.closest('label')?.innerText) || clean(el.placeholder) || clean(el.title) || '';
  };
  const question = el => {
    const fs = el.closest('fieldset'); if (fs) { const lg = fs.querySelector('legend'); if (lg) return clean(lg.innerText); }
    const rg = el.closest('[role=radiogroup],[role=group]');
    if (rg) { const t = byIds(rg.getAttribute('aria-labelledby')) || clean(rg.getAttribute('aria-label')); if (t) return t; }
    let n = el.parentElement;
    for (let i = 0; i < 6 && n; i++, n = n.parentElement) {
      const others = n.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`);
      if (others.length > 1 || i > 1) {
        let t = n.innerText || '';
        others.forEach(o => { const ol = ownLabel(o); if (ol) t = t.replace(ol, ''); });
        t = clean(t.split('\n').find(x => x.trim().length > 1));
        if (t) return t;
      }
    }
    return '';
  };
  const keyOf = el => el.id || el.name || ('label:' + ownLabel(el).toLowerCase());
  const fields = [], groups = {};
  document.querySelectorAll('input, select, textarea').forEach(el => {
    const type = el.tagName === 'INPUT' ? (el.type || 'text') : el.tagName.toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'password', 'search'].includes(type)) return;
    if (el.disabled || !visible(el)) return;
    if ((type === 'radio' || (type === 'checkbox' && el.name &&
         document.querySelectorAll(`input[type=checkbox][name="${CSS.escape(el.name)}"]`).length > 1)) && el.name) {
      const g = groups[el.name] || (groups[el.name] = { key: el.name, type: type === 'radio' ? 'radio' : 'checkbox-group',
        label: question(el), options: [], required: false });
      g.options.push(ownLabel(el) || el.value); g.required = g.required || el.required;
      if (g.options.length === 1) fields.push(g);
      return;
    }
    const f = { key: keyOf(el), type, label: ownLabel(el), required: el.required || el.getAttribute('aria-required') === 'true' };
    if (type === 'select') f.options = [...el.options].map(o => clean(o.text)).filter(Boolean).slice(0, 60);
    if (type === 'checkbox' && !f.label) f.label = question(el);
    fields.push(f);
  });
  const frames = document.querySelectorAll('iframe').length;
  return { url: location.href, host: location.host, title: document.title, fields, frames };
})()
"""

JS_FILL = r"""
((plan) => {
  const norm = s => String(s ?? '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  const labelOf = el => [...(el.labels || [])].map(l => l.innerText).join(' ') || el.getAttribute('aria-label')
     || el.closest('label')?.innerText || el.value || '';
  const keyOf = el => el.id || el.name || ('label:' + (labelOf(el) || el.placeholder || '').replace(/\s+/g, ' ').trim().toLowerCase());
  // words people write vs. words forms offer; each group is interchangeable
  const SYN = [
    ['spouse', 'wife', 'husband', 'partner', 'domestic partner', 'married'],
    ['phone call', 'phone', 'call', 'mobile', 'cell', 'cell phone', 'voice'],
    ['text message', 'text', 'sms', 'texting'],
    ['email', 'e mail'],
    ['occasional', 'occasionally', 'social', 'socially', 'rarely', 'sometimes', 'light'],
    ['none', 'no', 'never', 'n a', 'na', 'do not drink', 'dont drink'],
    ['weekly', 'few times a week', 'moderate'],
    ['daily', 'every day', 'heavy'],
    ['former', 'former smoker', 'quit', 'ex smoker'],
    ['current', 'current smoker', 'yes', 'smoker'],
    ['parent', 'mother', 'father', 'mom', 'dad'],
    ['child', 'son', 'daughter'],
    ['sibling', 'brother', 'sister'],
    ['self', 'me', 'myself', 'patient'],
    ['high blood pressure', 'hypertension', 'htn'],
    ['high cholesterol', 'hyperlipidemia', 'cholesterol'],
    ['depression anxiety', 'depression', 'anxiety'],
    ['sleep apnea', 'osa', 'obstructive sleep apnea'],
    ['diabetes', 'type 2 diabetes', 'type 1 diabetes', 't2d', 'diabetic'],
    ['heart disease', 'cad', 'coronary artery disease'],
    ['thyroid disease', 'hypothyroidism', 'hyperthyroidism', 'thyroid'],
    ['male', 'm', 'man'], ['female', 'f', 'woman'],
  ].map(g => g.map(norm));
  const variants = w => { const out = [w]; for (const g of SYN) if (g.includes(w)) out.push(...g.filter(x => x !== w)); return out; };
  const best = (cands, want) => { // cands: [{text, value, el}]
    const w0 = norm(want); if (!w0) return null;
    for (const w of variants(w0)) {
      const tiers = [c => norm(c.text) === w, c => norm(c.value) === w,
        c => norm(c.text).startsWith(w) || (w.startsWith(norm(c.text)) && norm(c.text).length > 1),
        c => (w.length > 2 && norm(c.text).includes(w)) || (norm(c.text).length > 2 && w.includes(norm(c.text)))];
      for (const t of tiers) { const hit = cands.filter(t); if (hit.length) return hit[0]; }
    }
    return null;
  };
  const setVal = (el, v) => {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
      : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    el.focus();
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.blur();
  };
  const mark = (el, ok) => { const t = (el.type === 'radio' || el.type === 'checkbox') ? (el.closest('label') || el.parentElement) : el;
    if (t) { t.style.outline = ok ? '3px solid #2e9e44' : '3px solid #d93025'; t.style.outlineOffset = '2px'; } };
  const truthy = v => ['true', 'yes', 'y', '1', 'checked', 'on'].includes(norm(v));
  const out = {}; const files = [];
  const all = [...document.querySelectorAll('input, select, textarea')];
  for (const [key, val] of Object.entries(plan)) {
    const els = all.filter(el => el.name === key || el.id === key || keyOf(el) === key);
    if (!els.length) { out[key] = 'not-on-page'; continue; }
    const el = els[0]; const type = el.tagName === 'INPUT' ? el.type : el.tagName.toLowerCase();
    if (val && typeof val === 'object' && 'file' in val) {
      el.setAttribute('data-formfill-upload', key); files.push(key); out[key] = 'upload-pending'; continue; }
    if (type === 'radio' || (type === 'checkbox' && els.length > 1)) {
      const wants = Array.isArray(val) ? val : [val];
      const cands = els.map(e => ({ el: e, text: labelOf(e), value: e.value }));
      let hits = 0, misses = [];
      for (const w of wants) { const c = best(cands, w);
        if (c) { if (!c.el.checked) c.el.click(); mark(c.el, true); hits++; } else misses.push(w); }
      if (type === 'checkbox') els.forEach(e => { if (e.checked && !wants.some(w => best([{ text: labelOf(e), value: e.value }], w))) e.click(); });
      if (type === 'checkbox') {  // a checklist: items not listed simply don't apply
        out[key] = hits ? 'filled' : 'none-apply';
        if (!hits) els.forEach(e => { const t = e.closest('label') || e.parentElement; if (t) t.style.outline = '2px dashed #2e9e44'; });
      } else {
        out[key] = misses.length ? (hits ? 'partial' : 'no-matching-option') : 'filled';
        if (!hits) els.forEach(e => mark(e, false));
      }
    } else if (type === 'checkbox') {
      if (el.checked !== truthy(val)) el.click(); mark(el, true); out[key] = 'filled';
    } else if (type === 'select') {
      const c = best([...el.options].map(o => ({ el: o, text: o.text, value: o.value })), val);
      if (c) { setVal(el, c.el.value); mark(el, true); out[key] = 'filled'; }
      else { mark(el, false); out[key] = 'no-matching-option'; }
    } else {
      let v = Array.isArray(val) ? val.join(', ') : String(val);
      if (type === 'date') { const m = v.match(/^(\d{1,2})[\/-](\d{1,2})[\/-](\d{4})$/); if (m) v = `${m[3]}-${m[1].padStart(2, '0')}-${m[2].padStart(2, '0')}`; }
      setVal(el, v); mark(el, el.value === v || el.value !== ''); out[key] = el.value ? 'filled' : 'rejected';
    }
  }
  // flag required fields still empty
  const empty = all.filter(el => (el.required || el.getAttribute('aria-required') === 'true') && !el.disabled
    && (el.type === 'radio' || el.type === 'checkbox' ? !document.querySelector(`input[name="${CSS.escape(el.name)}"]:checked`) : !el.value));
  empty.forEach(el => { if (!el.style.outline) { el.style.outline = '3px solid #f9ab00'; el.style.outlineOffset = '2px'; } });
  const firstEmpty = empty[0]; if (firstEmpty) firstEmpty.scrollIntoView({ block: 'center' });
  return { results: out, files, requiredEmpty: [...new Set(empty.map(keyOf))] };
})
"""


# ---------- commands ----------

def cmd_init(a):
    if ENC.exists() and not a.force:
        die(f"{ENC.name} already exists (use --force to overwrite)")
    tmp = SHM / "new.yaml"
    SHM.mkdir(mode=0o700, exist_ok=True)
    shutil.copy(TEMPLATE, tmp)
    os.chmod(tmp, 0o600)
    print("Fill in your details in the editor, save, and exit. Leave unknown items blank.")
    _edit_and_encrypt(tmp)


def _edit_and_encrypt(plain):
    editor = os.environ.get("EDITOR", "nano")
    subprocess.run([editor, str(plain)])
    try:
        yaml.safe_load(plain.read_text())
    except yaml.YAMLError as e:
        print(f"YAML error, not saved: {e}\nYour edits are kept at {plain}; fix and run: formfill edit")
        sys.exit(1)
    print("Choose a passphrase for the encrypted profile:")
    r = subprocess.run(["age", "-p", "-o", str(ENC) + ".new", str(plain)])
    if r.returncode != 0:
        die("encryption failed; profile not changed")
    os.replace(str(ENC) + ".new", ENC)
    os.chmod(ENC, 0o600)
    if plain != UNLOCKED:  # init: wipe the temp copy
        subprocess.run(["shred", "-u", str(plain)], check=False)
    print(f"Saved encrypted profile: {ENC}")


def _decrypt_to(dest):
    SHM.mkdir(mode=0o700, exist_ok=True)
    old = os.umask(0o077)
    try:
        r = subprocess.run(["age", "-d", "-o", str(dest), str(ENC)])
    finally:
        os.umask(old)
    if r.returncode != 0:
        dest.unlink(missing_ok=True)
        die("couldn't decrypt (wrong passphrase?)")


def cmd_edit(a):
    if not ENC.exists():
        die("no profile yet. Run: formfill init")
    if not UNLOCKED.exists():
        _decrypt_to(UNLOCKED)
        _schedule_lock(a.minutes)
    _edit_and_encrypt(UNLOCKED)


def _schedule_lock(minutes):
    subprocess.Popen(["setsid", "sh", "-c", f"sleep {int(minutes * 60)}; '{sys.executable}' '{__file__}' lock --quiet"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def cmd_unlock(a):
    if not ENC.exists():
        die("no profile yet. Run: formfill init")
    _decrypt_to(UNLOCKED)
    _schedule_lock(a.minutes)
    print(f"Unlocked for {a.minutes:g} minutes (decrypted copy is in RAM only: {UNLOCKED}).")


def cmd_lock(a):
    if UNLOCKED.exists():
        subprocess.run(["shred", "-u", str(UNLOCKED)], check=False)
        UNLOCKED.unlink(missing_ok=True)
    if shutil.which("adb"):
        adb("shell", "rm", "-rf", PHONE_DIR, check=False)
        adb("forward", "--remove", f"tcp:{PORT}", check=False)
    if not a.quiet:
        # cancel any pending auto-lock timer
        subprocess.run(["pkill", "-f", "sleep.*formfill.py' lock --quiet"], check=False)
        print("Locked: decrypted profile wiped, uploaded files removed from phone.")


def cmd_keys(a):
    src = yaml.safe_load(UNLOCKED.read_text()) if UNLOCKED.exists() else yaml.safe_load(TEMPLATE.read_text())
    for path, v in flatten(src):
        state = "" if not UNLOCKED.exists() else ("  (empty)" if v in (None, "", []) else "")
        print(path + state)


def map_path(host):
    return MAPS / f"{host.replace(':', '_')}.yaml"


def load_map(host, explicit=None):
    p = Path(explicit) if explicit else map_path(host)
    if p.exists():
        return p, (yaml.safe_load(p.read_text()) or {}).get("fields", {}) or {}
    return p, {}


def cmd_scan(a):
    ensure_adb()
    tab = pick_tab(a.tab)
    info = tab.js(JS_SCAN)
    p, mapping = load_map(info["host"], a.map)
    if a.json:
        for f in info["fields"]:
            f["mapped_to"] = mapping.get(f["key"])
        print(json.dumps(info, indent=1))
        return
    print(f"# {info['title']}\n# {info['url']}\n# map: {p}{'' if p.exists() else ' (new)'}")
    if info["frames"]:
        print(f"# note: page has {info['frames']} iframe(s); fields inside them aren't scanned")
    for f in info["fields"]:
        m = mapping.get(f["key"])
        opts = f" [{' | '.join(f['options'][:12])}{' …' if len(f.get('options', [])) > 12 else ''}]" if f.get("options") else ""
        req = "*" if f.get("required") else " "
        print(f"{req}{'✓' if m else '·'} {f['key']!r:34} {f['type']:14} {f['label']}{opts}" + (f"  -> {m}" if m else ""))
    unmapped = sum(1 for f in info["fields"] if f["key"] not in mapping)
    print(f"# {len(info['fields'])} fields, {unmapped} unmapped")


def cmd_fill(a):
    ensure_adb()
    profile = load_profile()
    tab = pick_tab(a.tab)
    host = tab.js("location.host")
    p, mapping = load_map(host, a.map)
    if not mapping:
        die(f"no mapping for {host} yet ({p}). Run: formfill scan, then create the map")
    plan, missing = {}, []
    for key, spec in mapping.items():
        if spec in (None, "", "skip"):
            continue
        v = resolve(profile, spec)
        if v in (None, "", []) or (isinstance(v, dict) and not v.get("file")):
            missing.append(key)
        else:
            plan[key] = v
    if a.dry_run:
        for k in mapping:
            print(f"{k!r:34} {'would fill' if k in plan else 'no profile value' if k in missing else 'skip'}  ({mapping[k]})")
        return
    t0 = time.time()
    res = tab.js(f"({JS_FILL})({json.dumps({k: v for k, v in plan.items()}, default=str)})")
    for key in res["files"]:
        res["results"][key] = _upload(tab, key, plan[key]["file"])
    took = time.time() - t0
    counts = {}
    for k, s in res["results"].items():
        counts[s] = counts.get(s, 0) + 1
    for k in missing:
        res["results"][k] = "no-profile-value"
    print(f"Filled in {took:.1f}s on {host}: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())))
    for k, s in res["results"].items():
        if s != "filled":
            print(f"  {s:18} {k}  ({mapping[k]})")
    if res["requiredEmpty"]:
        print(f"  required & still empty: {', '.join(res['requiredEmpty'])}")
    print("Green = filled, red = couldn't fill, orange = required but empty. Review, sign, and submit yourself.")


def _upload(tab, key, local):
    src = Path(os.path.expanduser(str(local)))
    if not src.is_file() and (HOME / "files" / src.name).is_file():
        src = HOME / "files" / src.name  # forgiving: a typo'd folder, right file name
    if not src.is_file():
        return "upload-file-missing"
    adb("shell", "mkdir", "-p", PHONE_DIR)
    remote = f"{PHONE_DIR}/{src.name}"
    if adb("push", str(src), remote, check=False).returncode != 0:
        return "upload-push-failed"
    doc = tab.call("DOM.getDocument", depth=0)
    node = tab.call("DOM.querySelector", nodeId=doc["root"]["nodeId"],
                    selector=f'[data-formfill-upload="{key}"]')["nodeId"]
    try:
        tab.call("DOM.setFileInputFiles", nodeId=node, files=[remote.replace("/sdcard", "/storage/emulated/0")])
    except RuntimeError as e:
        return f"upload-failed ({e})"
    ok = tab.js(f'document.querySelector(\'[data-formfill-upload="{key}"]\').files.length')
    tab.js(f'(e => (e.dispatchEvent(new Event("change", {{bubbles: true}})), e.style.outline = "3px solid #2e9e44"))'
           f'(document.querySelector(\'[data-formfill-upload="{key}"]\'))')
    return "filled" if ok else "upload-not-accepted"


def main():
    ap = argparse.ArgumentParser(prog="formfill", description=__doc__.splitlines()[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("init", help="create your encrypted profile");  s.add_argument("--force", action="store_true")
    s = sp.add_parser("edit", help="edit the encrypted profile");     s.add_argument("--minutes", type=float, default=30)
    s = sp.add_parser("unlock", help="decrypt into RAM for a while"); s.add_argument("--minutes", type=float, default=30)
    s = sp.add_parser("lock", help="wipe decrypted copy & phone uploads"); s.add_argument("--quiet", action="store_true")
    sp.add_parser("keys", help="list profile key names (no values)")
    for name, h in (("scan", "list fields on the open Chrome tab"), ("fill", "fill the open Chrome tab")):
        s = sp.add_parser(name, help=h)
        s.add_argument("--tab", help="pick the tab whose URL/title contains this text")
        s.add_argument("--map", help="mapping file (default: maps/<host>.yaml)")
        if name == "scan":
            s.add_argument("--json", action="store_true")
        else:
            s.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    {"init": cmd_init, "edit": cmd_edit, "unlock": cmd_unlock, "lock": cmd_lock, "keys": cmd_keys,
     "scan": cmd_scan, "fill": cmd_fill}[a.cmd](a)


if __name__ == "__main__":
    main()
