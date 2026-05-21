"""
state.py — Shared in-process state.

Used to coordinate between the webhook handler and the trade stream
so webhook-initiated orders don't trigger a duplicate fill notification.
"""

# Order IDs submitted via the TradingView webhook.
# trade_stream.py checks this set and skips notifications for these orders
# since the webhook handler already sent one at submission time.
webhook_order_ids: set = set()
