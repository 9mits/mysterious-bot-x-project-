"""
Bot-level constants — identity values are overridable via environment variables
so multiple instances can run from the same codebase.
"""
from __future__ import annotations

import os

import discord

# ---------------------------------------------------------------------------
# Bot identity  (override per-instance via .env)
# ---------------------------------------------------------------------------
BRAND_NAME = os.environ.get("BOT_BRAND_NAME", "Mysterious Bot X")
TOKEN_ENV_VARS = ("DISCORD_BOT_TOKEN", "MBX_BOT_TOKEN")

# ---------------------------------------------------------------------------
# Guild / Discord IDs  (fallbacks used before config.json is loaded)
# ---------------------------------------------------------------------------
DEFAULT_GUILD_ID = 1351136089259114516
DEFAULT_ANCHOR_ROLE_ID = 1433987521133674597  # custom roles are positioned under this role

DEFAULT_ROLE_OWNER = 1351544048934191185
DEFAULT_ROLE_ADMIN = 1351544086556835921
DEFAULT_ROLE_MOD = 1351544100482056254
DEFAULT_ROLE_COMMUNITY_MANAGER = 1453995056586424340
DEFAULT_ARCHIVE_CAT_ID = 1454629061556043890
DEFAULT_SPAM_ROLE_ID = 1352841838985482322

# ---------------------------------------------------------------------------
# Operational defaults
# ---------------------------------------------------------------------------
DEFAULT_MESSAGE_CACHE_LIMIT = 5000
DEFAULT_MESSAGE_CACHE_RETENTION_DAYS = 14
DEFAULT_MAX_UNREAD_PINGS = 100
COOLDOWN_SECONDS = 60

# Default moderation rules used when config.json has none.
# Values are timeout durations in minutes; 0 = warn only.
# Discord's timeout ceiling is ~28 days (40 320 minutes).
DEFAULT_RULES = {
    "Spamming":           {"base": 0,     "escalated": 60},    # Warn → 1 h
    "Insults":            {"base": 0,     "escalated": 120},   # Warn → 2 h
    "Harassment":         {"base": 1440,  "escalated": 10080}, # 24 h → 7 d
    "NSFW":               {"base": 10080, "escalated": 40320}, # 7 d  → 28 d
    "Scamming":           {"base": 40320, "escalated": 40320}, # 28 d → 28 d
    "Racism":             {"base": 40320, "escalated": 40320}, # 28 d → 28 d
    "Advertising":        {"base": 0,     "escalated": 1440},  # Warn → 24 h
    "Begging":            {"base": 0,     "escalated": 60},    # Warn → 1 h
    "Trolling":           {"base": 60,    "escalated": 1440},  # 1 h  → 24 h
    "Doxing":             {"base": 40320, "escalated": 40320}, # 28 d → 28 d
    "Hate Speech":        {"base": 10080, "escalated": 40320}, # 7 d  → 28 d
    "Impersonation":      {"base": 1440,  "escalated": 10080}, # 24 h → 7 d
    "Political":          {"base": 0,     "escalated": 60},    # Warn → 1 h
    "Raid":               {"base": 40320, "escalated": 40320}, # 28 d → 28 d
    "Exploiting":         {"base": 10080, "escalated": 40320}, # 7 d  → 28 d
    "Inappropriate Lang": {"base": 0,     "escalated": 60},    # Warn → 1 h
    "Ping Spam":          {"base": 30,    "escalated": 120},   # 30 m → 2 h
    "Misinformation":     {"base": 0,     "escalated": 360},   # Warn → 6 h
    "Off-Topic":          {"base": 0,     "escalated": 30},    # Warn → 30 m
    "Argumentative":      {"base": 0,     "escalated": 60},    # Warn → 1 h
}

# ---------------------------------------------------------------------------
# UI / branding
# ---------------------------------------------------------------------------
SCOPE_SYSTEM = "Control Center"
SCOPE_MODERATION = "Moderation Suite"
SCOPE_SUPPORT = "Support Center"
SCOPE_ROLES = "Custom Roles"
SCOPE_ANALYTICS = "Analytics"

THEME_ORANGE = discord.Color.from_rgb(255, 153, 0)
EMBED_PALETTE = {
    "neutral":   THEME_ORANGE,
    "success":   discord.Color.from_rgb(87, 242, 135),
    "warning":   THEME_ORANGE,
    "danger":    discord.Color.from_rgb(237, 66, 69),
    "info":      THEME_ORANGE,
    "muted":     THEME_ORANGE,
    "support":   THEME_ORANGE,
    "analytics": THEME_ORANGE,
}

FEATURE_FLAG_LABELS = {
    "advanced_case_panel":   "Case Panel",
    "advanced_modmail":      "Advanced Modmail",
    "setup_validation":      "Setup Check",
    "config_panel":          "Bot Settings Panel",
    "role_cleanup":          "Lost Booster Role Cleanup",
    "smart_automod":         "Smart Auto-Moderation",
    "native_automod_bridge": "Native AutoMod Follow-Up",
    "automod_panel":         "AutoMod Panel",
    "dm_modmail_prompt":     "DM Modmail Prompt",
}

# Holographic role colour presets (stored as integer RGB values)
HOLO_PRIMARY   = 11127295
HOLO_SECONDARY = 16759788
HOLO_TERTIARY  = 16761760

# ---------------------------------------------------------------------------
# Modmail panel
# ---------------------------------------------------------------------------
MODMAIL_PANEL_BANNER_URL = (
    "https://cdn.discordapp.com/attachments/1430583478713450506/"
    "1475440172790452466/New_Project_4_2.png"
    "?ex=699d7e3d&is=699c2cbd"
    "&hm=3ab07aa5ab3a612760ce8b4d8af6e6460a67df380fe28059468b9570429093e5&"
)

MODMAIL_PANEL_CATEGORIES = [
    ("Report",          "User reports, message links, IDs, or evidence."),
    ("General Support", "Server help, questions, or moderator assistance."),
    ("Bot Support",     "Bugs, broken commands, or automation issues."),
    ("Partnership",     "Partnership requests and server details."),
]
