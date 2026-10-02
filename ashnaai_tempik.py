#!/usr/bin/env python3
"""
AshnaAI auto-signup menggunakan Tempik (self-hosted disposable email).

Alur:
  1. Buat inbox baru di Tempik (random address)
  2. Isi form signup AshnaAI (name + email)
  3. Solve Cloudflare Turnstile (render widget sendiri, klik via CDP)
  4. Kirim OTP -> baca OTP dari inbox Tempik -> verify
  5. Login + create user via NextAuth credentials
  6. Simpan phone ke profile
  7. Generate API key

PENTING soal Turnstile:
  Turnstile "managed challenge" sering MENOLAK environment headless/datacenter
  dengan error 600010 (bot detected). Peluang lolos jauh lebih tinggi bila:
    - headless = False (browser terlihat)
    - dijalankan di mesin dengan IP residensial (PC lokal / VPS yang bersih)
    - pakai USE_STEALTH = True (butuh: pip install playwright-stealth)

Persyaratan:
  pip install playwright
  playwright install chromium
  (opsional) pip install playwright-stealth

Cara pakai:
  1. Isi CFG di bawah (minimal tempik_url + mail_domain milik sendiri).
  2. python3 ashnaai_tempik.py
"""

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

# ===================== CONFIG =====================
CFG = {
    # --- Tempik (disposable email milik sendiri) ---
    "tempik_url": "https://tempik.haerubirru.my.id",
    "mail_domain": "haerubirru.my.id",

    # --- Identitas signup ---
    "name": "Arjun Mehta",
    "phone": "9876543210",          # 10 digit (default validasi +91 IN)
    "password": "RahasiaKuat2026!",

    # --- Target AshnaAI (sitekey publik, bukan kredensial) ---
    "target": "https://app.ashna.ai/signup",
    "sitekey": "0x4AAAAAAExc-uV3gWRRC3ix",
    "action": "credentials-signup",

    # --- Browser ---
    "headless": False,              # True = tanpa jendela (butuh display utk False di server)
    "use_stealth": False,           # True = pakai playwright-stealth (install dulu)
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
    ),

    # --- Perilaku ---
    "turnstile_retries": 3,         # berapa kali ulang solve Turnstile per langkah
    "otp_timeout": 90,              # detik menunggu OTP masuk
    "save_output": "accounts.txt",  # file simpan email:password:apikey
}

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]
# ==================================================


class TempikAPI:
    """Client untuk Tempik disposable email API."""

    def __init__(self, base_url: str, mail_domain: str):
        self.base_url = base_url.rstrip("/")
        self.mail_domain = mail_domain
        self.session_id = None
        self.inbox_address = None

    def _request(self, path: str, method: str = "GET", data: dict = None) -> dict:
        url = f"{self.base_url}{path}"
        headers = {
            "User-Agent": CFG["user_agent"],
            "Content-Type": "application/json",
        }
        if self.session_id:
            headers["x-session-id"] = self.session_id

        body_bytes = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            print(f"  [Tempik] HTTP {e.code}: {detail[:200]}")
            return {"error": f"HTTP {e.code}"}
        except Exception as e:
            print(f"  [Tempik] request error: {e}")
            return {"error": str(e)}

    def ensure_session(self) -> str:
        if not self.session_id:
            result = self._request("/api/session")
            self.session_id = result.get("sessionId")
            if not self.session_id:
                raise RuntimeError("Gagal membuat session Tempik")
            print(f"[Tempik] Session: {self.session_id[:8]}...")
        return self.session_id

    def create_inbox(self, local_part: str = None) -> str:
        self.ensure_session()
        data = {"domain": self.mail_domain}
        if local_part:
            data["localPart"] = local_part
        result = self._request("/api/inboxes", "POST", data)
        if "error" in result:
            raise RuntimeError(f"Gagal membuat inbox: {result['error']}")
        self.inbox_address = result.get("address")
        print(f"[Tempik] Inbox: {self.inbox_address}")
        return self.inbox_address

    def get_messages(self, address: str = None) -> list:
        addr = address or self.inbox_address
        if not addr:
            raise RuntimeError("Belum ada inbox")
        result = self._request(f"/api/inboxes/{urllib.parse.quote(addr)}/messages")
        return result if isinstance(result, list) else []


def fetch_otp(tempik: TempikAPI, max_wait: int) -> str:
    """Tunggu & ekstrak kode OTP dari inbox Tempik."""
    print("[OTP] Menunggu email verifikasi...")
    # Pola spesifik dulu, lalu fallback angka 4-8 digit.
    patterns = [
        r"verification code is[:\s]+(\d{4,8})",
        r"your code is[:\s]+(\d{4,8})",
        r"\bcode[:\s]+(\d{4,8})",
        r"\bOTP[:\s]+(\d{4,8})",
        r"\b(\d{6})\b",
        r"\b(\d{4,8})\b",
    ]
    rounds = max(1, max_wait // 3)
    for attempt in range(rounds):
        time.sleep(3)
        for msg in tempik.get_messages():
            body = (
                msg.get("text")
                or msg.get("html")
                or msg.get("snippet")
                or msg.get("subject")
                or ""
            )
            for pat in patterns:
                m = re.search(pat, body, re.IGNORECASE)
                if m:
                    otp = m.group(1)
                    print(f"[OTP] Kode: {otp}")
                    return otp
        print(f"[OTP] menunggu... ({attempt + 1}/{rounds})")
    raise RuntimeError("OTP tidak ditemukan dalam batas waktu")


def _render_turnstile(page):
    """Render widget Turnstile sendiri (inject api.js bila perlu)."""
    page.evaluate(
        """async ([sitekey, action]) => {
        if (typeof turnstile === 'undefined') {
            const s = await fetch('https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit')
                        .then(r => r.text());
            (0, eval)(s);
            await new Promise(r => setTimeout(r, 800));
        }
        document.querySelectorAll('[id^=eni-box]').forEach(e => e.remove());
        const box = document.createElement('div');
        box.id = 'eni-box-' + Date.now();
        box.style.cssText = 'position:fixed;left:10px;top:10px;width:320px;height:70px;z-index:99999;background:#fff;';
        document.body.appendChild(box);
        window.__tk = '';
        window.__tk_err = '';
        turnstile.render(box, {
            sitekey, action,
            callback: t => { window.__tk = t; },
            'error-callback': e => { window.__tk_err = String(e); return true; },
        });
    }""",
        [CFG["sitekey"], CFG["action"]],
    )


def _find_turnstile_boxes(page):
    """Temukan iframe Turnstile (lewat shadow root) via CDP, kembalikan titik klik checkbox."""
    cdp = page.context.new_cdp_session(page)
    cdp.send("DOM.enable")
    doc = cdp.send("DOM.getFlattenedDocument", {"depth": -1, "pierce": True})
    boxes = []
    for node in doc.get("nodes", []):
        if node.get("nodeName", "").upper() != "IFRAME":
            continue
        attrs = node.get("attributes") or []
        src = ""
        for i in range(0, len(attrs) - 1, 2):
            if attrs[i] == "src":
                src = attrs[i + 1]
        if "challenges.cloudflare.com" not in src:
            continue
        try:
            m = cdp.send("DOM.getBoxModel", {"nodeId": node["nodeId"]})
            q = m["model"]["content"]
            xs, ys = q[0::2], q[1::2]
            # Checkbox di sisi kiri iframe.
            boxes.append((min(xs) + 28, (min(ys) + max(ys)) / 2))
        except Exception:
            continue
    return cdp, boxes


def _read_token(page):
    return page.evaluate(
        """() => {
            const my = String(window.__tk || '');
            if (my.length > 30) return my;
            const els = document.querySelectorAll('[name=cf-turnstile-response]');
            for (const el of els) {
                if (el.value && String(el.value).length > 30) return String(el.value);
            }
            return '';
        }"""
    )


def solve_turnstile(page):
    """Render + klik Turnstile, kembalikan token. Raise bila gagal setelah retry."""
    page.bring_to_front()

    for attempt in range(1, CFG["turnstile_retries"] + 1):
        _render_turnstile(page)
        time.sleep(3)

        cdp, boxes = _find_turnstile_boxes(page)
        if not boxes:
            print(f"  [turnstile] attempt {attempt}: iframe tidak ditemukan")
            continue

        # Klik tiap kandidat checkbox (gerakan mouse bertahap + CDP click).
        for cx, cy in boxes:
            try:
                page.mouse.move(cx - 150, cy - 60)
                time.sleep(0.2)
                page.mouse.move(cx, cy, steps=12)
                time.sleep(0.2)
            except Exception:
                pass
            for ev in ("mouseMoved", "mousePressed", "mouseReleased"):
                params = {"type": ev, "x": cx, "y": cy, "button": "left", "clickCount": 1}
                if ev == "mouseMoved":
                    params = {"type": ev, "x": cx, "y": cy}
                cdp.send("Input.dispatchMouseEvent", params)
                time.sleep(0.08)

            for _ in range(8):
                time.sleep(2)
                tok = _read_token(page)
                if len(tok) > 30:
                    print(f"  [turnstile] OK (token len={len(tok)})")
                    return tok

        err = page.evaluate("String(window.__tk_err || '')")
        print(f"  [turnstile] attempt {attempt} gagal (error={err or 'none'})")

    raise RuntimeError(
        "Turnstile gagal di-solve. "
        "Jika error 600010: Cloudflare mendeteksi otomasi/IP. "
        "Coba headless=False, IP residensial, atau use_stealth=True."
    )


def inpage(page, expr):
    return page.evaluate(expr)


def set_input(page, sel, val):
    page.evaluate(
        """([sel, val]) => {
        const el = document.querySelector(sel);
        if (!el) return;
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        setter.call(el, val);
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        el.dispatchEvent(new Event('blur', {bubbles: true}));
    }""",
        [sel, val],
    )


def main():
    if "YOURDOMAIN" in CFG["tempik_url"].upper() or not CFG["mail_domain"]:
        sys.exit("Isi CFG['tempik_url'] dan CFG['mail_domain'] dengan milik sendiri.")

    print("=" * 54)
    print("  AshnaAI Auto Signup + Tempik")
    print("=" * 54)

    tempik = TempikAPI(CFG["tempik_url"], CFG["mail_domain"])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=CFG["headless"], args=LAUNCH_ARGS)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=CFG["user_agent"],
            locale="en-US",
        )
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = context.new_page()

        if CFG["use_stealth"]:
            try:
                from playwright_stealth import stealth_sync
                stealth_sync(page)
                print("[init] playwright-stealth aktif")
            except Exception as e:
                print(f"[init] stealth tidak aktif ({e}); lanjut tanpa stealth")

        try:
            # 1. Buat inbox
            print("\n[1] Membuat inbox Tempik...")
            email = tempik.create_inbox()

            # 2. Buka signup
            print("\n[2] Membuka halaman signup...")
            page.goto(CFG["target"], wait_until="networkidle")
            page.bring_to_front()
            time.sleep(2)

            # 3. Isi form name + email
            print("\n[3] Mengisi form...")
            try:
                page.get_by_role("button", name="Signup with Email").click(timeout=10000)
            except Exception:
                page.click('button:has-text("Email")', timeout=8000)
            page.wait_for_selector("#email", timeout=15000, state="visible")
            set_input(page, "#name", CFG["name"])
            set_input(page, "#email", email)
            time.sleep(1)

            # 4. Send OTP
            print("\n[4] Kirim OTP...")
            tok = solve_turnstile(page)
            r = inpage(
                page,
                """(async () => {
                const x = await fetch('/api/auth/otp/send-signup', {method:'POST', credentials:'include',
                    headers:{'content-type':'application/json'},
                    body: JSON.stringify({email: %s, turnstileToken: %s})});
                return x.status + ' ' + await x.text();
            })()"""
                % (json.dumps(email), json.dumps(tok)),
            )
            print(f"  send-signup: {r}")
            if not r.startswith("200"):
                raise RuntimeError(f"send-signup gagal: {r}")

            # 5. Ambil OTP
            print("\n[5] Ambil OTP dari inbox...")
            otp = fetch_otp(tempik, CFG["otp_timeout"])

            # 6. Verify OTP
            print("\n[6] Verifikasi OTP...")
            tok = solve_turnstile(page)
            r = inpage(
                page,
                """(async () => {
                const x = await fetch('/api/auth/otp/verify', {method:'POST', credentials:'include',
                    headers:{'content-type':'application/json'},
                    body: JSON.stringify({otp: %s, email: %s, turnstileToken: %s})});
                return x.status + ' ' + await x.text();
            })()"""
                % (json.dumps(otp), json.dumps(email), json.dumps(tok)),
            )
            print(f"  verify: {r}")
            if not r.startswith("200"):
                raise RuntimeError(f"verify gagal: {r}")

            # 7. Login + create user
            print("\n[7] Login & create user...")
            tok = solve_turnstile(page)
            r = inpage(
                page,
                """(async () => {
                const c = await fetch('/api/auth/csrf', {credentials:'include'});
                const {csrfToken} = await c.json();
                const b = new URLSearchParams({csrfToken, callbackUrl: location.origin, json:'true',
                    name: %s, email: %s, password: %s, phone: %s, otp: %s,
                    turnstileToken: %s, createUser:'true'});
                await fetch('/api/auth/callback/credentials', {method:'POST', credentials:'include',
                    headers:{'content-type':'application/x-www-form-urlencoded'}, body: b.toString()});
                const s = await fetch('/api/auth/session', {credentials:'include'});
                return JSON.stringify(await s.json()).slice(0, 250);
            })()"""
                % (
                    json.dumps(CFG["name"]),
                    json.dumps(email),
                    json.dumps(CFG["password"]),
                    json.dumps("+" + CFG["phone"]),
                    json.dumps(otp),
                    json.dumps(tok),
                ),
            )
            print(f"  session: {r}")
            if email not in r:
                raise RuntimeError("login gagal (session tidak berisi email)")

            # 8. Update profile (phone)
            print("\n[8] Update profile...")
            r = inpage(
                page,
                """(async () => {
                const x = await fetch('/api/auth/update-profile', {method:'POST', credentials:'include',
                    headers:{'content-type':'application/json'},
                    body: JSON.stringify({phone: %s})});
                return x.status + ' ' + (await x.text()).slice(0, 120);
            })()"""
                % json.dumps("+" + CFG["phone"]),
            )
            print(f"  update-profile: {r}")

            # 9. Generate API key
            print("\n[9] Generate API key...")
            apikey = inpage(
                page,
                """(async () => {
                const x = await fetch('/api/generate-token', {method:'POST', credentials:'include',
                    headers:{'content-type':'application/json'}, body:'{}'});
                return await x.text();
            })()""",
            )

            # Simpan hasil
            line = f"{email}:{CFG['password']}:{apikey}"
            try:
                with open(CFG["save_output"], "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception as e:
                print(f"  (gagal simpan file: {e})")

            print("\n" + "=" * 54)
            print("  ✅ BERHASIL")
            print("=" * 54)
            print(f"  Email    : {email}")
            print(f"  Password : {CFG['password']}")
            print(f"  API Key  : {apikey}")
            print(f"  Tersimpan: {CFG['save_output']}")
            print("=" * 54)

        except Exception as e:
            print(f"\n❌ Error: {e}")
            try:
                page.screenshot(path="error.png")
                print("  Screenshot error disimpan: error.png")
            except Exception:
                pass
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    main()
