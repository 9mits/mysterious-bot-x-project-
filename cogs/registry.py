"""
registry.py — Documents the cog dependency graph and circular import boundaries.

Genuine mutual dependencies (resolved with lazy imports inside function bodies):

  shared.py ↔ modmail.py
    shared.py/send_modmail_panel() lazily imports ModmailPanelView from modmail
    modmail.py imports shared helpers at top level

  shared.py ↔ admin.py
    shared.py/punish_rogue_mod() lazily imports AntiNukeResolveView from admin
    admin.py imports shared helpers at top level

All other cross-cog imports that were previously lazy have been moved to
top-level imports now that the dependency graph is clean.

Load order (set in core/bot.py EXTENSIONS):
  cases → history → case_panel → moderation → roles → derole →
  modmail → automod → config → analytics → admin → events
"""
