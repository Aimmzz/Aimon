"""
ai_providers.py — Multi-provider AI layer dengan automatic failover.

Urutan provider diatur di config.AI_PROVIDER_ORDER (default: gemini → groq).
"groq" otomatis diperluas jadi groq, groq2, groq3, ... sesuai jumlah key
di config.GROQ_API_KEYS — tambah key baru cukup edit .env, tidak perlu
sentuh file ini.

Kalau provider kena rate limit (429), dia masuk cooldown dan request
otomatis pindah ke provider berikutnya. Kalau SEMUA provider sedang
cooldown, barulah dianggap rate_limited (bot pause scanning).

Semua provider dipanggil lewat endpoint OpenAI-compatible, jadi cukup
satu code path dengan library `openai`:
    pip install openai
"""

from datetime import datetime, timedelta
from openai import OpenAI, RateLimitError
import config

# ============================================
# PROVIDER STATE
# ============================================

class Provider:
    def __init__(self, name: str, api_key: str, base_url: str, model: str, extra_kwargs: dict = None):
        self.name           = name
        self.model          = model
        self.api_key        = api_key
        self.cooldown_until = None
        self._client        = None
        self.base_url       = base_url
        # Parameter tambahan khusus provider ini, digabung ke tiap request.
        # Dipakai untuk reasoning_effort Gemini — lihat _build_providers().
        self.extra_kwargs   = extra_kwargs or {}

    @property
    def client(self) -> OpenAI:
        # Lazy init + reuse — jangan buat client baru tiap request.
        # max_retries=0: JANGAN biarkan SDK retry sendiri dengan backoff
        # (default 2x retry bisa makan 8-15 detik) — failover ke provider
        # berikutnya jauh lebih cepat daripada retry provider yang sama.
        if self._client is None:
            self._client = OpenAI(
                api_key     = self.api_key,
                base_url    = self.base_url,
                timeout     = config.AI_TIMEOUT_SECONDS,
                max_retries = 0
            )
        return self._client

    def is_available(self) -> bool:
        if not self.api_key:
            return False
        if self.cooldown_until and datetime.now() < self.cooldown_until:
            return False
        return True

    def set_cooldown(self, minutes: float, reason: str = ""):
        self.cooldown_until = datetime.now() + timedelta(minutes=minutes)
        until = self.cooldown_until.strftime("%H:%M:%S")
        print(f"⏸️  Provider [{self.name}] cooldown {minutes:.0f}m sampai {until} — {reason}")

    def cooldown_remaining_str(self) -> str:
        if not self.cooldown_until:
            return "-"
        remaining = (self.cooldown_until - datetime.now()).total_seconds()
        if remaining <= 0:
            return "-"
        return f"{int(remaining // 60)}m{int(remaining % 60)}s"


# ============================================
# REGISTRY
# ============================================

_providers: dict[str, Provider] = {}

def _build_providers():
    """
    Init semua provider dari config. Dipanggil sekali (lazy).
    "gemini" dan "groq" SAMA-SAMA diperluas otomatis jadi gemini/gemini2/...
    dan groq/groq2/... sesuai jumlah key di config.GEMINI_API_KEYS dan
    config.GROQ_API_KEYS — tambah/kurangi key tidak perlu edit file ini.
    """
    global _providers
    if _providers:
        return

    # Gemini 3.5 Flash defaultnya thinking_level="medium" — token "mikir"
    # tersembunyi ini ikut memotong max_tokens yang sama dengan output
    # final. Untuk prompt singkat kayak keputusan trading, reasoning berat
    # tidak perlu, dan tanpa ini max_tokens kita (300-600) sering habis
    # duluan sebelum sempat menulis JSON — hasilnya respons kosong
    # (finish_reason: length) atau JSON kepotong. Berlaku untuk SEMUA
    # key Gemini, bukan cuma yang pertama.
    gemini_extra_kwargs = {"reasoning_effort": "low"}

    for name in config.AI_PROVIDER_ORDER:
        if name == "gemini":
            for i, key in enumerate(config.GEMINI_API_KEYS, start=1):
                label = "gemini" if i == 1 else f"gemini{i}"
                _providers[label] = Provider(
                    name         = label,
                    api_key      = key,
                    base_url     = config.GEMINI_BASE_URL,
                    model        = config.GEMINI_MODEL,
                    extra_kwargs = gemini_extra_kwargs
                )
        elif name == "groq":
            for i, key in enumerate(config.GROQ_API_KEYS, start=1):
                label = "groq" if i == 1 else f"groq{i}"
                _providers[label] = Provider(
                    name     = label,
                    api_key  = key,
                    base_url = config.GROQ_BASE_URL,
                    model    = config.GROQ_MODEL
                )

def get_provider_status() -> list[dict]:
    """Untuk dashboard/logging — status tiap provider."""
    _build_providers()
    return [
        {
            "name"      : p.name,
            "model"     : p.model,
            "available" : p.is_available(),
            "cooldown"  : p.cooldown_remaining_str(),
            "has_key"   : bool(p.api_key)
        }
        for p in _providers.values()
    ]

# ============================================
# RATE LIMIT DETECTION
# ============================================

_DAILY_LIMIT_HINTS = ("per day", "perday", "daily", "rpd", "tpd", "quota")

# Model yang sudah di-deprecate/dimatikan akan selalu gagal terlepas
# berapa lama ditunggu — ini BUKAN rate limit, jadi jangan diperlakukan
# sebagai limit harian (yang menyiratkan "akan pulih besok").
_MODEL_GONE_HINTS = ("not found", "not_found", "404", "deprecated", "no longer available", "does not exist")

def _classify_and_cooldown(provider: Provider, error_msg: str):
    """
    Tentukan lama cooldown berdasarkan jenis error.
    - Model sudah dimatikan/deprecated → cooldown panjang + pesan jelas
    - Limit harian (RPD/TPD/quota) → cooldown panjang, tidak akan reset cepat
    - Limit per-menit (RPM/TPM)   → cooldown pendek
    """
    lower = error_msg.lower()

    if any(h in lower for h in _MODEL_GONE_HINTS):
        provider.set_cooldown(
            config.PROVIDER_COOLDOWN_DAILY_MIN,
            f"model '{provider.model}' sepertinya sudah deprecated/dimatikan — "
            f"CEK & GANTI model string di config.py, menunggu tidak akan menyelesaikan ini"
        )
    elif any(h in lower for h in _DAILY_LIMIT_HINTS):
        provider.set_cooldown(config.PROVIDER_COOLDOWN_DAILY_MIN, "limit harian habis")
    else:
        provider.set_cooldown(config.PROVIDER_COOLDOWN_MINUTE_MIN, "limit per-menit")

def _is_rate_limit_error(e: Exception) -> bool:
    """
    True kalau error ini harus memicu cooldown + failover ke provider lain.
    Mencakup rate limit SUNGGUHAN (429) maupun model yang sudah tidak ada
    (404/deprecated) — keduanya sama-sama butuh cooldown supaya tidak
    dicoba ulang di setiap kandidat dalam cycle yang sama.
    """
    if isinstance(e, RateLimitError):
        return True
    msg = str(e).lower()
    if "429" in msg or "rate_limit" in msg or "resource_exhausted" in msg:
        return True
    return any(h in msg for h in _MODEL_GONE_HINTS)

# ============================================
# PUBLIC API
# ============================================

def chat(
    system: str,
    user: str,
    temperature: float = 0.1,
    max_tokens: int = 500,
    json_mode: bool = True
) -> dict:
    """
    Kirim chat completion ke provider pertama yang available.
    Failover otomatis ke provider berikutnya kalau kena rate limit.

    Return:
        {"ok": True,  "content": str, "provider": str}
        {"ok": False, "all_rate_limited": True}          ← semua provider cooldown
        {"ok": False, "all_rate_limited": False, "error": str}  ← error non-rate-limit
    """
    _build_providers()

    last_error    = None
    any_attempted = False

    for provider in _providers.values():
        if not provider.is_available():
            continue

        any_attempted = True

        try:
            kwargs = {
                "model"       : provider.model,
                "messages"    : [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user}
                ],
                "temperature" : temperature,
                "max_tokens"  : max_tokens,
            }
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            # Parameter tambahan khusus provider ini (misal reasoning_effort
            # untuk Gemini) — Groq tidak punya extra_kwargs jadi tidak
            # terpengaruh sama sekali.
            kwargs.update(provider.extra_kwargs)

            response = provider.client.chat.completions.create(**kwargs)

            # Ambil konten mentah TANPA langsung .strip() — kalau kosong
            # (None), .strip() akan meledak dengan AttributeError yang
            # membingungkan. Ini terjadi kalau token "thinking" (reasoning
            # tersembunyi Gemini) menghabiskan seluruh max_tokens sebelum
            # sempat menulis output — makanya reasoning_effort di atas
            # penting, tapi tetap jaga-jaga untuk kasus residual.
            content_raw = response.choices[0].message.content
            if not content_raw:
                finish_reason = response.choices[0].finish_reason if response.choices else "unknown"
                raise ValueError(
                    f"Respons kosong (kemungkinan token habis oleh reasoning/thinking). "
                    f"Finish reason: {finish_reason}"
                )
            content = content_raw.strip()

            return {"ok": True, "content": content, "provider": provider.name}

        except Exception as e:
            last_error = str(e)

            if _is_rate_limit_error(e):
                # Selalu print pesan mentah SEBELUM diklasifikasi — supaya
                # kalau heuristik salah menandai error sebagai "model
                # deprecated" (padahal sebenarnya sebab lain, misal region
                # lock / masalah tier free API key), pesan aslinya tetap
                # kelihatan untuk didiagnosis, bukan tertutup pesan kalengan.
                print(f"   [debug] Raw error [{provider.name}]: {last_error[:200]}")
                _classify_and_cooldown(provider, last_error)
                continue  # failover ke provider berikutnya

            print(f"❌ Provider [{provider.name}] error: {last_error[:120]}")
            continue

    all_on_cooldown = all(not p.is_available() for p in _providers.values())

    if all_on_cooldown or not any_attempted:
        statuses = ", ".join(
            f"{p.name}:{p.cooldown_remaining_str()}" for p in _providers.values()
        )
        print(f"⏸️  SEMUA provider tidak tersedia — {statuses}")
        return {"ok": False, "all_rate_limited": True, "error": last_error or "no provider"}

    return {"ok": False, "all_rate_limited": False, "error": last_error or "unknown"}


if __name__ == "__main__":
    print("🧪 Test AI Providers\n")

    for s in get_provider_status():
        key_status = "✅" if s["has_key"] else "❌ API key kosong"
        print(f"  [{s['name']}] model={s['model']} available={s['available']} {key_status}")

    print("\n📨 Test chat (JSON mode)...")
    result = chat(
        system = "Kamu asisten yang selalu jawab dengan JSON valid.",
        user   = 'Jawab dengan JSON: {"status": "ok", "pesan": "sapaan singkat bahasa Indonesia"}',
        max_tokens = 100
    )

    if result["ok"]:
        print(f"✅ Provider terpakai: {result['provider']}")
        print(f"📄 Response: {result['content']}")
    else:
        print(f"❌ Gagal: {result}")