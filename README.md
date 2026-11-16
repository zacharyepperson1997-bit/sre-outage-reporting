# SRE Individual Outage Monitor — Twilio SMS

This GitHub Actions workflow checks the SRE `outages.json` endpoint every
five minutes. It tracks individual outage records using `outageRecID`.

## Alert rules

- A new SMS is sent once an individual outage's `customersOutNow` exceeds 250.
- An outage first seen below the threshold is still monitored; if it later
  grows above 250, an SMS is sent.
- Each outage ID is tracked independently. Multiple qualifying outages
  produce separate messages.
- A restoration SMS is sent for an outage that previously triggered an alert
  when SRE supplies `outageEndTime`, or when the record is absent from two
  consecutive successful map checks.
- An outage dropping below 251 meters is not considered restored.
- Outages that never exceed 250 meters do not generate restoration texts.
- Recent restored IDs are retained for 60 days to prevent stale map responses
  from re-alerting the same outage.

## GitHub repository secrets

Keep these four repository secrets from the Twilio setup:

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_FROM_NUMBER`
- `TWILIO_TO_NUMBER`

Phone numbers should use E.164 format, e.g. `+1XXXXXXXXXX`.

## Files

- `monitor.py`: checks the endpoint, tracks outages, and sends Twilio SMS.
- `.github/workflows/sre-outage-monitor.yml`: five-minute scheduled workflow.
- `state.json`: persistent tracking data, committed by GitHub Actions.

## First run

The first run treats any currently visible qualifying outage as newly detected
and sends an alert for it. This means that if you install or run the upgraded
monitor during an ongoing outage above 250 meters, it will alert you about it.

## Restoration timing

If SRE provides a non-null `outageEndTime`, restoration is notified immediately.
If the record simply disappears, the monitor waits for two consecutive
successful checks (normally about ten minutes) before sending a restoration
message. This guards against temporary map/API gaps.

## Important

GitHub scheduled workflows can be delayed, so five minutes is the requested
polling cadence, not a guaranteed exact delivery interval. A failed request or
invalid JSON response causes the run to fail safely; it is never interpreted
as every outage being cleared.
