# SmartServe V9.2 — Safety, Scheduling & Cancellation Update

## Customer cancellation
- Customer cancellation is available from scheduled/pending discovery through active service and while completion proof is awaiting customer verification.
- Once the customer submits the verification photo and the service enters `AWAITING_PAYMENT`, cancellation is locked.
- Customer can report a service issue/dispute from Live Service and Safety & Tools.

## Provider cancellation
- Provider gets `Cancel service` during `ASSIGNED`, `ACCEPTED`, `ARRIVED`, `IN_PROGRESS`, and `AWAITING_VERIFICATION`.
- Provider cancellation is locked after customer verification (`AWAITING_PAYMENT` and later).
- Provider UI no longer exposes `Report issue`.
- Backend also rejects provider-created disputes.

## Scheduled bookings
- Countdown is shown on the customer dashboard for scheduled bookings, including 2–5 minute schedules.
- Scheduled activation uses the server's local booking time consistently instead of mixing it with SQLite UTC `datetime('now')`.
- Scheduler checks every second.
- At/after the exact scheduled time the booking changes to `SEARCHING` and the customer is automatically taken to provider discovery.
- The discovery page then lists currently eligible online providers in the request radius/service area and refreshes every 5 seconds.
- Socket.IO `scheduled_service_ready` is handled on the customer side for immediate activation.

## SOS / trusted contacts / WhatsApp
- SOS continues to notify SmartServe operations and records an audit event.
- All saved trusted contacts for the user are loaded for the SOS, rather than only the first contact.
- Optional WhatsApp Cloud API support sends a server-side approved template to the user's saved trusted contacts; no SOS details are placed in a public `wa.me` URL.
- WhatsApp requires business configuration, recipient opt-in, an approved template, and valid Meta Cloud API credentials.
- If WhatsApp is not configured or delivery fails, the SmartServe SOS still succeeds and operations are notified.

### WhatsApp environment variables
```env
WHATSAPP_ACCESS_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_GRAPH_VERSION=v23.0
WHATSAPP_SOS_TEMPLATE_NAME=smartserve_sos_alert
WHATSAPP_SOS_TEMPLATE_LANGUAGE=en_US
```

The template should accept four body variables in this order:
1. SmartServe user's name
2. Service/request ID
3. Service name
4. Location link or `Location unavailable`

## Booking timezone
- Scheduled bookings use `SMARTSERVE_TIMEZONE` (default `Asia/Kolkata`) consistently for validation and activation.
