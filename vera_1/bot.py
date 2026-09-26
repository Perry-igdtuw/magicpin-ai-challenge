"""Vera merchant-assistant challenge bot.

The implementation is deliberately deterministic and dependency-free.  It keeps
the four context layers in memory, routes trigger kinds to small, auditable
composition strategies, and exposes the HTTP contract described by the brief.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


STARTED = time.time()
VALID_SCOPES = {"category", "merchant", "customer", "trigger"}
STORE: dict[tuple[str, str], dict[str, Any]] = {}
CONVERSATIONS: dict[str, list[dict[str, Any]]] = defaultdict(list)
SENT: dict[str, set[str]] = defaultdict(set)
GLOBAL_REPLY_COUNTS: Counter[str] = Counter()
LOCK = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def payload(scope: str, context_id: str) -> dict[str, Any] | None:
    item = STORE.get((scope, context_id))
    return item["payload"] if item else None


def first_name(merchant: dict[str, Any]) -> str:
    identity = merchant.get("identity", {})
    return identity.get("owner_first_name") or identity.get("name", "there").split()[0]


def merchant_greeting(merchant: dict[str, Any]) -> str:
    name = first_name(merchant)
    category = merchant.get("category_slug", "")
    return f"Dr. {name}" if category == "dentists" and not name.lower().startswith("dr") else name


def active_offer(merchant: dict[str, Any], *terms: str) -> str | None:
    offers = merchant.get("offers", [])
    active = [o.get("title", "") for o in offers if o.get("status") == "active"]
    for offer in active:
        if not terms or any(term.lower() in offer.lower() for term in terms):
            return offer
    return active[0] if active else None


def item_for_trigger(category: dict[str, Any], trigger: dict[str, Any]) -> dict[str, Any] | None:
    p = trigger.get("payload", {})
    item_id = p.get("top_item_id") or p.get("digest_item_id") or p.get("alert_id")
    for item in category.get("digest", []):
        if item.get("id") == item_id:
            return item
    return None


def pct(value: Any) -> str:
    try:
        return f"{abs(float(value)) * 100:g}%"
    except (TypeError, ValueError):
        return str(value)


def language_mix(customer: dict[str, Any] | None, merchant: dict[str, Any]) -> bool:
    pref = (customer or {}).get("identity", {}).get("language_pref", "")
    langs = merchant.get("identity", {}).get("languages", [])
    return "hi" in pref.lower() or ("hi" in langs and not pref)


def compose(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compose one message from the four context layers."""
    kind = trigger.get("kind", "")
    p = trigger.get("payload", {}) or {}
    cat = category.get("slug", merchant.get("category_slug", "business"))
    name = merchant_greeting(merchant)
    offer = active_offer(merchant)
    send_as = "merchant_on_behalf" if customer else "vera"
    cta = "open_ended"
    rationale = f"{kind} trigger grounded in the provided {cat} and merchant context."
    body = ""

    if customer:
        cname = customer.get("identity", {}).get("name", "there")
        pref = customer.get("identity", {}).get("language_pref", "")
        if kind == "recall_due":
            slots = p.get("available_slots", [])
            slot_text = " ya ".join(s.get("label", "") for s in slots[:2])
            service = p.get("service_due", "your follow-up").replace("_", " ")
            price = active_offer(merchant, "cleaning", "consultation") or offer
            body = f"Hi {cname}, {merchant.get('identity', {}).get('name', 'the clinic')} here 🦷 It’s been a while since your last visit — your {service} recall is due."
            if slot_text:
                body += f" Apke liye slots ready hain: {slot_text}."
            if price:
                body += f" {price}."
            body += " Reply with a slot that works, or tell us a better time."
            rationale = "Customer consent, recall timing, available slots and a real active offer are used; language matches the customer's preference."
        elif kind in {"wedding_package_followup", "trial_followup"}:
            date = p.get("wedding_date") or p.get("trial_date", "")
            options = p.get("next_session_options", [])
            option = options[0].get("label") if options else None
            body = f"Hi {cname} 👋 {merchant.get('identity', {}).get('name', 'the team')} here."
            if kind == "wedding_package_followup":
                body += f" Your wedding is on {date}; the {p.get('next_step_window_open', 'prep')} window is open now."
            else:
                body += f" Following up on your trial from {date}."
            if offer:
                body += f" We can help you continue with {offer}."
            if option:
                body += f" The next option is {option}."
            body += " Would you like me to hold it?"
        elif kind in {"customer_lapsed_hard", "customer_lapsed_soft"}:
            days = p.get("days_since_last_visit")
            focus = p.get("previous_focus")
            body = f"Hi {cname} 👋 {merchant.get('identity', {}).get('name', 'the team')} here."
            if days:
                body += f" It’s been {days} days since your last visit — no pressure, it happens."
            if focus:
                body += f" We can pick up your {focus.replace('_', ' ')} goal."
            if offer:
                body += f" {offer} is available. Want me to share the next suitable slot?"
            else:
                body += " Would you like to hear what’s available this week?"
            rationale = "Win-back message uses the customer's prior relationship and goal without shame or unsupported claims."
        elif kind == "chronic_refill_due":
            molecules = ", ".join(p.get("molecule_list", [])[:3])
            body = f"Hi {cname}, {merchant.get('identity', {}).get('name', 'your pharmacy')} here. Your refill window is approaching for {molecules}."
            if p.get("delivery_address_saved"):
                body += " We have your delivery address saved. Would you like us to prepare the refill?"
            else:
                body += " Would you like us to prepare the refill for pickup?"
        else:
            body = f"Hi {cname}, {merchant.get('identity', {}).get('name', 'the team')} here. We have an update related to your recent visit. Would you like the details?"
        if language_mix(customer, merchant) and kind == "recall_due":
            body = body.replace("Reply with a slot that works, or tell us a better time.", "Reply 1/2 for a slot, ya apna convenient time bata dijiye.")
        return {"body": body, "cta": cta, "send_as": send_as, "suppression_key": trigger.get("suppression_key", trigger.get("id", "")), "rationale": rationale}

    if kind == "research_digest":
        item = item_for_trigger(category, trigger) or {}
        title = item.get("title") or item.get("summary") or "a new category update"
        source = item.get("source")
        details = title
        if item.get("trial_n"):
            details += f" ({item['trial_n']}-patient evidence)"
        if item.get("summary") and item.get("summary") not in title:
            details += f" — {item['summary']}"
        body = f"{name}, {source or 'this week’s category digest'} has an update relevant to {merchant.get('identity', {}).get('locality', 'your area')}: {details}."
        body += " Want me to pull the source and turn it into a short customer-ready post?"
        if source:
            body += f" — {source}"
        rationale = "Digest item and citation are retrieved from the current category version; the merchant's locality and category make the hook relevant."
    elif kind == "regulation_change":
        item = item_for_trigger(category, trigger) or {}
        deadline = p.get("deadline_iso", "the effective date")
        body = f"{name}, a compliance update needs a look before {deadline}: {item.get('title', 'the regulation change')}."
        body += " I can turn the source into a short checklist for your team. Want that?"
        rationale = "Compliance message foregrounds the supplied deadline and the current category digest item; it does not add legal advice."
    elif kind == "recall_due":
        body = f"{name}, your customer recall trigger is ready, with {len(p.get('available_slots', []))} supplied slot(s). Want me to draft the WhatsApp message?"
    elif kind in {"perf_dip", "seasonal_perf_dip"}:
        metric = p.get("metric", "performance")
        delta = p.get("delta_pct", merchant.get("performance", {}).get("delta_7d", {}).get(f"{metric}_pct"))
        body = f"{name}, {metric} is down {pct(delta)} in the {p.get('window', '7d')} window."
        if p.get("is_expected_seasonal"):
            body += f" This matches the supplied seasonal pattern ({p.get('season_note', 'seasonal acquisition dip')}); retention is the smarter focus now."
        else:
            body += " I can help isolate the likely cause and draft one concrete recovery step. Want me to?"
        rationale = "Performance message uses the observed metric and delta, then distinguishes an expected seasonal dip from an actionable decline."
    elif kind == "perf_spike":
        metric = p.get("metric", "performance")
        body = f"{name}, good signal: {metric} is up {pct(p.get('delta_pct'))} in the last {p.get('window', '7d')} (baseline {p.get('vs_baseline', 'provided')})."
        if p.get("likely_driver"):
            body += f" The likely driver is {p['likely_driver'].replace('_', ' ')}. Want me to build on it?"
        else:
            body += " Want me to help turn the spike into a repeatable post or offer?"
    elif kind == "renewal_due":
        body = f"{name}, your {p.get('plan', merchant.get('subscription', {}).get('plan', 'current'))} plan has {p.get('days_remaining', merchant.get('subscription', {}).get('days_remaining', '?'))} days left."
        if p.get("renewal_amount"):
            body += f" Renewal is ₹{p['renewal_amount']:,}. Want the renewal details?"
        else:
            body += " Want me to share the renewal details?"
    elif kind == "festival_upcoming":
        body = f"{name}, {p.get('festival', 'the festival')} is on {p.get('date', 'the supplied date')} ({p.get('days_until', '?')} days away)."
        body += f" For {cat}, I can shape a time-bound plan around your active offer {offer or 'current services'}. Want a draft?"
    elif kind == "wedding_package_followup":
        body = f"{name}, a bridal follow-up is ready for {p.get('days_to_wedding', '?')} days before the wedding window. Want me to review the next step?"
    elif kind == "curious_ask_due":
        body = f"Hi {first_name(merchant)}! Quick operator question — what service has been most asked-for at {merchant.get('identity', {}).get('name', 'your business')} this week? I’ll turn your answer into a Google post and a short WhatsApp reply."
    elif kind == "winback_eligible":
        body = f"{name}, you’re {p.get('days_since_expiry', '?')} days past expiry and {p.get('lapsed_customers_added_since_expiry', '?')} lapsed customers have been added since then. Want a simple win-back draft?"
    elif kind == "ipl_match_today":
        match = p.get("match", "today’s match")
        body = f"Quick heads-up {first_name(merchant)} — {match} at {p.get('venue', 'the supplied venue')} today. Your existing {offer or 'offer'} is the safest starting point. Want me to draft a delivery-first message?"
    elif kind == "review_theme_emerged":
        body = f"{name}, {p.get('occurrences_30d', '?')} recent reviews mention {p.get('theme', 'the same theme')} and the trend is {p.get('trend', 'emerging')}. Want me to draft a response and a fix-focused post?"
    elif kind == "milestone_reached":
        body = f"{name}, you’re at {p.get('value_now', '?')} {p.get('metric', 'reviews')} — only {max(0, int(p.get('milestone_value', 0)) - int(p.get('value_now', 0)))} to the next milestone. Want a review-request message?"
    elif kind == "active_planning_intent":
        topic = p.get("intent_topic", "your idea").replace("_", " ")
        body = f"{name}, here’s a starter plan for your {topic}: use the real offer {offer or 'your active service'} as the anchor, then I’ll draft the customer-facing copy. Shall I prepare it now?"
        rationale = "Explicit planning intent is routed straight to an action draft instead of restarting qualification."
    elif kind == "supply_alert":
        batches = ", ".join(p.get("affected_batches", []))
        body = f"{name}, compliance alert: {p.get('molecule', 'a medicine')} batches {batches} are listed for recall by {p.get('manufacturer', 'the named manufacturer')}. Want me to prepare the affected-customer filter?"
    elif kind == "category_seasonal":
        body = f"{name}, the supplied {p.get('season', 'seasonal')} trend shows {', '.join(p.get('trends', [])[:3])}. Shelf action is recommended. Want a stock-and-GBP checklist?"
    elif kind == "gbp_unverified":
        body = f"{name}, your Google profile is still unverified; the supplied verification path is {p.get('verification_path', 'the available path')}. Verification is estimated at {pct(p.get('estimated_uplift_pct'))} uplift. Want the steps?"
    elif kind == "cde_opportunity":
        body = f"{name}, the supplied professional opportunity offers {p.get('credits', '?')} credits and is {p.get('fee', 'available')}. Want me to pull the details and draft a participation note?"
    elif kind == "competitor_opened":
        body = f"{name}, {p.get('competitor_name', 'a competitor')} opened {p.get('distance_km', '?')} km away with {p.get('their_offer', 'a listed offer')}. Want a response built around your active offer {offer or 'current service mix'}?"
    elif kind == "dormant_with_vera":
        body = f"Hi {first_name(merchant)}, it’s been {p.get('days_since_last_merchant_message', '?')} days since we last spoke about {p.get('last_topic', 'your profile')}. I can pick up with one useful next step — want that?"
    else:
        body = f"{name}, I have a {kind.replace('_', ' ')} update grounded in your current profile. Want me to prepare the next step?"

    return {"body": body, "cta": cta, "send_as": send_as, "suppression_key": trigger.get("suppression_key", trigger.get("id", "")), "rationale": rationale}


def route_context(cid: str, trigger: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    mid = trigger.get("merchant_id") or trigger.get("payload", {}).get("merchant_id")
    merchant = payload("merchant", mid) or {}
    category = payload("category", merchant.get("category_slug", "")) or {}
    customer_id = trigger.get("customer_id") or trigger.get("payload", {}).get("customer_id")
    customer = payload("customer", customer_id) if customer_id else None
    return category, merchant, customer


def context_for_reply(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    merchant = payload("merchant", body.get("merchant_id")) or {}
    customer = payload("customer", body.get("customer_id")) if body.get("customer_id") else None
    category = payload("category", merchant.get("category_slug", "")) or {}
    return category, merchant, customer


def reply_action(body: dict[str, Any]) -> dict[str, Any]:
    conv = body.get("conversation_id", "unknown")
    message = str(body.get("message", "")).strip()
    normalized = re.sub(r"\s+", " ", message.lower())
    with LOCK:
        CONVERSATIONS[conv].append({"from": body.get("from_role", "merchant"), "body": message, "ts": body.get("received_at", now_iso())})
        GLOBAL_REPLY_COUNTS[normalized] += 1
        history = CONVERSATIONS[conv]
    if re.search(r"\b(stop|unsubscribe|remove me|not interested|don.t message|spam|useless)\b", normalized):
        return {"action": "end", "rationale": "Merchant/customer asked to stop or clearly rejected the outreach; ending immediately and respectfully."}
    if GLOBAL_REPLY_COUNTS[normalized] >= 3 or any(re.search(r"automated assistant|thank you for contacting|team will respond", t.get("body", "").lower()) for t in history):
        if GLOBAL_REPLY_COUNTS[normalized] >= 3:
            return {"action": "end", "rationale": "Repeated identical/canned reply detected as an auto-reply; stopping instead of burning turns."}
        return {"action": "wait", "wait_seconds": 1800, "rationale": "The message resembles an automated acknowledgement; pausing for a human response."}
    if re.search(r"\b(yes|ok|okay|sure|go ahead|let.s do it|proceed|send it|do it|interested)\b", normalized):
        return {"action": "send", "body": "Done — I’m moving this into action now. I’ve prepared the next step from the details already on file; I’ll share the draft for your review shortly.", "cta": "open_ended", "rationale": "Explicit acceptance is routed directly to execution without another qualification question."}
    if re.search(r"\b(later|tomorrow|busy|call me|give me time)\b", normalized):
        return {"action": "wait", "wait_seconds": 1800, "rationale": "Merchant asked for time; backing off for 30 minutes respects the request."}
    if not body.get("merchant_id"):
        return {"action": "send", "body": "Got it. I’ll keep this focused on the next concrete step and use only the details you confirm.", "cta": "open_ended", "rationale": "Acknowledged the reply while avoiding unsupported assumptions."}
    category, merchant, customer = context_for_reply(body)
    if customer:
        return {"action": "send", "body": "Thanks — I’ve noted that and will keep the follow-up tied to your preferred timing. What time works best?", "cta": "open_ended", "rationale": "Customer reply is acknowledged with a low-friction scheduling question."}
    return {"action": "send", "body": f"Understood, {first_name(merchant)}. I’ll keep it practical and use the current {merchant.get('category_slug', 'business')} context for the next draft. What should I prioritize?", "cta": "open_ended", "rationale": "Keeps the conversation on-mission and asks one focused follow-up."}


class Handler(BaseHTTPRequestHandler):
    server_version = "VeraBot/1.0"

    def _send(self, status: int, data: dict[str, Any]) -> None:
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path == "/":
            self._send(200, {
                "name": "Vera merchant assistant",
                "status": "ok",
                "message": "API is running. Use /v1/healthz or /v1/metadata.",
                "endpoints": ["/v1/healthz", "/v1/metadata", "/v1/context", "/v1/tick", "/v1/reply"],
            })
        elif self.path == "/v1/healthz":
            counts = {s: 0 for s in VALID_SCOPES}
            for scope, _ in STORE:
                counts[scope] += 1
            self._send(200, {"status": "ok", "uptime_seconds": int(time.time() - STARTED), "contexts_loaded": counts})
        elif self.path == "/v1/metadata":
            self._send(200, {"team_name": "Vera Context Lab", "team_members": ["Codex"], "model": "deterministic-router-v1", "approach": "four-context deterministic composer with trigger dispatch and replay-safe conversation routing", "contact_email": "challenge@example.com", "version": "1.0.0", "submitted_at": now_iso()})
        else:
            self._send(404, {"error": "not_found"})

    def _json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_POST(self) -> None:
        try:
            data = self._json()
            if self.path == "/v1/context":
                scope = data.get("scope")
                cid = data.get("context_id")
                version = data.get("version")
                if scope not in VALID_SCOPES:
                    return self._send(400, {"accepted": False, "reason": "invalid_scope", "details": "scope must be category, merchant, customer, or trigger"})
                if not isinstance(cid, str) or not isinstance(version, int) or not isinstance(data.get("payload"), dict):
                    return self._send(400, {"accepted": False, "reason": "malformed_context"})
                key = (scope, cid)
                with LOCK:
                    current = STORE.get(key)
                    if current and current["version"] >= version:
                        return self._send(409, {"accepted": False, "reason": "stale_version", "current_version": current["version"]})
                    STORE[key] = {"version": version, "payload": data["payload"]}
                return self._send(200, {"accepted": True, "ack_id": f"ack_{cid}_v{version}", "stored_at": now_iso()})
            if self.path == "/v1/tick":
                actions = []
                for trigger_id in data.get("available_triggers", [])[:20]:
                    trigger = payload("trigger", trigger_id)
                    if not trigger:
                        continue
                    category, merchant, customer = route_context(trigger_id, trigger)
                    if not merchant or not category:
                        continue
                    key = trigger.get("suppression_key", trigger_id)
                    conv = f"conv_{merchant.get('merchant_id', 'merchant')}_{trigger_id}"
                    with LOCK:
                        if key in SENT[conv]:
                            continue
                    msg = compose(category, merchant, trigger, customer)
                    with LOCK:
                        SENT[conv].add(key)
                        CONVERSATIONS[conv].append({"from": "vera", "body": msg["body"], "trigger_id": trigger_id, "ts": data.get("now", now_iso())})
                    actions.append({"conversation_id": conv, "merchant_id": trigger.get("merchant_id"), "customer_id": trigger.get("customer_id"), "send_as": msg["send_as"], "trigger_id": trigger_id, "template_name": "vera_customer_v1" if customer else "vera_context_v1", "template_params": [merchant.get("identity", {}).get("name", ""), msg["body"][:160]], **msg})
                return self._send(200, {"actions": actions})
            if self.path == "/v1/reply":
                return self._send(200, reply_action(data))
            if self.path == "/v1/teardown":
                with LOCK:
                    STORE.clear(); CONVERSATIONS.clear(); SENT.clear(); GLOBAL_REPLY_COUNTS.clear()
                return self._send(200, {"accepted": True})
            self._send(404, {"error": "not_found"})
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"error": "invalid_json", "details": str(exc)})
        except Exception as exc:
            self._send(500, {"error": "internal_error", "details": str(exc)})

    def log_message(self, *_args: Any) -> None:
        return


def run(host: str = "0.0.0.0", port: int = 8080) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Vera bot listening on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    import os
    run(os.getenv("BOT_HOST", "0.0.0.0"), int(os.getenv("PORT", "8080")))
